"""De novo assembly.

Note the path: `ncbi_assembly_handlers.py` is the sibling that *downloads* a
published assembly, and until 2026-08-01 it lived here. Anything pointing at
`queue/assembly_handlers.py` for the download path -- including
`docs/superpowers/specs/2026-07-29-ncbi-unified-download-design.md` -- means
that file, not this one.

The shape differs from every other pipeline handler in one way that drives the
rest: a single job produces several first-class outputs rather than one. Flye
writes contigs, an assembly graph and a per-contig table, and two of those
three become DataObjects. The nearest precedent is the NCBI download handler,
which already makes four objects from one job, so `_apply_assemble_reads`
follows `_apply_assembly_download` rather than `_apply_align_reads`.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import assembler_registry, assembly_params, assembly_runner, tools
from app.pipelines.assemblers import Assembler, OutputKind
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _failure, _prepare_workdir, _resolve_input
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# Assembly runs for hours. The default lease is minutes, so without this a
# laptop lid closing pauses the VM, the lease expires, and a second worker
# adopts a job that is still running -- at which point epoch fencing correctly
# rejects one of them, but only after both have burned the time.
#
# Six hours rather than the one hour the download handlers use: a bacterial
# assembly is minutes and a large eukaryotic one is a day, and the cost of
# over-reserving is only that a genuinely dead job is reclaimed later.
ASSEMBLY_LEASE_SECONDS = 6 * 3600


@handler(
    "assemble_reads",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # cpu is overridden per job with the user's thread count, as trim_reads and
    # align_reads both do -- see pipeline_service.launch_assembly. mem_mb is
    # declared honestly rather than usefully: governor.py does not read it
    # today, so the real guard is the launch-time estimate.
    resources=JobResources(cpu=8, mem_mb=16384, io=IoClass.HEAVY),
    # One attempt. Every other pipeline handler retries because its work is
    # cheap enough to repeat; a retried assembly costs hours and fails the same
    # way twice, since the input is identical and the tool is deterministic.
    # A genuine transient (a full disk) is better surfaced to the user than
    # silently re-run overnight.
    max_attempts=1,
)
def assemble_reads(ctx: JobContext) -> dict:
    """Assemble long reads into contigs, with no reference.

    Deliberately does not use Flye's `--resume`. Resuming needs the previous
    run's working directory, which `reap_pipeline_scratch` exists to delete,
    and a resume that silently found a half-deleted workdir would produce an
    assembly nobody could describe. A retry is a fresh run.
    """
    assembler = Assembler(ctx.payload.get("assembler", Assembler.FLYE))
    spec = assembler_registry.spec_for(assembler)
    if spec.tool is None:
        raise PermanentError(
            spec.unavailable_reason or f"{assembler.value} is not installed"
        )

    tool = tools.require(spec.tool())
    params = assembly_params.from_dict(ctx.payload.get("params"))

    work = _prepare_workdir(ctx, "assembly")
    reads = _resolve_input(ctx.payload, "reads")
    # A named link, for the reason Phase 6a established and every handler here
    # repeats: Flye infers gzip from the filename, and a content-addressed blob
    # has no extension at all.
    reads = _named_link(work, reads, ctx.payload.get("reads_name"))

    # Optional second mate. `_resolve_input` is already side-parameterized, so
    # the paired path needs no new plumbing -- only a payload key that
    # `launch_assembly` sets when it identified a mate.
    mate: Path | None = None
    if payload_has_mate(ctx.payload):
        mate = _resolve_input(ctx.payload, "mate")
        mate = _named_link(work, mate, ctx.payload.get("mate_name"))

    out_dir = work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = assembly_runner.build_assembly_command(
        assembler=assembler,
        tool_path=tool.path,
        reads=reads,
        out_dir=out_dir,
        params=params,
        mate=mate,
        # Set by launch_assembly from the same estimate that decided this run
        # could proceed. None for Flye, and for an ABySS run with no estimate
        # -- the builder floors it either way.
        memory_bytes=ctx.payload.get("memory_bytes"),
    )

    if assembler is Assembler.ABYSS:
        progress = assembly_runner.AbyssProgress()
    elif assembler is Assembler.FLYE:
        progress = assembly_runner.AssemblyProgress(
            stage_order=assembly_runner.flye_stage_order(params)
        )
    else:
        # SPAdes and MEGAHIT (and any future assembler landing here): no
        # stage-order source exists for either (flye_stage_order reads
        # params.iterations, which only FlyeParams has), and nothing in this
        # codebase parses their progress banners. An empty stage_order is the
        # honest default -- AssemblyProgress's docstring says exactly this
        # falls back to reporting the phase name alone, with no phase
        # structure. MEGAHIT's k-sweep would suit a step counter, but its
        # k-list is configurable and its log line format is not parsed here,
        # so a denominator would be invented.
        progress = assembly_runner.AssemblyProgress()
    ctx.progress(phase="starting", pct=None, message=f"starting {assembler.value}")
    ctx.extend_lease(ASSEMBLY_LEASE_SECONDS)

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log.info(
        "assembly_started",
        job_id=ctx.job_id,
        assembler=assembler.value,
        mode=getattr(params, "mode", None),
        threads=params.threads,
        genome_size=params.genome_size,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path), parser=progress)
    if code != 0:
        raise _failure(code, log_path, assembler.value)

    try:
        found = assembly_runner.harvest(out_dir, spec.outputs)
    except FileNotFoundError as e:
        # Retryable rather than permanent: an exit-0 run with no contigs is
        # most plausibly a disk that filled during the final write, which a
        # later attempt on a tidier machine would survive. `max_attempts=1`
        # means it will not actually be retried automatically -- the class is
        # what tells the user whether re-running is worth their time.
        raise RetryableError(str(e)) from None

    contigs = found[OutputKind.CONTIGS]
    graph = found.get(OutputKind.GRAPH)

    facts: dict = {}
    info_path = found.get(OutputKind.INFO_TABLE)
    if info_path is not None:
        # Two different tables: Flye's `assembly_info.txt` carries coverage and
        # circularity; ABySS's `-stats.tab` carries contiguity including N50.
        # Parse failures are swallowed inside both parsers: a table that could
        # not be read must not fail an assembly that took six hours and
        # produced a perfectly good FASTA.
        if assembler is Assembler.ABYSS:
            facts = assembly_runner.parse_abyss_stats(
                info_path.read_text(errors="replace")
            )
        else:
            facts = assembly_runner.parse_assembly_info(
                info_path.read_text(errors="replace")
            )

    ctx.progress(phase="done", pct=1.0, message="assembly complete")
    log.info(
        "assembly_finished",
        job_id=ctx.job_id,
        contigs=facts.get("assembly_contig_count"),
        circular=facts.get("assembly_circular_count"),
        size=contigs.stat().st_size,
    )

    return {
        "object_id": ctx.payload.get("object_id"),
        "project_id": ctx.payload.get("project_id"),
        "job_id": ctx.job_id,
        "assembler": assembler.value,
        "tool_version": tool.version,
        "params": params.as_dict(),
        "contigs": {"tmp_path": str(contigs), "name": _contigs_name(ctx)},
        "graph": (
            {"tmp_path": str(graph), "name": _graph_name(ctx)}
            if graph is not None
            else None
        ),
        "assembly_facts": facts,
        "workdir": str(work),
    }


def payload_has_mate(payload: dict) -> bool:
    return bool(payload.get("mate_sha256") or payload.get("mate_path"))


def _contigs_name(ctx: JobContext) -> str:
    """`<sample>.assembly.fasta`, or Flye's own name if we have nothing better.

    Named after the reads rather than left as `assembly.fasta`: a project that
    assembles three samples would otherwise hold three files with identical
    names, distinguishable only by clicking each one.
    """
    stem = _reads_stem(ctx)
    return f"{stem}.assembly.fasta" if stem else "assembly.fasta"


def _graph_suffix(assembler: str) -> str:
    """ABySS emits Graphviz `.dot`; Flye and SPAdes both emit GFA.

    Naming an ABySS graph `.gfa` would be a lie that survives into the object
    store, and `AssemblyGraph.tsx` would then try to render it as GFA and fail
    confusingly rather than declining cleanly.

    Still a two-way string comparison with an `else` fallback rather than a
    mapping keyed by every `Assembler` member, so a future assembler emitting
    neither format would be silently misnamed as GFA. That structural gap is
    tracked as a follow-up rather than fixed here; SPAdes happens to be
    correct today because it genuinely does emit GFA.
    """
    return ".assembly_graph.dot" if assembler == "abyss" else ".assembly_graph.gfa"


def _graph_name(ctx: JobContext) -> str:
    suffix = _graph_suffix(ctx.payload.get("assembler", "flye"))
    stem = _reads_stem(ctx)
    return f"{stem}{suffix}" if stem else f"assembly_graph{suffix[len('.assembly_graph'):]}"


def _reads_stem(ctx: JobContext) -> str:
    name = ctx.payload.get("reads_name") or ""
    for suffix in (".fastq.gz", ".fq.gz", ".fastq", ".fq", ".fasta", ".fa"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem if name else ""


def _named_link(work: Path, target: Path, name: str | None) -> Path:
    """A symlink to the reads under their user-facing name.

    Same helper as `pipeline_handlers._named_link` in intent; not imported from
    there because that one is private to the trim path and takes its default
    from it. Duplicating six lines is better than making a private helper into
    a shared contract for the sake of it.
    """
    link = work / (name or "reads.fastq")
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    return link
