"""Launching pipeline runs.

Sits between the API and the queue: resolves which files a run will read,
validates that they can actually be trimmed, and builds the payload the
handler expects. Kept out of the router so the launch rules are testable
without HTTP.
"""

from dataclasses import dataclass
from pathlib import Path

from beanie import PydanticObjectId

from app.config import settings
from app.errors import ConflictError, NotFoundError, ValidationError
from app.logging import get_logger
from app.models import (
    ACTIVE_STATES,
    BlobStorage,
    DataObject,
    FormatKind,
    IoClass,
    Job,
    JobClass,
    JobResources,
    ObjectRole,
    ObjectStatus,
    RunInput,
    RunInputRole,
    RunJobRole,
    RunKind,
    SidecarRole,
)
from app.pipelines import (
    align_params as align_params_module,
)
from app.pipelines import (
    align_runner,
    aligner_registry,
    aligners,
    cutadapt_runner,
    fastp_runner,
    pairing,
    tools,
    trimmomatic_runner,
    variant_runner,
)
from app.pipelines.aligners import Aligner
from app.services import blob_service, run_service
from app.storage.paths import blob_path

log = get_logger(__name__)

TRIMMABLE_KINDS = {FormatKind.FASTQ}


async def suggest_mate(obj: DataObject) -> DataObject | None:
    """The file this one would be trimmed alongside, if any.

    Prefers the persisted link, which a user may have corrected, and falls back
    to the filename convention for a pair whose ingest predates mate linking.
    """
    if obj.mate_object_id is not None:
        return await DataObject.get(obj.mate_object_id)

    if pairing.split_mate(obj.name) is None:
        return None

    candidates = await DataObject.find(
        DataObject.project_id == obj.project_id,
        DataObject.id != obj.id,
    ).to_list()
    matches = [c for c in candidates if pairing.is_mate_of(obj.name, c.name)]
    return matches[0] if len(matches) == 1 else None


_TRIM_PARAM_TYPES = {
    "fastp": fastp_runner.TrimParams,
    "cutadapt": cutadapt_runner.CutadaptParams,
    "trimmomatic": trimmomatic_runner.TrimmomaticParams,
}

# Tools whose *default* length/quality filters were tuned for Illumina reads
# and can discard most of a long-read run -- fastp's min_length defaults to
# 15, Trimmomatic's to 36 (its own documented default). cutadapt's own
# summary in tools.py advertises cross-platform support and its min_length
# defaults to 1, so it does not share this failure mode and is deliberately
# excluded: warning about it here would be a false alarm, not a caution.
_SHORT_READ_TUNED_TRIM_TOOLS = {"fastp", "trimmomatic"}


def default_params(tool: str = "fastp") -> dict:
    """Server-owned defaults for the named trim tool, so the form does not
    encode its own copy. Raises for any tool this application has no runner
    for -- the same "does this application call it" question TOOL_META's
    `runnable` flag answers, checked here rather than trusted from the
    caller."""
    params_cls = _TRIM_PARAM_TYPES.get(tool)
    if params_cls is None:
        raise ValidationError(f"Unknown trim tool: {tool!r}")
    return params_cls(threads=settings.pipeline_default_threads).as_dict()


def _check_tool_runnable(tool: str) -> None:
    """Assert a trim tool both exists and has an actual code path.

    Distinct from `tools.require`, which only checks the binary is usable --
    an unrecognized tool name would pass that check trivially (it is simply
    absent from `all_tools()`) and reach the queue with no handler branch to
    run it.
    """
    if tool not in _TRIM_PARAM_TYPES:
        raise ValidationError(f"Unknown trim tool: {tool!r}")


def _trim_tool(tool: str):
    """Look up the probed `Tool` for a trim tool name.

    Assumes `tool` was already validated by `_check_tool_runnable` -- an
    unrecognized name raises `KeyError` here, not `ValidationError`.
    """
    return {
        "fastp": tools.fastp,
        "cutadapt": tools.cutadapt,
        "trimmomatic": tools.trimmomatic,
    }[tool]()


async def _resolve_readable(obj: DataObject) -> tuple[str | None, str | None]:
    """Locate an object's bytes as (digest, path).

    Registered-in-place files have no managed blob to address by hash, so the
    external path is the only way to reach them.
    """
    if obj.blob_sha256 is None:
        raise ValidationError(
            f"{obj.name!r} has no stored content yet (status={obj.status.value})"
        )

    blob = await blob_service.find_present_blob(obj.blob_sha256)
    if blob is not None and blob.storage is BlobStorage.EXTERNAL:
        if not blob.external_path:
            raise ValidationError(f"{obj.name!r} is registered in place but has no path")
        return None, blob.external_path
    return obj.blob_sha256, None


def _check_fastq_ready(obj: DataObject, *, verb: str = "trim") -> None:
    """Assert an object is a FASTQ that is ready to be read.

    Shared by trim and QC, which have identical input requirements. `verb` only
    shapes the message: "not ready to trim" on a QC run would send the user
    looking for a trim they never started.
    """
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready to {verb} (status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind not in TRIMMABLE_KINDS:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not FASTQ reads",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )


async def launch_trim(
    *,
    object_id: PydanticObjectId,
    mate_object_id: PydanticObjectId | None = None,
    params: dict | None = None,
    paired: bool = True,
    tool: str = "fastp",
):
    """Queue a trim run over one object, or an R1/R2 pair.

    `paired=False` forces single-end treatment even when a mate is known, which
    is the escape hatch for a pair that should not be trimmed together.
    """
    from app.queue import queue

    _check_tool_runnable(tool)
    tools.require(_trim_tool(tool))

    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")
    _check_fastq_ready(obj)

    # fastp's and Trimmomatic's default length/quality filters are tuned for
    # short reads and can discard most of a long-read run -- wrong by
    # default, but not never legitimate, so this warns rather than blocks
    # (the same choice already made for desynchronizing an unpaired mate).
    # cutadapt does not share this failure mode (see
    # _SHORT_READ_TUNED_TRIM_TOOLS) and is deliberately excluded.
    long_read_advisory = tool in _SHORT_READ_TUNED_TRIM_TOOLS and is_long_read(obj)
    if long_read_advisory:
        log.warning(
            "trim_long_read_advisory",
            object_id=str(obj.id),
            tool=tool,
            message=f"{tool}'s short-read-tuned defaults may discard most of this run",
        )

    mate: DataObject | None = None
    if paired:
        if mate_object_id is not None:
            mate = await DataObject.get(mate_object_id)
            if mate is None:
                raise NotFoundError(f"Mate object not found: {mate_object_id}")
        else:
            mate = await suggest_mate(obj)

    if mate is not None:
        if mate.id == obj.id:
            raise ValidationError("A file cannot be its own mate")
        if mate.project_id != obj.project_id:
            raise ValidationError("Paired reads must be in the same project")
        _check_fastq_ready(mate)

        # R1 leads, so the outputs come back in the order fastp was given them
        # and the -i/-I assignment is not left to whichever the user clicked.
        if pairing.mate_of(obj.name) == "R2" and pairing.mate_of(mate.name) == "R1":
            obj, mate = mate, obj

    r1_digest, r1_path = await _resolve_readable(obj)
    params_cls = _TRIM_PARAM_TYPES[tool]
    merged_params = params_cls.from_dict(
        {"threads": settings.pipeline_default_threads, **(params or {})}
    ).as_dict()
    payload: dict = {
        "object_id": str(obj.id),
        "project_id": str(obj.project_id),
        "r1_name": obj.name,
        "tool": tool,
        "params": merged_params,
    }
    if r1_digest:
        payload["r1_sha256"] = r1_digest
    if r1_path:
        payload["r1_path"] = r1_path
    if long_read_advisory:
        payload["long_read_advisory"] = True

    # The read total drives the progress bar. The parser only ever produces an
    # estimate -- extrapolated from the first 1000 records, with
    # read_count_exact False -- which is precisely why TrimProgress caps the
    # bar below 100%. Absent, the bar is indeterminate rather than invented.
    expected = obj.facts.get("read_count_estimate")
    if isinstance(expected, int) and expected > 0:
        payload["expected_reads"] = expected

    if mate is not None:
        r2_digest, r2_path = await _resolve_readable(mate)
        payload["mate_object_id"] = str(mate.id)
        payload["r2_name"] = mate.name
        if r2_digest:
            payload["r2_sha256"] = r2_digest
        if r2_path:
            payload["r2_path"] = r2_path

    # Keyed on the inputs rather than a timestamp, so a double-submit collapses
    # into one run. Re-trimming with different settings is still possible: the
    # params are part of the key.
    dedup_key = "trim:" + ":".join(
        [str(obj.id), str(mate.id) if mate else "-", _params_fingerprint(payload["params"])]
    )

    job = await queue.enqueue(
        "trim_reads",
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(
            cpu=payload["params"]["threads"], mem_mb=2048, io=IoClass.HEAVY
        ),
        max_attempts=2,
        dedup_key=dedup_key,
        project_id=obj.project_id,
        object_id=obj.id,
    )
    if job is None:
        raise ConflictError(
            "An identical trim is already queued or running",
            details={"object_id": str(obj.id)},
        )

    # A trim produces three rows for one action -- the trim plus an ingest per
    # output -- so it has the same grouping problem in miniature.
    run = await run_service.create_run(
        kind=RunKind.TRIM,
        project_id=obj.project_id,
        label=_trim_label(obj, mate),
        inputs=_trim_inputs(obj, mate),
        params=payload["params"],
        tool=tool,
    )
    await run_service.link_job(run.id, job.id, RunJobRole.TRIM)

    log.info(
        "trim_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        object_id=str(obj.id),
        mate_id=str(mate.id) if mate else None,
        threads=payload["params"]["threads"],
    )
    return job


def _trim_label(reads: DataObject, mate: DataObject | None) -> str:
    if mate is None:
        return f"Trim {reads.name}"
    stem = pairing.split_mate(reads.name)
    if stem and stem[0]:
        return f"Trim {stem[0]} (paired)"
    return f"Trim {reads.name} + {mate.name}"


def _trim_inputs(reads: DataObject, mate: DataObject | None) -> list[RunInput]:
    inputs = [RunInput(object_id=reads.id, name=reads.name, role=RunInputRole.READS)]
    if mate is not None:
        inputs.append(RunInput(object_id=mate.id, name=mate.name, role=RunInputRole.MATE))
    return inputs


# SAM platform codes to the SRA platform names run_qc dispatches on. Only the
# long-read pair needs mapping; everything else takes the short-read path by
# default, so listing it would add nothing.
_SAM_TO_SRA_PLATFORM = {"ONT": "OXFORD_NANOPORE", "PACBIO": "PACBIO_SMRT"}


def _qc_platform(obj: DataObject) -> str:
    """Which QC tool family this file's reads call for.

    Goes through `sam_platform` rather than reading `metadata.platform`
    directly, because that field holds an instrument model -- "PromethION",
    "Sequel IIe" -- not a platform name. The substring table that already
    exists for read groups is the thing that knows those models.

    An SRA download stamps `sra_platform` in facts, which is NCBI's own
    spelling and needs no inference; it wins when present.
    """
    recorded = (obj.facts or {}).get("sra_platform")
    if isinstance(recorded, str) and recorded.strip():
        return recorded.strip().upper()

    sam = sam_platform((obj.metadata or {}).get("platform"))
    return _SAM_TO_SRA_PLATFORM.get(sam, "ILLUMINA")


_LONG_READ_QC_PLATFORMS = frozenset({"OXFORD_NANOPORE", "PACBIO_SMRT"})


def is_long_read(obj: DataObject) -> bool:
    """Whether fastp's short-read assumptions are the wrong tool for this file.

    Chemistry, when QC has already inferred it, is the more specific fact --
    it is what actually determines whether the reads are long, not who made
    them (a chemistry of SHORT means QC found a mislabelled short-read file
    even on nominally long-read metadata). Absent or unrecognized chemistry
    falls back to platform, the same way `suggested_preset` does, since most
    files reach the trim dialog before anyone has run QC on them.
    """
    chemistry = read_chemistry(obj)
    if chemistry is not None and chemistry is not align_runner.ReadChemistry.UNKNOWN:
        return chemistry in (
            align_runner.ReadChemistry.HIFI,
            align_runner.ReadChemistry.CLR,
            align_runner.ReadChemistry.ONT_SIMPLEX,
            align_runner.ReadChemistry.ONT_DUPLEX,
        )
    return _qc_platform(obj) in _LONG_READ_QC_PLATFORMS


async def launch_qc(*, object_id: PydanticObjectId):
    """Queue a QC run over a single FASTQ file.

    Read-only: it produces a description of the file rather than a new file,
    so unlike trim there is no output to name and no mate to pair with. A
    paired-end library is two files and gets two QC runs, which is what the
    per-file reports describe anyway.

    No `PipelineRun` is created. A run groups the several jobs one click
    produces; QC is a single job, and a run wrapping one job would add a row to
    the activity view that says nothing the job does not.
    """
    from app.queue import queue

    tools.require(tools.fastp())

    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")
    _check_fastq_ready(obj, verb="QC")

    digest, path = await _resolve_readable(obj)
    payload: dict = {
        "object_id": str(obj.id),
        "project_id": str(obj.project_id),
        "name": obj.name,
        # Chooses the QC tool. Recovered from the file's own metadata so a
        # manual QC on a long-read file reaches NanoPlot exactly as an
        # automatic post-download one does -- the download path passes the
        # resolver's value directly, but a hand-uploaded file only has this.
        "platform": _qc_platform(obj),
    }
    if digest:
        payload["r1_sha256"] = digest
    if path:
        payload["r1_path"] = path

    expected = obj.facts.get("read_count_estimate")
    if isinstance(expected, int) and expected > 0:
        payload["expected_reads"] = expected

    # Keyed on the object alone, with no parameter fingerprint: QC takes no
    # parameters, so a second run over unchanged content would produce an
    # identical report. This is the same key the post-download QC in the SRA
    # path uses, so a manual click and an automatic run collapse into one job.
    job = await queue.enqueue(
        "run_qc",
        payload=payload,
        job_class=JobClass.COMPUTE,
        # Matches the handler's declaration -- see run_qc for why 2048.
        resources=JobResources(cpu=2, mem_mb=2048, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"qc:{obj.id}",
        project_id=obj.project_id,
        object_id=obj.id,
    )
    if job is None:
        raise ConflictError(
            "QC is already queued or running for this file",
            details={"object_id": str(obj.id)},
        )

    log.info("qc_launched", job_id=str(job.id), object_id=str(obj.id))
    return job


# --- Narrative summaries ----------------------------------------------------


def summary_fingerprint(obj: DataObject) -> str:
    """A digest of the inputs a summary would be written from.

    Covers facts and metadata but deliberately excludes the `ai_summary_*` keys
    themselves -- otherwise writing a summary would change the fingerprint that
    describes what it summarized, and every summary would be born stale.
    """
    import hashlib
    import json

    material = {
        "facts": {k: v for k, v in obj.facts.items() if not k.startswith("ai_summary")},
        "metadata": obj.metadata,
        "role": obj.role.value if obj.role else None,
    }
    encoded = json.dumps(material, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


async def launch_summary(
    *,
    object_id: PydanticObjectId,
    force: bool = False,
    job_class: JobClass = JobClass.USER_BACKGROUND,
) -> Job | None:
    """Queue a narrative summary of a file's QC data and metadata.

    Returns None rather than raising when there is nothing to do -- the feature
    is additive, and both of its "no" answers (disabled by configuration, or a
    summary that already covers exactly these numbers) are ordinary outcomes
    rather than errors. The API turns None into a 409 only for the explicit
    user-clicked path, where silence would look like a broken button.

    `force` re-runs even when a current summary exists, which is what the
    regenerate button wants: the numbers are unchanged but the user has asked
    for another attempt, perhaps against a different model.
    """
    from app.queue import queue

    if not settings.llm_summaries_enabled:
        return None

    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")

    # Nothing to describe. Checked here rather than in the handler so an
    # unsummarizable file never becomes a queued job at all.
    if not obj.facts and not obj.metadata:
        return None

    fingerprint = summary_fingerprint(obj)
    if not force and obj.facts.get("ai_summary_fingerprint") == fingerprint:
        return None

    organism = obj.metadata.get("organism")
    payload = {
        "object_id": str(obj.id),
        "project_id": str(obj.project_id),
        "name": obj.name,
        "format_kind": obj.format.kind.value,
        "role": obj.role.value if obj.role else None,
        "organism": organism.strip() if isinstance(organism, str) and organism.strip() else None,
        # Sent whole: the handler runs in a thread and cannot read the database,
        # and the prompt builder is what decides which of these keys matter.
        "facts": {k: v for k, v in obj.facts.items() if not k.startswith("ai_summary")},
        "metadata": obj.metadata,
        "facts_fingerprint": fingerprint,
    }

    # Keyed on the inputs, not just the object: a summary of unchanged numbers
    # is the same job, while a post-QC re-summary has a different key and is
    # allowed to queue alongside. `force` adds a nonce so an explicit re-run is
    # never deduplicated away against its own prior result.
    dedup = f"summary:{obj.id}:{fingerprint}"
    if force:
        from uuid import uuid4

        dedup = f"{dedup}:{uuid4().hex[:8]}"

    job = await queue.enqueue(
        "summarize_object",
        payload=payload,
        job_class=job_class,
        resources=JobResources(cpu=0, mem_mb=64, io=IoClass.LIGHT),
        max_attempts=2,
        dedup_key=dedup,
        project_id=obj.project_id,
        object_id=obj.id,
    )
    if job is not None:
        log.info("summary_launched", job_id=str(job.id), object_id=str(obj.id))
    return job


def _params_fingerprint(params: dict) -> str:
    """A short stable digest of the trim settings, for the dedup key."""
    import hashlib

    encoded = "|".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha256(encoded.encode()).hexdigest()[:12]


# --- Alignment --------------------------------------------------------------

ALIGNABLE_KINDS = {FormatKind.FASTQ}
REFERENCE_KINDS = {FormatKind.FASTA}

# The metadata vocabulary is human-facing; SAM's PL field has its own
# controlled vocabulary, and a value outside it makes downstream callers behave
# inconsistently -- GATK warns and some tools silently treat the platform as
# unknown. Mapped rather than passed through for that reason.
#
# Matched on *substrings* rather than by exact label, because the values that
# actually land in `metadata.platform` are instrument models, not dropdown
# entries: SRA enrichment writes INSTRUMENT_MODEL, so a real file says
# "NextSeq 550" and never "Illumina NextSeq". An exact-match table read every
# such file as OTHER.
#
# Ordered, and specific before general: "DNBSEQ" has to be tested before the
# bare "seq" family names it contains, and "Illumina" last so a model name that
# happens to mention the vendor does not outrank its own instrument family.
_SAM_PLATFORM_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("nanopore", "minion", "gridion", "promethion", "flongle"), "ONT"),
    (("pacbio", "sequel", "revio", "rs ii"), "PACBIO"),
    (("dnbseq", "mgiseq", "bgiseq"), "BGI"),
    (("ion torrent", "ion proton", "ion s5", "ion gene"), "IONTORRENT"),
    (("454 gs", "gs flx", "gs junior"), "LS454"),
    (("solid",), "SOLID"),
    (("helicos",), "HELICOS"),
    (("element", "aviti"), "ELEMENT"),
    (("ultima",), "ULTIMA"),
    (("singular", "g4"), "SINGULAR"),
    (
        (
            "illumina", "novaseq", "nextseq", "miseq", "hiseq", "miniseq",
            "iseq", "genome analyzer", "nova x",
        ),
        "ILLUMINA",
    ),
)

# Which preset suits a platform's reads. The wrong one produces silently poor
# alignments rather than an error, so this is a real default rather than a
# convenience.
_PLATFORM_PRESETS: dict[str, str] = {
    "ONT": align_runner.Preset.MAP_ONT,
    "PACBIO": align_runner.Preset.MAP_PB,
}


def sam_platform(metadata_platform: str | None) -> str:
    """A SAM `PL` value from a platform label or instrument model.

    Falls back to ILLUMINA when nothing is recorded -- the overwhelmingly
    common case here, and a wrong guess is visible in the BAM header rather
    than silent. An unrecognized *non-empty* value becomes OTHER, which is in
    the SAM vocabulary; passing the raw label through would not be.
    """
    if not metadata_platform:
        return "ILLUMINA"

    text = metadata_platform.strip().lower()
    for needles, sam_value in _SAM_PLATFORM_PATTERNS:
        if any(needle in text for needle in needles):
            return sam_value
    return "OTHER"


def suggested_preset(
    sam_pl: str, *, chemistry: align_runner.ReadChemistry | None = None
) -> str:
    """The minimap2 preset matching a platform, defaulting to short-read.

    Chemistry, when known, wins over the platform default: it is the axis
    that actually determines read accuracy (HiFi vs. CLR are both PACBIO),
    while platform only says who made the file. UNKNOWN chemistry is treated
    the same as no chemistry at all -- map-pb stays the PacBio fallback,
    since running HiFi parameters on genuinely noisy CLR reads loses far more
    than running CLR parameters on HiFi loses.
    """
    if chemistry is not None and chemistry is not align_runner.ReadChemistry.UNKNOWN:
        return align_runner.preset_for_chemistry(chemistry)
    return _PLATFORM_PRESETS.get(sam_pl, align_runner.Preset.SHORT_READ)


def default_read_group(obj: DataObject) -> dict:
    """Read-group fields defaulted from the reads' own metadata.

    `sample_id` and `library_prep` are already in the schema, so this is
    usually a confirmation rather than data entry. Falling back to the filename
    for the sample keeps the dialog answerable for a file nobody has annotated
    -- a placeholder the user can see and correct beats an empty required field.
    """
    metadata = obj.metadata or {}
    sample = metadata.get("sample_id") or Path(obj.name).name.split(".")[0]
    return {
        "sample": str(sample),
        "library": default_library(metadata, sample=str(sample)),
        "platform": sam_platform(metadata.get("platform")),
    }


def default_library(metadata: dict, *, sample: str) -> str:
    """The @RG `LB` value to offer, best available first.

    An explicit library prep or id wins. Failing that the sequencing platform
    stands in: reads off one instrument are normally one library, so the
    instrument is a better guess than the sample name -- and it is the value
    that distinguishes two libraries of the same sample, which is exactly what
    LB exists to record.

    The human-readable instrument is used rather than the SAM `PL` tag, since
    "NextSeq 550" identifies a library run far better than "ILLUMINA" does.

    The sample remains the last resort: LB is required, and a duplicate of the
    sample name is at least honest about carrying no extra information.
    """
    for key in ("library_prep", "library_id"):
        value = metadata.get(key)
        if value:
            return str(value)

    platform = metadata.get("platform")
    if platform and str(platform).strip():
        return str(platform).strip()

    return sample


def read_chemistry(obj: DataObject | None) -> align_runner.ReadChemistry | None:
    """The chemistry QC already inferred, read from facts rather than
    recomputed -- QC runs before alignment, so the fact is known by the time
    the align dialog opens. Facts are tool-written data, not a validated
    enum, so an unrecognized or stale value degrades to None (the platform
    default) rather than raising in the middle of building a dialog."""
    if obj is None:
        return None
    value = (obj.facts or {}).get("qc_read_chemistry")
    if not value:
        return None
    try:
        return align_runner.ReadChemistry(value)
    except ValueError:
        return None


async def read_chemistry_for_alignment(
    obj: DataObject | None,
) -> align_runner.ReadChemistry | None:
    """The chemistry of the reads behind an alignment.

    Prefers the BAM's own copy, which `align_provenance` stamps on at ingest.
    Falls back to the FASTQ the BAM descends from: alignments produced before
    that copy existed carry no chemistry of their own, and re-aligning a BAM
    purely to learn how accurate its reads were would be absurd.

    Returns None when nothing knows -- QC may simply never have run. Callers
    treat that as "unknown" and fall back to the conservative short-read
    default rather than guessing.
    """
    if obj is None:
        return None

    chemistry = read_chemistry(obj)
    if chemistry is not None:
        return chemistry

    for parent_id in obj.derived_from:
        parent = await DataObject.get(parent_id)
        if parent is None or parent.format.kind is not FormatKind.FASTQ:
            continue
        chemistry = read_chemistry(parent)
        if chemistry is not None:
            return chemistry
    return None


def default_align_params(obj: DataObject | None = None) -> dict:
    """Server-owned alignment defaults, so the form does not encode its own.

    The aligner defaults to whichever is actually usable: bwa-mem2 is x86-64
    only, so on an arm64 host minimap2 is not merely preferred but the only
    option, and offering a default that cannot run would be a dialog the user
    has to fail at before learning that.
    """
    bwa = tools.bwa_mem2()
    aligner = Aligner.BWA_MEM2 if bwa.available else Aligner.MINIMAP2

    platform = sam_platform((obj.metadata or {}).get("platform")) if obj else "ILLUMINA"
    preset = suggested_preset(platform, chemistry=read_chemistry(obj))

    # Long reads are minimap2's domain regardless of what else is installed:
    # bwa-mem2 is a short-read aligner and would produce poor alignments.
    if preset != align_runner.Preset.SHORT_READ:
        aligner = Aligner.MINIMAP2

    return align_runner.AlignParams(
        aligner=aligner,
        preset=preset if aligner is Aligner.MINIMAP2 else "",
        threads=settings.pipeline_default_threads,
        sort_memory_mb=settings.samtools_sort_mem_mb,
    ).as_dict()


def _check_alignable(obj: DataObject) -> None:
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready to align (status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind not in ALIGNABLE_KINDS:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not FASTQ reads",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )


def _check_reference(obj: DataObject) -> None:
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready to use as a reference "
            f"(status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind not in REFERENCE_KINDS:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not a FASTA reference",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )


async def reference_index_status(reference: DataObject) -> dict:
    """Which indexes a reference already has.

    Keyed by content: sidecars attach to the reference object, and the same
    genome registered twice shares one index because the blob is shared. That
    falls out of content addressing rather than being designed in.
    """
    from app.services import object_service

    sidecars = await object_service.list_sidecars(reference.id)
    have = {s.sidecar_role for s in sidecars if s.sidecar_role}
    return {
        aligner.value: aligners.INDEX_ROLE[aligner] in have for aligner in Aligner
    } | {"fai": SidecarRole.FAI in have}


async def align_envelope(
    *, object_id: PydanticObjectId, reference_id: PydanticObjectId
) -> dict:
    """Host budgets, input sizes, and the per-aligner memory coefficients.

    Budgets come from the governor, which reads cgroup limits -- so inside
    Docker this reports the container's real allocation rather than the
    host's. That distinction is the whole reason the warning is trustworthy:
    a machine with 64 GB and an 8 GB Docker allocation will OOM at 8.
    """
    from dataclasses import asdict

    from app.queue.governor import LoadGovernor

    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")

    reference = await DataObject.get(reference_id)
    if reference is None:
        raise NotFoundError(f"Reference not found: {reference_id}")

    governor = LoadGovernor()

    # Reference size in bases, approximated by file size. A FASTA carries
    # about one byte per base plus headers and newlines, so this overestimates
    # by a few percent -- the right direction for a memory warning.
    reference_bases = reference.size or 0

    status = await reference_index_status(reference)

    return {
        "cpu_budget": governor.cpu_budget(),
        "mem_budget_mb": int(governor.mem_budget_bytes() / (1024 * 1024)),
        "reference_bases": reference_bases,
        "input_bytes": obj.size or 0,
        "index_status": status,
        "models": {
            aligner.value: asdict(
                aligner_registry.spec_for(aligner).memory_model
            )
            for aligner in Aligner
        },
    }


async def sidecar_payload(reference: DataObject, aligner: Aligner) -> dict:
    """{stored name: blob path} for the sidecars an alignment needs.

    Includes the `.fai` alongside the aligner's own index: samtools wants it
    beside the reference, and materializing one without the other produces a
    workdir that looks right and fails partway through.
    """
    from app.services import object_service

    wanted = {aligners.INDEX_ROLE[aligner], SidecarRole.FAI}
    payload: dict = {}
    for sidecar in await object_service.list_sidecars(reference.id):
        if sidecar.sidecar_role not in wanted or not sidecar.blob_sha256:
            continue
        digest, path = await _resolve_readable(sidecar)
        payload[sidecar.name] = path or str(blob_path(digest))
    return payload


async def launch_build_index(
    *, reference_id: PydanticObjectId, aligner: str | Aligner = Aligner.MINIMAP2
):
    """Queue an index build for one (reference, aligner) pair.

    The eager entry point behind the explorer's **Build index** button. The
    same job type the alignment path queues, so there is no second code path to
    keep correct.
    """
    aligner = Aligner(aligner)
    tools.require(_aligner_tool(aligner))
    tools.require(tools.samtools())

    reference = await DataObject.get(reference_id)
    if reference is None:
        raise NotFoundError(f"Reference not found: {reference_id}")
    _check_reference(reference)

    job = await _enqueue_build_index(reference, aligner)
    if job is None:
        raise ConflictError(
            "An index for this reference is already being built",
            details={"reference_id": str(reference.id), "aligner": aligner.value},
        )
    return job


async def _enqueue_build_index(reference: DataObject, aligner: Aligner):
    """Queue the index build, deduplicated on (reference blob, aligner)."""
    from app.queue import queue

    digest, path = await _resolve_readable(reference)
    payload: dict = {
        "reference_object_id": str(reference.id),
        "project_id": str(reference.project_id),
        "reference_name": reference.name,
        "aligner": aligner.value,
    }
    if digest:
        payload["reference_sha256"] = digest
    if path:
        payload["reference_path"] = path

    # Keyed on the blob rather than the object: the same genome registered in
    # two projects is one index, with no cross-project bookkeeping.
    dedup_key = f"build_index:{digest or path}:{aligner.value}"

    return await queue.enqueue(
        "build_index",
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=dedup_key,
        project_id=reference.project_id,
        object_id=reference.id,
    )


def _aligner_tool(aligner: Aligner):
    """The probe for one aligner.

    A registry lookup rather than an if/else: the old form returned minimap2
    for anything that was not bwa-mem2, so a new aligner would silently run
    the wrong binary against the right index.
    """
    return aligner_registry.spec_for(aligner).tool()


def active_index_job_query(reference_id: PydanticObjectId) -> dict:
    """The in-flight index build for a reference, if there is one.

    A raw Mongo query rather than Beanie's field expressions. `Job.state` is
    not resolvable as an attribute outside a query context and has no `.in_()`,
    so the expression form raised on every call -- and this branch only runs
    when two alignments race for one index, so it shipped broken and stayed
    that way until a test forced the race.

    Extracted and named so the query shape is assertable without a database.
    """
    return {
        "type": "build_index",
        "state": {"$in": [s.value for s in ACTIVE_STATES]},
        "object_id": reference_id,
    }


async def launch_alignment(
    *,
    object_id: PydanticObjectId,
    reference_id: PydanticObjectId,
    mate_object_id: PydanticObjectId | None = None,
    read_group: dict | None = None,
    params: dict | None = None,
    paired: bool = True,
):
    """Queue an alignment, building the reference index first if it is missing.

    An alignment against an unindexed reference enqueues `build_index` and then
    the alignment *behind it*, using the queue's dependency gate rather than a
    delay: the alignment waits on the index job's completion, so a failed index
    fails the alignment with a comprehensible reason instead of leaving it
    queued forever.
    """
    from app.queue import queue

    merged_params = {**default_align_params(), **(params or {})}
    align_params = align_params_module.from_dict(merged_params)
    aligner = align_params.aligner
    tools.require(_aligner_tool(aligner))
    tools.require(tools.samtools())

    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")
    _check_alignable(obj)

    reference = await DataObject.get(reference_id)
    if reference is None:
        raise NotFoundError(f"Reference not found: {reference_id}")
    _check_reference(reference)
    if reference.project_id != obj.project_id:
        raise ValidationError("Reads and reference must be in the same project")

    # The authoritative check. The dialog runs the same arithmetic for
    # immediacy, but it can be bypassed -- the API is directly callable -- and
    # its envelope goes stale if the host's load changes between opening the
    # dialog and pressing Launch.
    from app.pipelines import resource_estimator
    from app.queue.governor import LoadGovernor

    governor = LoadGovernor()
    mem_budget_mb = int(governor.mem_budget_bytes() / (1024 * 1024))
    index_status = await reference_index_status(reference)
    building = not index_status.get(aligner.value) or not index_status.get("fai")

    estimate = resource_estimator.estimate_mb(
        aligner=aligner,
        reference_bases=reference.size or 0,
        threads=align_params.threads,
        sort_memory_mb=align_params.sort_memory_mb,
        building_index=building,
    )
    band = resource_estimator.classify(
        estimated_mb=estimate,
        mem_budget_mb=mem_budget_mb,
        threads=align_params.threads,
        cpu_budget=governor.cpu_budget(),
    )
    if band is resource_estimator.Band.BLOCK:
        raise ValidationError(
            resource_estimator.explain(
                aligner=aligner,
                reference_bases=reference.size or 0,
                threads=align_params.threads,
                sort_memory_mb=align_params.sort_memory_mb,
                building_index=building,
                mem_budget_mb=mem_budget_mb,
            ),
            details={"estimate_mb": estimate, "budget_mb": mem_budget_mb},
        )

    mate: DataObject | None = None
    if paired:
        mate = (
            await DataObject.get(mate_object_id)
            if mate_object_id is not None
            else await suggest_mate(obj)
        )
        if mate_object_id is not None and mate is None:
            raise NotFoundError(f"Mate object not found: {mate_object_id}")

    if mate is not None:
        if mate.id == obj.id:
            raise ValidationError("A file cannot be its own mate")
        if mate.project_id != obj.project_id:
            raise ValidationError("Paired reads must be in the same project")
        _check_alignable(mate)
        # R1 leads, so the mates reach the aligner in the order it expects.
        if pairing.mate_of(obj.name) == "R2" and pairing.mate_of(mate.name) == "R1":
            obj, mate = mate, obj

    rg = align_runner.ReadGroup.from_dict(
        {**default_read_group(obj), **(read_group or {})}
    )

    # The record of what was asked for, created before anything is enqueued so
    # every job the launch produces can be linked to it as it is created.
    run = await run_service.create_run(
        kind=RunKind.ALIGNMENT,
        project_id=obj.project_id,
        label=_alignment_label(obj, mate, reference),
        inputs=_alignment_inputs(obj, mate, reference),
        params={**align_params.as_dict(), "read_group": rg.as_dict()},
    )

    # Build the index first if it is missing, and hold the alignment behind it.
    # `building` was already computed above for the resource guard -- same
    # underlying sidecar lookup, so reused here rather than queried twice.
    needs_index = building
    depends_on = []
    index_job = None
    if needs_index:
        index_job = await _enqueue_build_index(reference, aligner)
        if index_job is not None:
            depends_on.append(index_job.id)
            await run_service.link_job(run.id, index_job.id, RunJobRole.INDEX)
        else:
            # Deduplicated away: an identical build is already queued or
            # running. Wait on *that* job rather than racing it.
            existing = await Job.find_one(active_index_job_query(reference.id))
            if existing is not None:
                depends_on.append(existing.id)
                # Linked as shared: this run depends on the build but did not
                # cause it. Showing it as reused beats omitting it (a gap where
                # the index came from) or claiming credit for another run's work.
                await run_service.link_job(
                    run.id, existing.id, RunJobRole.INDEX, shared=True
                )

    r1_digest, r1_path = await _resolve_readable(obj)
    payload: dict = {
        "object_id": str(obj.id),
        "project_id": str(obj.project_id),
        "reference_object_id": str(reference.id),
        "reference_name": reference.name,
        "r1_name": obj.name,
        "aligner": aligner.value,
        "params": align_params.as_dict(),
        "read_group": rg.as_dict(),
        "output_name": _bam_name(obj.name, rg.sample),
    }
    ref_digest, ref_path = await _resolve_readable(reference)
    if ref_digest:
        payload["reference_sha256"] = ref_digest
    if ref_path:
        payload["reference_path"] = ref_path
    if r1_digest:
        payload["r1_sha256"] = r1_digest
    if r1_path:
        payload["r1_path"] = r1_path

    expected = obj.facts.get("read_count_estimate")
    if isinstance(expected, int) and expected > 0:
        payload["expected_reads"] = expected

    if mate is not None:
        r2_digest, r2_path = await _resolve_readable(mate)
        payload["mate_object_id"] = str(mate.id)
        payload["r2_name"] = mate.name
        if r2_digest:
            payload["r2_sha256"] = r2_digest
        if r2_path:
            payload["r2_path"] = r2_path

    # Sidecars are resolved at launch when they already exist. When the index
    # is being built in this same request they do not exist yet, so the handler
    # re-resolves them -- see `align_reads`, which fails loudly rather than
    # aligning against a reference whose index never materialized.
    if not needs_index:
        payload["sidecars"] = await sidecar_payload(reference, aligner)

    dedup_key = "align:" + ":".join(
        [
            str(obj.id),
            str(mate.id) if mate else "-",
            str(reference.id),
            _params_fingerprint(payload["params"]),
        ]
    )

    job = await queue.enqueue(
        "align_reads",
        payload=payload,
        job_class=JobClass.COMPUTE,
        # The user's thread count, exactly as trim_reads declares it.
        resources=JobResources(
            cpu=align_params.threads, mem_mb=8192, io=IoClass.HEAVY
        ),
        max_attempts=2,
        dedup_key=dedup_key,
        project_id=obj.project_id,
        object_id=obj.id,
        depends_on=depends_on,
    )
    if job is None:
        # The run describes work that will not happen, so it must not linger in
        # the activity view claiming otherwise. The index build it may have
        # queued is left alone: that work is real and the earlier run owns it.
        await run_service.discard_run(run.id)
        raise ConflictError(
            "An identical alignment is already queued or running",
            details={"object_id": str(obj.id)},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.ALIGN)

    log.info(
        "align_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        object_id=str(obj.id),
        reference_id=str(reference.id),
        aligner=aligner.value,
        index_job_id=str(index_job.id) if index_job else None,
        waiting_on=[str(d) for d in depends_on],
    )
    return job


def _alignment_label(
    reads: DataObject, mate: DataObject | None, reference: DataObject
) -> str:
    """A one-line description of what this run does.

    Built at launch, when every part is known and present, and stored rather
    than derived so it survives the deletion of its inputs.
    """
    left = reads.name
    if mate is not None:
        # The pair reads as one input, which is what it is to the aligner.
        stem = pairing.split_mate(reads.name)
        left = f"{stem[0]} (paired)" if stem and stem[0] else f"{reads.name} + {mate.name}"
    return f"{left} → {reference.name}"


def _alignment_inputs(
    reads: DataObject, mate: DataObject | None, reference: DataObject
) -> list[RunInput]:
    inputs = [RunInput(object_id=reads.id, name=reads.name, role=RunInputRole.READS)]
    if mate is not None:
        inputs.append(RunInput(object_id=mate.id, name=mate.name, role=RunInputRole.MATE))
    inputs.append(
        RunInput(
            object_id=reference.id, name=reference.name, role=RunInputRole.REFERENCE
        )
    )
    return inputs


def _bam_name(reads_name: str, sample: str) -> str:
    """A BAM filename derived from the reads it came from.

    The reads' stem is kept rather than the sample name alone, so two libraries
    from one sample do not produce two files with the same name.
    """
    stem = Path(reads_name).name
    for suffix in (".gz", ".bz2", ".zst"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    stem = Path(stem).stem
    stem = pairing.split_mate(stem)[0] if pairing.split_mate(stem) else stem
    return f"{stem or sample}.bam"


# --- Variant calling --------------------------------------------------------

VARIANT_CALLABLE_KINDS = {FormatKind.BAM}


def _check_variant_callable(obj: DataObject) -> None:
    """Whether a file can have variants called from it.

    Both callers read an indexed alignment; the index is checked separately at
    launch because it is a fixable condition ("run index_bam first") rather
    than a wrong-file-type one.
    """
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready for variant calling "
            f"(status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind not in VARIANT_CALLABLE_KINDS:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not a BAM alignment",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )


BAM_STATS_CALLABLE_KINDS = {FormatKind.BAM}


def _check_bam_stats_callable(obj: DataObject) -> None:
    """Whether a BAM is eligible for the Results job.

    Coordinate sort is checked here rather than left to samtools to fail on,
    because `coverage`/`idxstats` on an unsorted BAM do not error -- they
    quietly produce numbers that look plausible and are wrong. Missing index
    is checked separately by the caller (see launch_bam_stats), matching how
    launch_variant_calling separates "wrong file type" from "fixable
    precondition".
    """
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready for results (status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind not in BAM_STATS_CALLABLE_KINDS:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not a BAM alignment",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )
    if obj.facts.get("sort_order") != "coordinate":
        raise ValidationError(
            f"{obj.name!r} is not coordinate-sorted. Coverage statistics "
            f"require a coordinate-sorted BAM.",
            details={"object_id": str(obj.id)},
        )


async def launch_bam_stats(*, object_id: PydanticObjectId):
    """Queue the Results computation for a BAM: coverage, idxstats-derived
    per-contig counts, and binned depth across the reference.

    Read-only, like QC: no derived objects, just facts merged onto the object
    plus one TSV report on disk. Requires a coordinate-sorted BAM, checked
    here rather than left for samtools to fail confusingly on. Unlike
    launch_variant_calling, a missing `.bai` is *not* refused here: the
    Results tab has no Align button and the Metadata tab has no standalone
    index action, so there is nowhere else for the user to ask for one. This
    queues `index_bam` first and chains straight into `run_bam_stats` when it
    finishes -- see `_apply_index_bam` in queue/results.py.
    """
    from app.queue import queue

    tools.require(tools.samtools())

    bam = await DataObject.get(object_id)
    if bam is None:
        raise NotFoundError(f"Object not found: {object_id}")
    _check_bam_stats_callable(bam)

    bai = await _sidecar_of_role(bam, SidecarRole.BAI)
    if bai is None:
        if not bam.blob_sha256:
            raise ValidationError(
                f"{bam.name!r} has no stored content yet (status={bam.status.value})"
            )
        index_job = await queue.enqueue(
            "index_bam",
            payload={
                "bam_object_id": str(bam.id),
                "bam_sha256": bam.blob_sha256,
                "bam_name": bam.name,
                "project_id": str(bam.project_id),
                "then_bam_stats": True,
            },
            job_class=JobClass.COMPUTE,
            resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.LIGHT),
            max_attempts=2,
            dedup_key=f"index_bam:{bam.blob_sha256}",
            project_id=bam.project_id,
            object_id=bam.id,
        )
        if index_job is None:
            raise ConflictError(
                "This BAM is already being indexed. Wait for it to finish, "
                "then compute results.",
                details={"object_id": str(bam.id)},
            )
        return index_job

    digest, path = await _resolve_readable(bam)
    bai_digest, bai_path = await _resolve_readable(bai)

    payload: dict = {
        "object_id": str(bam.id),
        "project_id": str(bam.project_id),
        "bam_name": bam.name,
    }
    if digest:
        payload["bam_sha256"] = digest
    if path:
        payload["bam_path"] = path
    if bai_digest:
        payload["bai_sha256"] = bai_digest
    if bai_path:
        payload["bai_path"] = bai_path

    # No parameters, like QC: a repeat over unchanged content is the same
    # result, so the object id alone is the dedup key.
    job = await queue.enqueue(
        "run_bam_stats",
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"bamstats:{bam.id}",
        project_id=bam.project_id,
        object_id=bam.id,
    )
    if job is None:
        raise ConflictError(
            "Results are already queued or running for this file",
            details={"object_id": str(bam.id)},
        )
    return job


VCF_STATS_CALLABLE_KINDS = {FormatKind.VCF, FormatKind.BCF}


def _check_vcf_stats_callable(obj) -> None:
    """Whether a Variant Results computation can run against this object.

    Unlike the BAM path there is no index precondition: `bcftools stats` and
    `bcftools query` both stream the whole file, so a `.tbi` is never
    required and there is nothing to chain an index build onto.
    """
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready for results (status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind not in VCF_STATS_CALLABLE_KINDS:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not a VCF or BCF",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )


async def launch_vcf_stats(*, object_id: PydanticObjectId):
    """Queue the Results computation for a VCF: call-set summary statistics
    and the per-variant table.

    Read-only, like launch_bam_stats: no derived objects, just facts merged
    onto the object plus a TSV and a SQLite database on disk.
    """
    from app.queue import queue

    tools.require(tools.bcftools())

    vcf = await DataObject.get(object_id)
    if vcf is None:
        raise NotFoundError(f"Object not found: {object_id}")
    _check_vcf_stats_callable(vcf)

    digest, path = await _resolve_readable(vcf)

    # Contig names and lengths come from the facts the ingest parser already
    # wrote: the handler runs in a worker thread and cannot query for them.
    lengths = vcf.facts.get("reference_lengths") or {}
    contig_lengths = [[name, length] for name, length in lengths.items()]

    payload: dict = {
        "object_id": str(vcf.id),
        "project_id": str(vcf.project_id),
        "vcf_name": vcf.name,
        "contig_lengths": contig_lengths,
    }
    if digest:
        payload["vcf_sha256"] = digest
    if path:
        payload["vcf_path"] = path

    return await queue.enqueue(
        "run_vcf_stats",
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"vcf_stats:{vcf.id}",
        project_id=vcf.project_id,
        object_id=vcf.id,
    )


def _variant_dedup_key(*, bam_id, params: dict) -> str:
    """Identity of a variant calling request.

    Includes the caller: calling one BAM with Clair3 and with bcftools is two
    results worth comparing, not a double-submit to collapse.
    """
    return f"call_variants:{bam_id}:{_params_fingerprint(params)}"


async def reference_for_bam(bam: DataObject) -> DataObject | None:
    """The reference this BAM was aligned against, from its provenance.

    An alignment records the reference in `derived_from`, so this is a lookup
    rather than a guess. Prefers an explicit reference role over bare FASTA
    format, since a BAM's parents may include more than one FASTA in unusual
    setups. Returns None for an uploaded BAM, which has no provenance at all --
    the caller asks the user instead.
    """
    fallback: DataObject | None = None
    for parent_id in bam.derived_from:
        parent = await DataObject.get(parent_id)
        if parent is None or parent.format.kind is not FormatKind.FASTA:
            continue
        if parent.role is ObjectRole.REFERENCE:
            return parent
        fallback = fallback or parent
    return fallback


async def default_variant_params(obj: DataObject | None = None) -> dict:
    """Server-owned variant calling defaults, so the form does not encode its own.

    Async because the chemistry may have to be read from the reads the BAM
    descends from -- see `read_chemistry_for_alignment`.
    """
    chemistry = await read_chemistry_for_alignment(obj)
    if chemistry is align_runner.ReadChemistry.CLR:
        # Do not raise while building a dialog: the UI needs to *render* the
        # refusal, not fail to open. The launch path is what actually blocks.
        return {"caller": None, "threads": settings.pipeline_default_threads}

    caller = variant_runner.caller_for_chemistry(chemistry or align_runner.ReadChemistry.UNKNOWN)
    return variant_runner.VariantParams(
        caller=caller, threads=settings.pipeline_default_threads
    ).as_dict()


async def _sidecar_of_role(obj: DataObject, role: SidecarRole) -> DataObject | None:
    from app.services import object_service

    for sidecar in await object_service.list_sidecars(obj.id):
        if sidecar.sidecar_role is role and sidecar.blob_sha256:
            return sidecar
    return None


async def launch_variant_calling(
    *,
    bam_id: PydanticObjectId,
    reference_id: PydanticObjectId | None = None,
    caller: str | None = None,
    params: dict | None = None,
):
    """Queue a variant calling run over an aligned BAM.

    Unlike alignment, this does not build its missing indexes: it requires the
    `.bai` and the reference `.fai` to exist and refuses otherwise. Both are
    produced by jobs the user has already run (`index_bam`, `build_index`), and
    an actionable "run index_bam first" beats a job that sits blocked behind
    work the user did not ask for.
    """
    from app.queue import queue

    bam = await DataObject.get(bam_id)
    if bam is None:
        raise NotFoundError(f"BAM not found: {bam_id}")
    _check_variant_callable(bam)

    # Refuse CLR before anything is enqueued. Raises ValidationError naming the
    # alternative; the dialog renders it rather than offering a caller.
    chemistry = await read_chemistry_for_alignment(bam)
    if chemistry is not None:
        variant_runner.caller_for_chemistry(chemistry)

    reference = await _resolve_variant_reference(bam, reference_id)

    bai = await _sidecar_of_role(bam, SidecarRole.BAI)
    if bai is None:
        raise ValidationError(
            f"{bam.name!r} has no BAM index (.bai). Index it first.",
            details={"bam_id": str(bam.id), "needs": "index_bam"},
        )

    fai = await _sidecar_of_role(reference, SidecarRole.FAI)
    if fai is None:
        raise ValidationError(
            f"Reference {reference.name!r} has no FASTA index (.fai). "
            f"Build its index first.",
            details={"reference_id": str(reference.id), "needs": "build_index"},
        )

    merged = variant_runner.VariantParams.from_dict(
        {
            **(await default_variant_params(bam)),
            **({"caller": caller} if caller else {}),
            **(params or {}),
        }
    )
    if merged.caller is variant_runner.VariantCaller.DEEPVARIANT:
        raise ValidationError(
            "DeepVariant is not available in this installation: it has no "
            "arm64 Linux build. Use Clair3 for long reads, or bcftools for "
            "short reads."
        )
    tools.require(
        tools.clair3()
        if merged.caller is variant_runner.VariantCaller.CLAIR3
        else tools.bcftools()
    )
    tools.require(tools.bcftools())  # always: it writes the .tbi

    payload = await _variant_payload(
        bam=bam,
        reference=reference,
        bai=bai,
        fai=fai,
        chemistry=chemistry,
        params=merged,
    )

    run = await run_service.create_run(
        kind=RunKind.VARIANT_CALLING,
        project_id=bam.project_id,
        label=f"{bam.name} → variants ({merged.caller.value})",
        inputs=[
            RunInput(object_id=bam.id, name=bam.name, role=RunInputRole.READS),
            RunInput(
                object_id=reference.id,
                name=reference.name,
                role=RunInputRole.REFERENCE,
            ),
        ],
        params=merged.as_dict(),
        tool=merged.caller.value,
    )

    job = await queue.enqueue(
        "call_variants",
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=merged.threads, mem_mb=8192, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=_variant_dedup_key(bam_id=bam.id, params=merged.as_dict()),
        project_id=bam.project_id,
        object_id=bam.id,
        # No depends_on: the .bai and .fai are required above, so there is
        # nothing left to wait for.
    )
    if job is None:
        await run_service.discard_run(run.id)
        raise ConflictError(
            "An identical variant calling run is already queued or running",
            details={"bam_id": str(bam.id), "caller": merged.caller.value},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.CALL_VARIANTS)
    log.info(
        "variant_calling_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        bam_id=str(bam.id),
        caller=merged.caller.value,
    )
    return job


async def _resolve_variant_reference(
    bam: DataObject, reference_id: PydanticObjectId | None
) -> DataObject:
    """The reference to call against: explicit if given, else the BAM's own."""
    if reference_id is not None:
        reference = await DataObject.get(reference_id)
        if reference is None:
            raise NotFoundError(f"Reference not found: {reference_id}")
        _check_reference(reference)
        if reference.project_id != bam.project_id:
            raise ValidationError("BAM and reference must be in the same project")
        return reference

    reference = await reference_for_bam(bam)
    if reference is None:
        raise ValidationError(
            f"Cannot determine which reference {bam.name!r} was aligned "
            f"against. Choose one -- an uploaded BAM carries no record of it.",
            details={"bam_id": str(bam.id), "needs": "reference_id"},
        )
    return reference


async def _variant_payload(
    *,
    bam: DataObject,
    reference: DataObject,
    bai: DataObject,
    fai: DataObject,
    chemistry,
    params,
) -> dict:
    """The call_variants payload, with every input addressed by digest or path."""
    payload: dict = {
        "bam_object_id": str(bam.id),
        "reference_object_id": str(reference.id),
        "project_id": str(bam.project_id),
        "bam_name": bam.name,
        "reference_name": reference.name,
        "caller": params.caller.value,
        "params": params.as_dict(),
        "output_name": variant_runner.output_name(bam.name, params.caller.value),
    }

    for key, obj in (
        ("bam", bam),
        ("reference", reference),
        ("bai", bai),
        ("fai", fai),
    ):
        digest, path = await _resolve_readable(obj)
        if digest:
            payload[f"{key}_sha256"] = digest
        if path:
            payload[f"{key}_path"] = path

    if chemistry is not None:
        payload["chemistry"] = chemistry.value

    if params.caller is variant_runner.VariantCaller.CLAIR3:
        payload["clair3_params"] = variant_runner.Clair3Params(
            threads=params.threads,
            platform=variant_runner.clair3_platform_for_chemistry(chemistry),
        ).as_dict()
    else:
        payload["bcftools_params"] = variant_runner.BcftoolsParams(
            threads=params.threads
        ).as_dict()

    return payload


# --- Consequence annotation --------------------------------------------------

# ObjectRole.ANNOTATION spans GFF3, GTF and BED -- the role records intent
# and cannot say which interval format a file actually is. `bcftools csq`
# reads GFF3 only, so a BED blacklist tagged ANNOTATION would launch a job
# that dies in the worker on a parse error.
ANNOTATION_KINDS = {FormatKind.GFF}


@dataclass(frozen=True)
class AnnotationInputs:
    """What a VCF needs to be annotated, or why it cannot be.

    One result type for both the Actions card and the launcher. They asked the
    same question separately once before -- the align card and
    `resolve_reference` -- and disagreed about which references counted, which
    is how a project with one usable genome ended up refusing to align beside
    a card saying it could.
    """

    ok: bool
    reference: DataObject | None = None
    annotation: DataObject | None = None
    reason: str | None = None


async def resolve_annotation_inputs(vcf: DataObject) -> AnnotationInputs:
    """Find the reference and GFF3 for a VCF, or say what is missing.

    The reason text names the missing input and, where there is one, the
    action: "no annotation, download it from NCBI" is something a user can do,
    "cannot annotate" is not. All three projects on this machine are blocked on
    a different one of these, so every branch is reachable in practice.
    """
    from app.services import object_service

    summary = vcf.facts.get("vcf_stats_summary")
    if not isinstance(summary, dict) or "variants" not in summary:
        return AnnotationInputs(
            ok=False,
            reason=(
                "Variant results haven't been computed for this VCF yet, so "
                "there is nothing to annotate. Compute its results first."
            ),
        )
    if not summary.get("variants"):
        return AnnotationInputs(
            ok=False,
            reason=(
                "This VCF contains no called variants, so there is nothing "
                "to annotate."
            ),
        )

    reference = None
    for parent_id in vcf.derived_from or []:
        parent = await DataObject.get(parent_id)
        if parent is not None and parent.role is ObjectRole.REFERENCE:
            reference = parent
            break

    if reference is None:
        return AnnotationInputs(
            ok=False,
            reason=(
                f"Cannot determine which reference {vcf.name} was called "
                "against -- an uploaded VCF carries no record of it, so there "
                "is nothing to read genes from."
            ),
        )

    # Without a confirmed accession there is nothing to match a GFF3 against:
    # picking "any GFF3 in the project" would silently pair an unrelated
    # genome's annotation with this reference (see the accession check below).
    accession = reference.facts.get("ncbi_assembly_accession")
    if not accession:
        return AnnotationInputs(
            ok=False,
            reference=reference,
            reason=(
                f"{reference.name} has no NCBI assembly accession recorded, so "
                "no annotation can be confirmed to match it. Annotation needs "
                "a GFF3 for this exact assembly."
            ),
        )

    candidates = await object_service.list_objects(
        vcf.project_id, limit=500, status=ObjectStatus.READY
    )
    annotation = None
    for obj in candidates:
        if obj.role is not ObjectRole.ANNOTATION:
            continue
        if obj.format.kind not in ANNOTATION_KINDS:
            continue
        # Match on assembly accession rather than taking any GFF3 in the
        # project. An annotation for a different assembly parses fine and
        # annotates nothing, which reads as a successful run producing an
        # empty column -- worse than refusing.
        if obj.facts.get("ncbi_assembly_accession") != accession:
            continue
        annotation = obj
        break

    if annotation is None:
        return AnnotationInputs(
            ok=False,
            reference=reference,
            reason=(
                "No annotation (GFF3) for this reference. Download it from "
                "NCBI alongside the genome -- the assembly download offers it."
            ),
        )

    return AnnotationInputs(ok=True, reference=reference, annotation=annotation)
