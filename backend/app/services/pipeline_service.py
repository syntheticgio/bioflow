"""Launching pipeline runs.

Sits between the API and the queue: resolves which files a run will read,
validates that they can actually be trimmed, and builds the payload the
handler expects. Kept out of the router so the launch rules are testable
without HTTP.
"""

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from beanie import PydanticObjectId

from app.config import settings
from app.errors import ConflictError, ValidationError
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
    Locality,
    ObjectRole,
    ObjectStatus,
    RunInput,
    RunInputRole,
    RunJobRole,
    RunKind,
    SequencingPlatform,
    SidecarRole,
)
from app.models.run import AppliedParameterSet
from app.pipelines import (
    align_params as align_params_module,
)
from app.pipelines import (
    align_runner,
    aligner_registry,
    aligners,
    assembler_registry,
    assembly_qc_registry,
    counts_runner,
    csq_parse,
    cutadapt_runner,
    de_runner,
    fastp_runner,
    lineage_inference,
    pairing,
    polypolish_runner,
    qc_stats,
    ragtag_runner,
    resource_estimator,
    sniffles_runner,
    tools,
    trimmomatic_runner,
    variant_runner,
)
from app.pipelines import (
    assembly_params as assembly_params_module,
)
from app.pipelines.aligners import Aligner
from app.services import (
    blob_service,
    memory_estimate,
    object_service,
    resource_limit_service,
    run_service,
)
from app.storage.paths import blob_path

log = get_logger(__name__)

TRIMMABLE_KINDS = {FormatKind.FASTQ}

# Merqury ships `best_k.sh` to derive k from genome size; 21 is its own
# commonly-used default for small-to-mid genomes. Task 7 (real-data
# verification) should sanity-check this against the genome sizes BioFlow
# actually sees rather than trusting it forever.
DEFAULT_MERYL_K = 21


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
    obj_input = pairing.PairInput(name=obj.name, facts=obj.facts, metadata=obj.metadata)
    matches = []
    for c in candidates:
        c_input = pairing.PairInput(name=c.name, facts=c.facts, metadata=c.metadata)
        v = pairing.verdict(obj_input, c_input)
        if v in (pairing.Verdict.CONFIRMED, pairing.Verdict.NAME_ONLY):
            matches.append(c)
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


def _refetch_accession(obj: DataObject) -> str | None:
    """The accession an offloaded object can be fetched back from.

    Prefers the typed `remote_source`, falling back to `metadata.sra_run`.
    The fallback is not redundant: `sra_run` is what the download path has
    always written, so every object downloaded before this feature existed
    carries the address in metadata and nothing in `remote_source`. Without
    it the refusal degrades to "fetch it before using it" -- true, but it
    drops the one detail that tells the user *what* to fetch.
    """
    if obj.remote_source is not None:
        return obj.remote_source.accession
    run = obj.metadata.get("sra_run")
    return str(run) if run else None


async def _resolve_readable(obj: DataObject) -> tuple[str | None, str | None]:
    """Locate an object's bytes as (digest, path).

    Registered-in-place files have no managed blob to address by hash, so the
    external path is the only way to reach them.

    The remote check comes first deliberately. An offloaded object has no
    `blob_sha256`, so the not-yet-ingested branch below would claim it "has no
    stored content yet (status=ready)" -- a sentence that describes an upload
    in flight, contradicts itself, and sends the user looking for a job that
    does not exist. Answering the remote case first is what makes the message
    name the action they can actually take.
    """
    if obj.locality is Locality.REMOTE:
        accession = _refetch_accession(obj)
        where = f" from {accession}" if accession else ""
        raise ValidationError(
            f"{obj.name!r} is stored remotely -- fetch it{where} before using it",
            details={
                "object_id": str(obj.id),
                "locality": obj.locality.value,
                "accession": accession,
            },
        )

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
    owner: str,
    mate_object_id: PydanticObjectId | None = None,
    params: dict | None = None,
    paired: bool = True,
    tool: str = "fastp",
):
    """Queue a trim run over one object, or an R1/R2 pair.

    `paired=False` forces single-end treatment even when a mate is known, which
    is the escape hatch for a pair that should not be trimmed together.

    `owner` gates the input lookup rather than merely labelling the output, and
    that ordering is the whole point. Resolving the reads unscoped and then
    stamping the caller's profile onto the job would let one profile spend the
    machine on another profile's file and deposit the trimmed FASTQ into its
    own library -- a read leak and a write leak from one mistake. Refusing the
    read is the only version that cannot go wrong in either direction.
    """
    from app.queue import queue
    from app.services import object_service

    _check_tool_runnable(tool)
    tools.require(_trim_tool(tool))

    obj = await object_service.get_object(object_id, owner=owner)
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
            # Scoped like the primary read: an explicit mate id is just as
            # caller-supplied, and an unscoped fetch here would put another
            # profile's R2 into this profile's trim.
            mate = await object_service.get_object(mate_object_id, owner=owner)
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
        owner=owner,
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
        # The caller's profile, not `obj.owner`. The lookup above is scoped, so
        # the two are now equal by construction -- naming the caller keeps that
        # equality enforced at the seam that checks it rather than re-derived
        # from a field that would silently carry a stale value if the fetch
        # ever widened again.
        owner=owner,
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
# default, so listing it would add nothing. Derived from
# qc_stats.LONG_READ_PLATFORMS rather than hand-written as its own inverse --
# this dict and qc_stats._QC_STATS_PLATFORM (now qc_stats.LONG_READ_PLATFORMS
# itself) used to be independently maintained inverses of each other in two
# different files.
_SAM_TO_SRA_PLATFORM = qc_stats.SHORT_TO_SRA_PLATFORM

# The closed vocabulary `metadata.platform` holds since #525. Derived from the
# enum, so a tag added there is recognized here without a second edit -- the
# lookup is what tells a real SRA tag apart from an instrument model left in
# the field by the pre-split code.
_SRA_PLATFORM_TAGS = frozenset(p.value for p in SequencingPlatform)


def _qc_platform(obj: DataObject) -> str:
    """Which QC tool family this file's reads call for.

    Three sources, most authoritative first.

    `facts.sra_platform` is stamped by an SRA download in NCBI's own
    spelling and needs no inference, so it wins outright.

    `metadata.platform` comes second: since #525 it is a closed field
    holding an SRA tag, so it is read directly rather than inferred from.
    Before that split it held an instrument model, which is why an
    unrecognized value here falls through to the model rather than being
    trusted -- an object last written by the old code has a machine name in
    this field, and the migration cannot reach an object nobody has
    re-saved.

    `metadata.instrument_model` is last and goes through `sam_platform`,
    because it holds names like "PromethION" or "Sequel IIe" rather than a
    platform. The substring table that already exists for read groups is
    the thing that knows those models.
    """
    recorded = (obj.facts or {}).get("sra_platform")
    if isinstance(recorded, str) and recorded.strip():
        return recorded.strip().upper()

    metadata = obj.metadata or {}
    declared = metadata.get("platform")
    if isinstance(declared, str) and declared.strip().upper() in _SRA_PLATFORM_TAGS:
        return declared.strip().upper()

    sam = sam_platform(platform_label(metadata))
    return _SAM_TO_SRA_PLATFORM.get(sam, "ILLUMINA")


_LONG_READ_QC_PLATFORMS = frozenset(qc_stats.LONG_READ_PLATFORMS)


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


async def launch_qc(*, object_id: PydanticObjectId, owner: str):
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
    from app.services import object_service

    tools.require(tools.fastp())

    obj = await object_service.get_object(object_id, owner=owner)
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
        owner=owner,
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


# Prefixes of the bookkeeping keys each summary applier writes onto
# obj.facts (the narrative, model name, timestamp, and the fingerprint
# itself). One shared function computes the dedup fingerprint for all three
# summary kinds, so it has to exclude all three prefixes -- missing one means
# that once its applier lands, its own bookkeeping keys become part of the
# material the next fingerprint is computed from, so the "unchanged inputs"
# case can never match again and force=False stops deduping forever. Add a
# prefix here whenever a new summary_fingerprint()-based dedup check is added.
_SUMMARY_FACT_PREFIXES = ("ai_summary", "ai_de_summary", "ai_variant_summary")


def summary_fingerprint(obj: DataObject) -> str:
    """A digest of the inputs a summary would be written from.

    Covers facts and metadata but deliberately excludes the summary
    bookkeeping keys themselves (see `_SUMMARY_FACT_PREFIXES`) -- otherwise
    writing a summary would change the fingerprint that describes what it
    summarized, and every summary would be born stale.
    """
    import hashlib
    import json

    material = {
        "facts": {
            k: v
            for k, v in obj.facts.items()
            if not k.startswith(_SUMMARY_FACT_PREFIXES)
        },
        "metadata": obj.metadata,
        "role": obj.role.value if obj.role else None,
    }
    encoded = json.dumps(material, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


async def launch_summary(
    *,
    object_id: PydanticObjectId,
    owner: str,
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
    from app.services import object_service

    if not settings.llm_summaries_enabled:
        return None

    # Raises rather than returning None, unlike the two "nothing to do" exits
    # around it. A wrong-owner id is not an ordinary outcome of an additive
    # feature -- returning None would let the API report it as the 409
    # "disabled or nothing to summarize", which tells a caller poking at
    # another profile's id something different from what it tells a caller
    # poking at a nonexistent one. The 404 keeps those two indistinguishable.
    obj = await object_service.get_object(object_id, owner=owner)

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
        owner=owner,
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


async def launch_de_summary(
    *,
    object_id: PydanticObjectId,
    owner: str,
    force: bool = False,
) -> Job | None:
    """Queue a narrative summary of a differential-expression result.

    Same "additive, both no's are ordinary" contract as launch_summary. The
    top-gene table is read from the DE results TSV rather than facts, since
    facts holds aggregate counts only -- de_runner.read_results() is the same
    reader ExpressionResults' gene-table endpoint already uses. Resolving the
    file goes through object_service.object_with_blob(), the same
    blob/external-path resolution the results-table route uses; DataObject has
    no storage_path attribute of its own.
    """
    from app.queue import queue
    from app.services import object_service

    if not settings.llm_summaries_enabled:
        return None

    obj = await object_service.get_object(object_id, owner=owner)
    if obj.role != ObjectRole.DE_RESULTS:
        return None

    facts = {k: v for k, v in obj.facts.items() if not k.startswith("ai_de_summary")}
    fingerprint = summary_fingerprint(obj)
    if not force and obj.facts.get("ai_de_summary_fingerprint") == fingerprint:
        return None

    significant_up = facts.get("significant_up") or 0
    significant_down = facts.get("significant_down") or 0
    top_genes: list[dict] = []
    if significant_up or significant_down:
        _, blob = await object_service.object_with_blob(object_id, owner=owner)
        if blob is not None:
            target = (
                Path(blob.external_path)
                if blob.storage is BlobStorage.EXTERNAL and blob.external_path
                else blob_path(obj.blob_sha256)
            )
            if target.is_file():
                rows = de_runner.read_results(target)
                sorted_rows = de_runner.sort_rows(rows, sort="padj", direction="asc")
                top_genes = [
                    {
                        "gene": row.get("gene") or None,
                        "log2fc": row.get("log2_fold_change"),
                        "padj": row.get("padj"),
                    }
                    for row in sorted_rows[:20]
                ]

    if not significant_up and not significant_down and not top_genes:
        return None

    payload = {
        "object_id": str(obj.id),
        "facts": facts,
        "top_genes": top_genes,
        "facts_fingerprint": fingerprint,
    }

    dedup = f"de_summary:{obj.id}:{fingerprint}"
    if force:
        from uuid import uuid4

        dedup = f"{dedup}:{uuid4().hex[:8]}"

    job = await queue.enqueue(
        "summarize_de_results",
        owner=owner,
        payload=payload,
        job_class=JobClass.USER_BACKGROUND,
        resources=JobResources(cpu=0, mem_mb=64, io=IoClass.LIGHT),
        max_attempts=2,
        dedup_key=dedup,
        project_id=obj.project_id,
        object_id=obj.id,
    )
    if job is not None:
        log.info("de_summary_launched", job_id=str(job.id), object_id=str(obj.id))
    return job


async def launch_variant_summary(
    *,
    object_id: PydanticObjectId,
    owner: str,
    force: bool = False,
) -> Job | None:
    """Queue a narrative summary of a VCF's call-set statistics.

    Same contract as launch_de_summary. The top-N-by-severity variant list
    comes from `facts["severe_variants"]`, populated by run_vcf_stats -- see
    _top_severe_variants.
    """
    from app.queue import queue
    from app.services import object_service

    if not settings.llm_summaries_enabled:
        return None

    obj = await object_service.get_object(object_id, owner=owner)
    facts = {k: v for k, v in obj.facts.items() if not k.startswith("ai_variant_summary")}
    variant_count = (facts.get("vcf_stats_summary") or {}).get("variants") or 0

    fingerprint = summary_fingerprint(obj)
    if not force and obj.facts.get("ai_variant_summary_fingerprint") == fingerprint:
        return None

    top_variants = _top_severe_variants(facts)

    if not variant_count and not top_variants:
        return None

    payload = {
        "object_id": str(obj.id),
        "facts": facts,
        "top_variants": top_variants,
        "facts_fingerprint": fingerprint,
    }

    dedup = f"variant_summary:{obj.id}:{fingerprint}"
    if force:
        from uuid import uuid4

        dedup = f"{dedup}:{uuid4().hex[:8]}"

    job = await queue.enqueue(
        "summarize_variant_results",
        owner=owner,
        payload=payload,
        job_class=JobClass.USER_BACKGROUND,
        resources=JobResources(cpu=0, mem_mb=64, io=IoClass.LIGHT),
        max_attempts=2,
        dedup_key=dedup,
        project_id=obj.project_id,
        object_id=obj.id,
    )
    if job is not None:
        log.info("variant_summary_launched", job_id=str(job.id), object_id=str(obj.id))
    return job


def _top_severe_variants(facts: dict, limit: int = 20) -> list[dict]:
    """The variant rows facts already carries, ranked by consequence severity.

    Reads from `facts["severe_variants"]` -- populated by run_vcf_stats
    alongside consequence_counts, one row per variant with a consequence in
    csq_parse.SEVERITY_ORDER, capped there at the same limit this function
    also respects. Nothing here re-parses the VCF. Absent (Task 7 not yet
    run, or an older object predating it), this simply returns [].

    The severity vocabulary lives in csq_parse.py, not here: it must match
    the consequence strings parse_bcsq actually returns (bcftools csq's own
    "missense"/"stop_gained"/... vocabulary), and csq_parse is the only
    place that produces those strings.
    """
    rows = facts.get("severe_variants")
    if not isinstance(rows, list):
        return []

    def rank(row: dict) -> int:
        consequence = row.get("consequence")
        try:
            return csq_parse.SEVERITY_ORDER.index(consequence)
        except ValueError:
            return len(csq_parse.SEVERITY_ORDER)

    ranked = sorted(rows, key=rank)
    return ranked[:limit]


def _params_fingerprint(params: dict) -> str:
    """A short stable digest of the trim settings, for the dedup key."""
    import hashlib

    encoded = "|".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha256(encoded.encode()).hexdigest()[:12]


# --- Alignment --------------------------------------------------------------

ALIGNABLE_KINDS = {FormatKind.FASTQ}
REFERENCE_KINDS = {FormatKind.FASTA}

class SamPlatform(StrEnum):
    """The SAM `@RG PL` vocabulary, verbatim from the specification.

    Source: https://github.com/samtools/hts-specs, `SAMv1.tex`, the `@RG` `PL`
    row -- "Valid values: CAPILLARY, DNBSEQ (MGI/BGI), ELEMENT, HELICOS,
    ILLUMINA, IONTORRENT, LS454, ONT (Oxford Nanopore), PACBIO (Pacific
    Biosciences), SINGULAR, SOLID, and ULTIMA."

    Membership is set by that standard, not by what this codebase happens to
    detect. Two members are currently produced by no pattern -- CAPILLARY,
    which nothing sequences here, and DNBSEQ, which is new in this commit --
    and that is correct: a reachability test of the kind
    `test_every_option_is_reachable_by_some_token` applies would be wrong for
    an externally-owned vocabulary.

    There is deliberately no OTHER member. It is not in the spec, and the
    spec's remedy for an unrecognized technology is to omit the field rather
    than substitute a placeholder -- see `sam_platform`.
    """

    CAPILLARY = "CAPILLARY"
    DNBSEQ = "DNBSEQ"
    ELEMENT = "ELEMENT"
    HELICOS = "HELICOS"
    ILLUMINA = "ILLUMINA"
    IONTORRENT = "IONTORRENT"
    LS454 = "LS454"
    ONT = "ONT"
    PACBIO = "PACBIO"
    SINGULAR = "SINGULAR"
    SOLID = "SOLID"
    ULTIMA = "ULTIMA"


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
_SAM_PLATFORM_PATTERNS: tuple[tuple[tuple[str, ...], SamPlatform], ...] = (
    (("nanopore", "minion", "gridion", "promethion", "flongle"), SamPlatform.ONT),
    (("pacbio", "sequel", "revio", "rs ii"), SamPlatform.PACBIO),
    (("dnbseq", "mgiseq", "bgiseq"), SamPlatform.DNBSEQ),
    (("ion torrent", "ion proton", "ion s5", "ion gene"), SamPlatform.IONTORRENT),
    (("454 gs", "gs flx", "gs junior"), SamPlatform.LS454),
    (("solid",), SamPlatform.SOLID),
    (("helicos",), SamPlatform.HELICOS),
    (("element", "aviti"), SamPlatform.ELEMENT),
    (("ultima",), SamPlatform.ULTIMA),
    (("singular", "g4"), SamPlatform.SINGULAR),
    (
        (
            "illumina", "novaseq", "nextseq", "miseq", "hiseq", "miniseq",
            "iseq", "genome analyzer", "nova x",
        ),
        SamPlatform.ILLUMINA,
    ),
)

# Which preset suits a platform's reads. The wrong one produces silently poor
# alignments rather than an error, so this is a real default rather than a
# convenience.
_PLATFORM_PRESETS: dict[SamPlatform, str] = {
    SamPlatform.ONT: align_runner.Preset.MAP_ONT,
    SamPlatform.PACBIO: align_runner.Preset.MAP_PB,
}


def sam_platform(metadata_platform: str | None) -> SamPlatform | None:
    """A SAM `PL` value from a platform label or instrument model.

    Returns None when the recorded value is not in the SAM vocabulary, which
    means *omit the field*: SAMv1.tex says the PL field "should be omitted
    when the technology is not in this list ... or is unknown." This used to
    return "OTHER", and the docstring used to claim OTHER was a spec value.
    It is not one.

    Falls back to ILLUMINA when nothing is recorded at all -- the
    overwhelmingly common case here, and a wrong guess is visible in the BAM
    header rather than silent. That asymmetry is deliberate: an empty field
    means "nobody said," while an unrecognized non-empty field means "somebody
    said something this vocabulary cannot express," and only the second is a
    case the spec rules on.
    """
    text = (metadata_platform or "").strip().lower()
    if not text:
        return SamPlatform.ILLUMINA

    for needles, sam_value in _SAM_PLATFORM_PATTERNS:
        if any(needle in text for needle in needles):
            return sam_value
    return None


def platform_label(metadata: dict | None) -> str | None:
    """The best text to hand `sam_platform`, from either shape of metadata.

    Since #525 the machine name lives in `instrument_model` and `platform`
    holds an SRA tag. `sam_platform` matches on substrings and recognizes
    both spellings -- "MinION" and "OXFORD_NANOPORE" both contain a needle
    it knows -- so this only has to decide which field to prefer, not
    translate between them.

    The instrument model wins because it is the more specific of the two:
    "Sequel IIe" and "Revio" are both PACBIO_SMRT, and while that
    distinction does not change the SAM `PL` value today, reading the
    coarser field first would make it impossible to ever act on.

    Objects written before the split still carry a model in `platform`, and
    fall through to it unchanged -- the migration cannot reach an object
    that is not in the database.
    """
    metadata = metadata or {}
    for key in ("instrument_model", "platform"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def suggested_preset(
    sam_pl: SamPlatform | None, *, chemistry: align_runner.ReadChemistry | None = None
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
        "platform": sam_platform(platform_label(metadata)),
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

    # The instrument model before the platform tag: "NextSeq 550" is a more
    # informative library name than "ILLUMINA", which after #525 is one of
    # thirteen values and so distinguishes almost nothing.
    for key in ("instrument_model", "platform"):
        value = metadata.get(key)
        if value and str(value).strip():
            return str(value).strip()

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


def gci_slot_for_chemistry(chemistry: align_runner.ReadChemistry | None) -> str | None:
    """Which GCI input slot a read chemistry belongs in, or None to refuse.

    GCI has exactly two slots and no short-read input exists at all, which
    makes this stricter than CRAQ's `-ngs`/`-sms` routing:

      HIFI                      -> --hifi
      ONT_SIMPLEX, ONT_DUPLEX   -> --nano
      CLR                       -> refuse
      SHORT                     -> refuse
      UNKNOWN / None            -> refuse (the dialog asks)

    CLR is the case worth spelling out, because it is long-read and
    therefore looks eligible. PacBio CLR is not HiFi: GCI's identity and
    clipping filters assume HiFi-grade per-read accuracy, and CLR's error
    profile is nothing like it. Routing CLR to --hifi does not degrade
    gracefully, it mislabels the evidence.

    Refusing UNKNOWN follows CRAQ's rule.
    `read_chemistry_for_alignment`'s docstring says callers "fall back to
    the conservative short-read default rather than guessing" -- correct
    for picking an alignment preset, wrong here, and doubly so when SHORT
    is not even a valid input.
    """
    if chemistry == align_runner.ReadChemistry.HIFI:
        return "hifi"
    if chemistry in (align_runner.ReadChemistry.ONT_SIMPLEX, align_runner.ReadChemistry.ONT_DUPLEX):
        return "nano"
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

    platform = sam_platform(platform_label(obj.metadata)) if obj else "ILLUMINA"
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

    `star_annotated` rides alongside `star` rather than replacing it: the two
    are built from different sidecar roles and a reference can carry either,
    neither, or both -- see `aligners.SidecarRole.STAR_ANNOTATED_INDEX`.
    """
    from app.services import object_service

    sidecars = await object_service.list_sidecars(reference.id, owner=reference.owner)
    have = {s.sidecar_role for s in sidecars if s.sidecar_role}
    # Map each built index to its sidecar object id so the frontend can offer
    # download buttons. A role not in the map simply has no download link.
    role_to_aligner = {v: k.value for k, v in aligners.INDEX_ROLE.items()}
    index_ids: dict[str, str] = {}
    for s in sidecars:
        if s.sidecar_role and s.sidecar_role in role_to_aligner:
            index_ids[role_to_aligner[s.sidecar_role]] = str(s.id)
        elif s.sidecar_role == SidecarRole.FAI:
            index_ids["fai"] = str(s.id)
    return {
        aligner.value: aligners.INDEX_ROLE[aligner] in have for aligner in Aligner
    } | {
        "fai": SidecarRole.FAI in have,
        "star_annotated": SidecarRole.STAR_ANNOTATED_INDEX in have,
        "index_ids": index_ids,
    }


async def align_envelope(
    *, object_id: PydanticObjectId, reference_id: PydanticObjectId, owner: str,
    chunked: bool = False,
) -> dict:
    """Host budgets, input sizes, and the per-aligner memory coefficients.

    Budgets come from the governor, which reads cgroup limits -- so inside
    Docker this reports the container's real allocation rather than the
    host's. That distinction is the whole reason the warning is trustworthy:
    a machine with 64 GB and an 8 GB Docker allocation will OOM at 8.
    """
    from dataclasses import asdict

    from app.queue.governor import LoadGovernor
    from app.services import object_service

    obj = await object_service.get_object(object_id, owner=owner)
    reference = await object_service.get_object(reference_id, owner=owner)

    governor = LoadGovernor()

    reference_bases = reference_bases_for(reference)

    status = await reference_index_status(reference)

    from app.pipelines import aligner_registry

    result = {
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

    if chunked:
        # Read .fai to get sequence count
        fai_path = (
            Path(settings.bioinfo_home)
            / "objects" / str(reference.id)
            / f"{reference.name}.fai"
        )
        if fai_path.exists():
            sequences = _parse_fai(fai_path)
            result["chunking"] = {
                "supported": len(sequences) > 1,
                "total_sequences": len(sequences),
            }
        else:
            result["chunking"] = {"supported": False}

    return result


async def sidecar_payload(reference: DataObject, aligner: Aligner) -> dict:
    """{stored name: blob path} for the sidecars an alignment needs.

    Includes the `.fai` alongside the aligner's own index: samtools wants it
    beside the reference, and materializing one without the other produces a
    workdir that looks right and fails partway through.
    """
    from app.services import object_service

    wanted = {aligners.INDEX_ROLE[aligner], SidecarRole.FAI}
    payload: dict = {}
    for sidecar in await object_service.list_sidecars(reference.id, owner=reference.owner):
        if sidecar.sidecar_role not in wanted or not sidecar.blob_sha256:
            continue
        digest, path = await _resolve_readable(sidecar)
        payload[sidecar.name] = path or str(blob_path(digest))
    return payload


async def launch_build_index(
    *,
    reference_id: PydanticObjectId,
    owner: str,
    aligner: str | Aligner = Aligner.MINIMAP2,
    annotation_id: PydanticObjectId | None = None,
):
    """Queue an index build for one (reference, aligner) pair.

    The eager entry point behind the explorer's **Build index** button. The
    same job type the alignment path queues, so there is no second code path to
    keep correct.

    `annotation_id` is STAR-only. Explicit-or-refuse rather than
    auto-picking a lone candidate the way `resolve_annotation` does for
    counting: unlike featureCounts, which fails loudly on the wrong
    annotation (a near-zero assignment rate), a STAR index built against the
    wrong GTF just quietly finds fewer junctions than it could have, so
    building *without* one is the safer default when the caller did not ask
    for one by name.
    """
    from app.services import object_service

    aligner = Aligner(aligner)
    tools.require(_aligner_tool(aligner))
    tools.require(tools.samtools())

    reference = await object_service.get_object(reference_id, owner=owner)
    _check_reference(reference)

    annotation: DataObject | None = None
    if annotation_id is not None:
        if aligner is not Aligner.STAR:
            raise ValidationError(
                f"{aligner.value} has no annotation-aware index; only STAR does."
            )
        annotation = await resolve_annotation(
            reference.project_id, annotation_id, owner=owner
        )

    job = await _enqueue_build_index(
        reference, aligner, owner=owner, annotation=annotation
    )
    if job is None:
        raise ConflictError(
            "An index for this reference is already being built",
            details={"reference_id": str(reference.id), "aligner": aligner.value},
        )
    return job


def reference_bases_for(reference) -> int:
    """How many bases of sequence a reference holds, for memory sizing.

    Prefers the parser's measured `total_bases` and falls back to the file
    size. That fallback was the sole input until #100, justified by a FASTA
    carrying about one byte per base -- true uncompressed, and wrong by ~6x for
    the gzipped references NCBI downloads produce by default. The #96 reference
    is 143.6 MB on disk against 448.6 Mbp of sequence, so every estimate for it
    was made against a sixth of the real genome, and an index build that could
    not fit was admitted as though it could.

    Note this is deliberately the opposite choice from `_infer_genome_size`,
    which refuses `total_bases` on purpose. That helper guesses an assembly's
    size from whatever candidate object it can find, where a `protein.faa`
    roled `reference` would answer with a protein length. Here the object is
    not a candidate: it is the exact file about to be indexed, so its own
    measured base count is precisely the number wanted.

    When nothing measured the sequence, a compressed file still must not be
    sized at its compressed length -- that understates in the one direction
    that causes an OOM kill. `max()` keeps the fallback at worst as wrong as it
    always was, never newly worse.
    """
    size = reference.size or 0
    measured = (reference.facts or {}).get("total_bases") or 0
    if measured > 0:
        return max(int(measured), size)
    return size


# The floor under a declared memory reservation. The estimator's coefficients
# are heuristics, and a reference whose `size` is missing or absurdly small
# would otherwise reserve almost nothing and let the governor admit the job
# alongside everything else.
MIN_DECLARED_MEM_MB = 2048

# The threads `build_index` runs with. Declared here rather than only in the
# handler's `@handler(resources=...)` because the memory estimate has to be
# computed against the same number the job will actually use.
INDEX_BUILD_THREADS = 4

# The job types whose memory the resolver arbitrates. These must match the
# strings the handlers register under (align_handlers.py:307,
# assembly_handlers.py:45) -- a typo here resolves against an empty history
# and silently falls back to the heuristic forever, with nothing failing.
JOB_TYPE_ALIGN_READS = "align_reads"
JOB_TYPE_ASSEMBLE = "assemble_reads"

# What to reserve for an assembly nothing can estimate. Assemblies are the
# heaviest thing this tool runs, and de novo assembly with no genome size is
# the normal case rather than a misconfigured one -- so this is deliberately
# generous. Reserving too little would let the governor admit an assembly
# alongside other work and drive the machine into swap.
UNKNOWN_ASSEMBLY_MEM_MB = 16384

# Bakta's declared reservation. Named rather than inlined so the launcher and
# the exhaustiveness test in test_heavy_launcher_overrides.py read the same
# number.
ANNOTATE_GENOME_MEM_MB = 16384

# Sized for bwa-mem2's alignment step, not for Polypolish -- see the handler's
# own note on why peak RSS scales with the draft rather than the reads.
POLISH_MEM_MB = 16384
POLISH_LONG_MEM_MB = 16384

# Matches the handler's own @handler(...) registration -- see
# assess_assembly_qv's docstring for the real-data measurement this figure
# is based on.
QV_QC_MEM_MB = 12288

CONTINUITY_QC_MEM_MB = 16384

VARIANT_CALLING_MEM_MB = 8192

COMPLETENESS_MEM_MB = 8192

MERYL_ANALYSIS_MEM_MB = 8192

# Matches the handler's own budget (see reference_assembly_handlers.
# consensus_from_alignment): 8192, not 4096, after a real run against a
# 26Mb T. brucei reference OOM-killed at the lower number.
CONSENSUS_MEM_MB = 8192

# Sized for minimap2's whole-genome alignment, not for RagTag's own graph
# work -- see the handler's own note.
SCAFFOLD_MEM_MB = 8192

MISASSEMBLY_QC_MEM_MB = 8192

SYNTENY_MEM_MB = 8192

ASSEMBLY_ERROR_QC_MEM_MB = 8192

QUANTIFY_MEM_MB = 4096

DIFFERENTIAL_EXPRESSION_MEM_MB = 4096

# Matches the handler's own @handler(...) registration (see
# feature_coverage_handlers.run_feature_coverage): a job cannot need less
# memory to run than it declares to the scheduler.
FEATURE_COVERAGE_MEM_MB = 1024


def refuse_if_over_budget(
    *, declared_mb: int, budget_mb: int, resource_override: bool
) -> None:
    """Refuse a job whose declared reservation could never be claimed (#478).

    Pure and budget-injected so it can be tested without a database, and so
    both launch paths share one definition of the refusal.

    `resource_override` is the user's "Launch anyway", the same flag the
    estimate-based refusal honours: it rides the job document to claim.lua,
    which admits the job when it is the sole occupant. That is a real escape
    here rather than a rubber stamp -- an over-budget job genuinely can run,
    just not alongside anything else.
    """
    if resource_override:
        return
    if not resource_estimator.exceeds_declared_budget(
        declared_mb=declared_mb, budget_mb=budget_mb
    ):
        return
    raise ValidationError(
        resource_estimator.explain_declared_refusal(
            declared_mb=declared_mb, budget_mb=budget_mb
        ),
        details={
            "refusal": "declared",
            "declared_mb": declared_mb,
            "budget_mb": budget_mb,
        },
    )


async def current_admission_budget_mb() -> int:
    """The ceiling a launch is checked against, matching the worker's.

    Reads the stored limits like `worker._resource_budgets` does, and falls
    back to the machine's own budget on a read failure for the same reason:
    a DB blip must not refuse every launch.
    """
    # Imported in-function, matching the existing LoadGovernor call sites
    # in this module. Hoisting it to module scope is a separate change with
    # cycle risk, not part of this fix.
    from app.queue.governor import LoadGovernor

    machine_mb = int(LoadGovernor().mem_budget_bytes() / (1024 * 1024))
    try:
        stored = await resource_limit_service.load()
        stored_mb = stored.max_mem_mb
    except Exception as e:  # noqa: BLE001 - a launch must survive a DB blip
        log.warning("resource_limits_read_failed", error=str(e))
        stored_mb = None
    return resource_limit_service.admission_budget_mb(
        stored_mb=stored_mb,
        machine_mb=machine_mb,
        hard_mem_mb=resource_limit_service.hard_mem_mb(),
    )


async def declared_align_mem_mb(
    *,
    aligner: Aligner,
    reference_bases: int,
    threads: int,
    sort_memory_mb: int,
    building_index: bool,
    input_bytes: int | None = None,
) -> int:
    """What to reserve with the queue for an alignment or index build.

    The same `resource_estimator` call the launch dialog already makes, used
    for the reservation rather than only for the warning shown to the user.

    Before this, both handlers declared a flat 8 GB whatever the aligner and
    whatever the genome. That is roughly right for bwa-mem2 on a human genome
    and wrong in both directions elsewhere: a STAR human index needs about
    30 GB, so the governor would admit it believing there was room, and the
    result is an OOM kill twenty minutes in with a log that says nothing --
    exactly the failure `resource_estimator` exists to prevent, arriving
    through the one door it was not watching.

    Note this is a *reservation*, not a limit: nothing enforces it on the
    process. Its job is to stop the governor from running two heavy jobs whose
    combined footprint does not fit.

    The number is resolved through `memory_estimate.resolve`, so once a job
    type has enough trustworthy history on this machine the reservation stops
    being a published coefficient and becomes a measurement. `input_bytes` is
    what makes that possible; without it only the heuristic can answer.

    The floor applies whatever the source, including UNKNOWN.
    """
    heuristic_mb = resource_estimator.estimate_mb(
        aligner=aligner,
        reference_bases=reference_bases,
        threads=threads,
        sort_memory_mb=sort_memory_mb,
        building_index=building_index,
    )
    resolved = await memory_estimate.resolve(
        job_type=JOB_TYPE_ALIGN_READS,
        input_bytes=input_bytes,
        heuristic_mb=heuristic_mb,
        threads=threads,
    )
    return max(resolved.mb or 0, MIN_DECLARED_MEM_MB)


async def _enqueue_build_index(
    reference: DataObject,
    aligner: Aligner,
    *,
    owner: str,
    annotation: DataObject | None = None,
):
    """Queue the index build, deduplicated on (reference blob, aligner, annotation).

    `owner` is the caller's profile. Both call sites resolve `reference`
    through an owner-scoped lookup first, so it equals `reference.owner` -- it
    is passed explicitly so this function does not have to trust that a
    reference it was merely handed was fetched under the right scope.

    `annotation` is STAR-only -- see `align_handlers.build_index`, which
    raises if a GTF payload arrives for any other aligner.
    """
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

    gtf_digest: str | None = None
    gtf_path: str | None = None
    if annotation is not None:
        gtf_digest, gtf_path = await _resolve_readable(annotation)
        if gtf_digest:
            payload["gtf_sha256"] = gtf_digest
        if gtf_path:
            payload["gtf_path"] = gtf_path

    # Keyed on the blob rather than the object: the same genome registered in
    # two of one profile's projects is one index, with no cross-project
    # bookkeeping. Deliberately left content-only -- `queue.enqueue` prefixes
    # the owner onto whatever key it is handed, so the profile scoping is
    # applied in exactly one place instead of being re-derived by every caller
    # that happens to remember. Sharing therefore stops at the profile
    # boundary: two profiles aligning against the same genome build one index
    # each, which is the price of neither of them silently getting none.
    #
    # The annotation's own digest joins the key rather than a bare
    # "annotated"/"unannotated" flag: two different GTFs for the same
    # reference (a stale one and a corrected re-download) are two different
    # indexes, not the same slot overwritten -- an alignment already run
    # against the stale one should not have its index silently swapped
    # underneath it by a later build.
    dedup_key = f"build_index:{digest or path}:{aligner.value}:{gtf_digest or gtf_path or ''}"

    # `sort_memory_mb=0` because an index build runs no samtools sort -- the
    # estimator's sort term would otherwise reserve memory for a step that is
    # not in this job.
    mem_mb = await declared_align_mem_mb(
        aligner=aligner,
        reference_bases=reference_bases_for(reference),
        threads=INDEX_BUILD_THREADS,
        sort_memory_mb=0,
        building_index=True,
    )

    return await queue.enqueue(
        "build_index",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(
            cpu=INDEX_BUILD_THREADS, mem_mb=mem_mb, io=IoClass.HEAVY
        ),
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


async def ensure_local(obj: DataObject, *, owner: str) -> PydanticObjectId | None:
    """Queue the fetch an offloaded object needs, returning a job to wait on.

    Returns None when the object's bytes are already here, which is the
    overwhelmingly common case -- callers can chain this unconditionally and
    only extend `depends_on` when something comes back.

    The dedup key is the object id rather than its content: an offloaded
    object has no digest to key on, which is the whole reason it is remote.
    `queue.enqueue` folds the owner in, so two profiles never collide.

    When the key deduplicates the job away, the in-flight job is found and
    returned instead -- the same race `_enqueue_build_index`'s caller handles,
    and for the same reason: two pipelines started seconds apart on one
    offloaded file must wait on one download, not race two.
    """
    # `getattr` rather than attribute access: this is called from every launch
    # path, several of which are exercised with lightweight object stand-ins,
    # and an object without the field is by definition one that predates
    # offloading -- which is local. Same defaulting the model itself does.
    if getattr(obj, "locality", Locality.LOCAL) is not Locality.REMOTE:
        return None

    from app.queue import queue

    accession = _refetch_accession(obj)
    if not accession:
        raise ValidationError(
            f"{obj.name!r} is stored remotely but nothing records where to fetch it from",
            details={"object_id": str(obj.id)},
        )

    job = await queue.enqueue(
        "fetch_remote",
        owner=owner,
        payload={
            "object_id": str(obj.id),
            "accession": accession,
            "project_id": str(obj.project_id),
            "bytes_estimate": obj.remote_source.size if obj.remote_source else None,
        },
        job_class=JobClass.USER_INTERACTIVE,
        project_id=obj.project_id,
        object_id=obj.id,
        dedup_key=f"fetch_remote:{obj.id}",
    )
    if job is not None:
        return job.id

    existing = await Job.find_one(active_fetch_job_query(obj.id))
    return existing.id if existing is not None else None


def active_fetch_job_query(object_id: PydanticObjectId) -> dict:
    """The in-flight fetch for an offloaded object, if there is one.

    A raw Mongo query for the reason `active_index_job_query` records:
    `Job.state` is not resolvable as an attribute outside a query context.
    Extracted and named so the shape is assertable without a database.
    """
    return {
        "type": "fetch_remote",
        "state": {"$in": [s.value for s in ACTIVE_STATES]},
        "object_id": object_id,
    }


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


def active_annotation_stats_job_query(object_id: PydanticObjectId) -> dict:
    """The in-flight stats computation for an annotation, if there is one.

    Same shape as `active_index_job_query` above, for the same reason:
    `ensure_annotation_stats` calling `launch_annotation_stats` while an
    identical job is already queued or running gets `None` back from
    `queue.enqueue` (deduplicated on `dedup_key=f"annotation_stats:{id}"`),
    and needs this to find the in-flight job's id so it can depend on it
    instead of racing it.
    """
    return {
        "type": "run_annotation_stats",
        "state": {"$in": [s.value for s in ACTIVE_STATES]},
        "object_id": object_id,
    }


class ReadSet:
    """One resolved read set in an alignment launch.

    `r1` is the file the run's read group, output name, and label are built
    from, always R1-first after the same swap the single-shot path applied to
    the primary pair. `r2` is None for a single-end set. Set 0 of a launch is
    the primary pair; the rest are the additional sets, in request order.
    """

    __slots__ = ("r1", "r2")

    def __init__(self, r1: DataObject, r2: DataObject | None = None):
        self.r1 = r1
        self.r2 = r2


# One additional read set at the launch boundary: (r1_id, r2_id) from the API
# route, or (r1_id, r2_id, strict) from the workflow node, whose chunked reads
# are single-end by construction and opt out of the strict pairing rule.
ReadSetSpec = tuple[
    PydanticObjectId, PydanticObjectId | None
] | tuple[PydanticObjectId, PydanticObjectId | None, bool]


async def _resolve_alignment_read_sets(
    *,
    primary: DataObject,
    mate_object_id: PydanticObjectId | None,
    additional_sets: list[ReadSetSpec],
    owner: str,
    paired: bool,
) -> list[ReadSet]:
    """Resolve and validate every read set in a launch.

    Pairing is a property of the run, decided by the primary pair exactly as
    before: `paired` asks, an explicit mate or `suggest_mate` answers, and a
    primary with no discoverable mate degrades to single-end. Every additional
    set must then match that outcome -- a paired run requires each set to have
    a mate (explicit, else suggested, else the launch fails), and a single-end
    run forbids any set from declaring one.

    Every member is owner-scoped, READY, FASTQ, in the primary's project, and
    unique across the whole request; the R1-leads swap is applied per set. The
    checks here are the authoritative ones -- the dialog mirrors them for
    immediacy but can be bypassed.
    """
    from app.services import object_service

    # One deliberate asymmetry: the primary's `mate_object_id` in a single-end
    # launch is ignored, because that is what the field has always meant and
    # the frontend has always sent null. The additional sets are new surface,
    # so they get the strict reading of the rule from day one.
    seen: dict[str, str] = {}

    def _claim(obj: DataObject, where: str) -> None:
        key = str(obj.id)
        if key in seen:
            raise ValidationError(
                f"{obj.name!r} is used twice in this alignment: as {seen[key]} "
                f"and as {where}"
            )
        seen[key] = where

    async def _resolve_one(
        r1_id: PydanticObjectId,
        r2_id: PydanticObjectId | None,
        *,
        set_label: str,
        require_mate: bool,
        strict: bool,
        r1: DataObject | None = None,
    ) -> ReadSet:
        # The primary arrives already fetched and checked by the caller;
        # additional sets are fetched by id here, so their objects are
        # scoped and validated the same way the primary's was.
        if r1 is None:
            r1 = await object_service.get_object(r1_id, owner=owner)
            _check_alignable(r1)
            if r1.project_id != primary.project_id:
                raise ValidationError("Reads must be in the same project")
        _claim(r1, f"{set_label} reads")

        r2: DataObject | None = None
        if r2_id is not None:
            if not require_mate:
                raise ValidationError(
                    f"{r1.name!r} declares a mate, but this alignment is "
                    "single-end; remove the mate or launch paired"
                )
            r2 = await object_service.get_object(r2_id, owner=owner)
        elif require_mate:
            r2 = await suggest_mate(r1)
        if r2 is not None:
            if r2.id == r1.id:
                raise ValidationError("A file cannot be its own mate")
            if r2.project_id != primary.project_id:
                raise ValidationError("Reads must be in the same project")
            _check_alignable(r2)
            _claim(r2, f"{set_label} mate")
            # R1 leads, so the mates reach the aligner in the order it expects.
            if pairing.mate_of(r1.name) == "R2" and pairing.mate_of(r2.name) == "R1":
                r1, r2 = r2, r1
        elif require_mate and strict:
            raise ValidationError(
                f"{r1.name!r} is in a paired alignment but has no mate; "
                "pair it with a mate or launch single-end"
            )
        return ReadSet(r1, r2)

    primary_set = await _resolve_one(
        primary.id,
        mate_object_id if paired else None,
        set_label="the primary",
        # The primary degrades to single-end when no mate is found, exactly as
        # the launch path always did; the additional sets are strict because
        # their pairing must match the run's.
        require_mate=paired,
        strict=False,
        r1=primary,
    )
    run_paired = primary_set.r2 is not None

    sets = [primary_set]
    for index, item in enumerate(additional_sets, start=1):
        # A set is (r1_id, r2_id) plus an optional strictness flag. The dialog
        # transport never sets it: its sets are whole libraries, so pairing
        # must match the run exactly. The workflow node sets it False for its
        # chunked reads -- pieces of the primary's own library, deliberately
        # single-end, concatenated into the run's R1 stream the way they
        # always were before the strict rule existed.
        r1_id, r2_id = item[0], item[1]
        strict = item[2] if len(item) > 2 else True
        label = f"additional read set {index}"
        # The declared mate is passed through untouched: _resolve_one rejects
        # it when the run is single-end rather than silently dropping it.
        sets.append(
            await _resolve_one(
                r1_id,
                r2_id,
                set_label=label,
                require_mate=run_paired,
                strict=strict,
            )
        )
    return sets


async def _extra_set_payload(set_: ReadSet) -> dict:
    """One payload entry for an additional read set.

    Carries the set's R1 and, when paired, its mate -- both content-addressed
    so the handler can resolve them from the store and concatenate the R1s
    and R2s into their own streams.
    """
    r1_digest, r1_path = await _resolve_readable(set_.r1)
    entry: dict = {"name": set_.r1.name}
    if r1_digest:
        entry["sha256"] = r1_digest
    if r1_path:
        entry["path"] = r1_path
    if set_.r2 is not None:
        r2_digest, r2_path = await _resolve_readable(set_.r2)
        entry["mate_name"] = set_.r2.name
        if r2_digest:
            entry["mate_sha256"] = r2_digest
        if r2_path:
            entry["mate_path"] = r2_path
    return entry


async def launch_alignment(
    *,
    object_id: PydanticObjectId,
    reference_id: PydanticObjectId,
    owner: str,
    mate_object_id: PydanticObjectId | None = None,
    additional_read_sets: list[ReadSetSpec] | None = None,
    read_group: dict | None = None,
    params: dict | None = None,
    paired: bool = True,
    resource_override: bool = False,
    from_parameter_set: AppliedParameterSet | None = None,
):
    """Queue an alignment, building the reference index first if it is missing.

    An alignment against an unindexed reference enqueues `build_index` and then
    the alignment *behind it*, using the queue's dependency gate rather than a
    delay: the alignment waits on the index job's completion, so a failed index
    fails the alignment with a comprehensible reason instead of leaving it
    queued forever.
    """
    from app.queue import queue
    from app.services import object_service

    merged_params = {**default_align_params(), **(params or {})}
    # The workflow node used to carry additional reads through params as a
    # flat id list, which cannot express pairs; they are a typed request
    # field now, so a caller still sending this key is running stale code and
    # must hear about it rather than silently lose files.
    if "extra_reads" in merged_params:
        raise ValidationError(
            "params['extra_reads'] is no longer read; send additional reads "
            "as additional_read_sets"
        )
    align_params = align_params_module.from_dict(merged_params)
    aligner = align_params.aligner
    tools.require(_aligner_tool(aligner))
    tools.require(tools.samtools())

    # Reads and reference are both scoped to the caller. The same-project check
    # below would already catch most cross-profile pairings, since projects do
    # not span profiles -- but it would catch them as a ValidationError naming
    # the other profile's reference, which is a worse answer than a 404 that
    # never confirms the id resolves at all.
    obj = await object_service.get_object(object_id, owner=owner)
    _check_alignable(obj)

    # Every read set in the launch -- the primary pair first, then the
    # additional sets in request order -- is resolved and validated once,
    # before either path branches below.
    read_sets = await _resolve_alignment_read_sets(
        primary=obj,
        mate_object_id=mate_object_id,
        additional_sets=additional_read_sets or [],
        owner=owner,
        paired=paired,
    )
    primary_set = read_sets[0]
    extra_sets = read_sets[1:]

    reference = await object_service.get_object(reference_id, owner=owner)
    _check_reference(reference)
    if reference.project_id != obj.project_id:
        raise ValidationError("Reads and reference must be in the same project")

    # --- Chunked alignment path ---
    chunked = merged_params.pop("chunked", False)
    if chunked:
        from app.queue.governor import LoadGovernor

        spec = aligner_registry.spec_for(aligner)
        if not spec.chunking_supported:
            raise ValidationError(
                f"{aligner.value} does not support chunked alignment"
            )

        fai_path = (
            Path(settings.bioinfo_home)
            / "objects" / str(reference.id)
            / f"{reference.name}.fai"
        )
        if not fai_path.exists():
            raise ValidationError("Reference has no .fai — cannot chunk")

        governor = LoadGovernor()
        budget_mb = int(governor.mem_budget_bytes() / (1024 * 1024))
        sequences = _parse_fai(fai_path)

        from app.pipelines.align_buckets import pack_buckets, write_bucket_fastas

        fasta_path = blob_path(reference.blob_sha256) if reference.blob_sha256 else None
        if not fasta_path or not fasta_path.exists():
            raise ValidationError("Reference FASTA file not found — cannot chunk")

        buckets = pack_buckets(
            sequences=sequences,
            memory_budget_mb=budget_mb,
            per_base_index_mb=spec.memory_model.index_bytes_per_ref_base / (1024 * 1024),
            fixed_overhead_mb=spec.memory_model.fixed_overhead_mb,
            bytes_per_thread_mb=spec.memory_model.bytes_per_thread_mb,
            threads=align_params.threads,
            sort_memory_mb=align_params.sort_memory_mb,
        )

        if buckets is None:
            # Single bucket — fall through to normal path below
            pass
        else:
            cache_dir = settings.tmp_dir / "chunked-refs"
            buckets = write_bucket_fastas(fasta_path, buckets, cache_dir)

            # The primary pair and every additional set were resolved and
            # validated once above, before either path branched -- the chunked
            # path returns before the single-shot code that used to redo the
            # primary mate resolution itself.
            mate_obj = primary_set.r2

            rg = align_runner.ReadGroup.from_dict(
                {**default_read_group(primary_set.r1), **(read_group or {})}
            )

            # Build the reads payload the same way the single-shot path
            # assembles it, so every field the align_reads handler expects is
            # present in the per-bucket sub-job.
            r1_digest, r1_path = await _resolve_readable(primary_set.r1)
            ref_digest, ref_path = await _resolve_readable(reference)

            reads_payload: dict = {
                "object_id": str(primary_set.r1.id),
                "project_id": str(primary_set.r1.project_id),
                "reference_object_id": str(reference.id),
                "reference_name": reference.name,
                "r1_name": primary_set.r1.name,
                "aligner": aligner.value,
                "params": align_params.as_dict(),
                "read_group": rg.as_dict(),
                "paired": paired,
                "output_name": _bam_name(primary_set.r1.name, rg.sample),
                "reads_object_id": str(primary_set.r1.id),
                "reference_id": str(reference.id),
                "reference_path": ref_path or str(fasta_path),
            }
            if ref_digest:
                reads_payload["reference_sha256"] = ref_digest
            if r1_digest:
                reads_payload["r1_sha256"] = r1_digest
            if r1_path:
                reads_payload["r1_path"] = r1_path

            # Additional read sets (same as single-shot path below): each set
            # is one entry carrying its R1 and, when paired, its mate, so the
            # handler can concatenate the R1s and R2s into their own streams.
            if extra_sets:
                extra_payload = []
                for extra_set in extra_sets:
                    entry = await _extra_set_payload(extra_set)
                    extra_payload.append(entry)
                reads_payload["extra_reads"] = extra_payload

            # Mate / R2 (same as single-shot path below).
            if mate_obj is not None:
                r2_digest, r2_path = await _resolve_readable(mate_obj)
                reads_payload["mate_object_id"] = str(mate_obj.id)
                reads_payload["r2_name"] = mate_obj.name
                if r2_digest:
                    reads_payload["r2_sha256"] = r2_digest
                if r2_path:
                    reads_payload["r2_path"] = r2_path

            reads_payload["owner"] = owner

            return await queue.enqueue(
                "align_reads_chunked",
                owner=owner,
                payload={
                    "bucket_specs": [
                        {"index": b.index, "sequences": b.sequences,
                         "total_bases": b.total_bases, "estimated_mb": b.estimated_mb,
                         "fasta_path": str(b.fasta_path)}
                        for b in buckets
                    ],
                    **reads_payload,
                },
                # Same attribution the single-shot path below gives its job.
                # Without it `/jobs?object_id=` cannot see this run, so every
                # "is anything running on this file?" surface -- the Actions
                # tab's Launch buttons most of all -- reports the file idle
                # for the hours a chunked alignment takes.
                project_id=primary_set.r1.project_id,
                object_id=primary_set.r1.id,
            )

    # --- Normal (single-shot) path ---

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

    # Resolved once and reused for both the index-build and align declared-vs-
    # budget checks below (#478 follow-up): calling it twice would be wasteful
    # and, worse, `resource_override=True` makes it unnecessary work entirely
    # since neither check will use it. Skipped in that case rather than
    # resolved and discarded.
    admission_budget_mb = (
        None if resource_override else await current_admission_budget_mb()
    )

    heuristic_mb = resource_estimator.estimate_mb(
        aligner=aligner,
        reference_bases=reference_bases_for(reference),
        threads=align_params.threads,
        sort_memory_mb=align_params.sort_memory_mb,
        building_index=building,
    )
    # Every set's R1 and R2 is concatenated into the streams the aligner
    # reads, so the real input is the sum of all of them -- not just the
    # primary's size.
    total_input_bytes = (
        sum(
            (s.r1.size or 0) + ((s.r2.size or 0) if s.r2 else 0) for s in read_sets
        )
    ) or None
    resolved = await memory_estimate.resolve(
        job_type=JOB_TYPE_ALIGN_READS,
        input_bytes=total_input_bytes,
        heuristic_mb=heuristic_mb,
        threads=align_params.threads,
    )
    estimate = resolved.mb
    # UNKNOWN cannot arise here (the heuristic always answers for an
    # alignment), but classifying None would be a crash rather than a refusal.
    if estimate is not None:
        band = resource_estimator.classify(
            estimated_mb=estimate,
            mem_budget_mb=mem_budget_mb,
            threads=align_params.threads,
            cpu_budget=governor.cpu_budget(),
        )
        # `resource_override` is the user's "Launch anyway" from the refusal
        # card. It skips the refusal here and rides on the job document to
        # claim.lua, which admits the job only when it is the sole occupant.
        if band is resource_estimator.Band.BLOCK and not resource_override:
            from app.services import replan_service

            proposal = replan_service.replan(
                job_type=JOB_TYPE_ALIGN_READS,
                # as_dict() doesn't carry reference_bases/building_index --
                # the proposer reads them directly, and AlignParams has no
                # reason to know about them.
                params={
                    **align_params.as_dict(),
                    "reference_bases": reference_bases_for(reference),
                    "building_index": building,
                },
                budget_mb=mem_budget_mb,
                cpu_budget=governor.cpu_budget() or 1,
            )
            raise ValidationError(
                resource_estimator.explain(
                    aligner=aligner,
                    reference_bases=reference_bases_for(reference),
                    threads=align_params.threads,
                    sort_memory_mb=align_params.sort_memory_mb,
                    building_index=building,
                    mem_budget_mb=mem_budget_mb,
                    provenance=resolved.detail,
                ),
                details={
                    "refusal": "estimate",
                    "estimate_mb": estimate,
                    "budget_mb": mem_budget_mb,
                    "estimate_source": resolved.source.value,
                    # The card names the source in prose; `detail` is the
                    # phrase resolve() already wrote for exactly that.
                    "detail": resolved.detail,
                    "replan": replan_service.as_payload(proposal),
                },
            )

    # The primary pair -- and with it the run's pairing mode -- was resolved
    # and validated above, before either path branched; the chunked path
    # returns before this code runs.
    mate = primary_set.r2

    rg = align_runner.ReadGroup.from_dict(
        {**default_read_group(primary_set.r1), **(read_group or {})}
    )

    # The record of what was asked for, created before anything is enqueued so
    # every job the launch produces can be linked to it as it is created.
    run = await run_service.create_run(
        kind=RunKind.ALIGNMENT,
        project_id=primary_set.r1.project_id,
        label=_alignment_label(
            primary_set.r1, primary_set.r2, reference, extra_sets
        ),
        inputs=_alignment_inputs(
            primary_set.r1, primary_set.r2, reference, extra_sets
        ),
        params={**align_params.as_dict(), "read_group": rg.as_dict()},
        # The caller's profile. Reads and reference were both resolved under
        # it, and they are required to share a project, so all three agree --
        # naming the caller states which of them is authoritative rather than
        # leaving a reader to work out that it does not matter.
        owner=owner,
        from_parameter_set=from_parameter_set,
    )

    # Build the index first if it is missing, and hold the alignment behind it.
    # `building` was already computed above for the resource guard -- same
    # underlying sidecar lookup, so reused here rather than queried twice.
    needs_index = building
    depends_on = []
    index_job = None
    if needs_index:
        # The index build declares MORE memory than the alignment itself for
        # the same reference (`declared_align_mem_mb`'s own docstring: a STAR
        # human index needs about 30 GB) -- so it must be checked here, before
        # it is enqueued, using the same mirrored kwargs `_enqueue_build_index`
        # uses internally. Checking only the align job's declaration below
        # would let this larger, unchecked reservation queue and wait forever
        # even when the align job's own check would have passed -- the exact
        # #478 symptom, reachable through this function's own index-build path.
        # All-or-nothing: raising here happens before either job is enqueued.
        index_build_mem_mb = await declared_align_mem_mb(
            aligner=aligner,
            reference_bases=reference_bases_for(reference),
            threads=INDEX_BUILD_THREADS,
            sort_memory_mb=0,
            building_index=True,
        )
        refuse_if_over_budget(
            declared_mb=index_build_mem_mb,
            budget_mb=admission_budget_mb or 0,
            resource_override=resource_override,
        )
        index_job = await _enqueue_build_index(reference, aligner, owner=owner)
        if index_job is not None:
            depends_on.append(index_job.id)
            await run_service.link_job(run.id, index_job.id, RunJobRole.INDEX)
        else:
            # Deduplicated away: an identical build is already queued or
            # running. Wait on *that* job rather than racing it.
            #
            # This lookup is not owner-filtered, and it does not need to be --
            # but the reason it is safe changed when the dedup key became
            # owner-scoped, so it is worth stating rather than leaving implicit.
            # It keys on `object_id`, a specific DataObject id, and objects are
            # owner-scoped, so two profiles holding the same genome necessarily
            # have distinct reference rows and cannot match each other here.
            # Before the key carried an owner this query was the thing that
            # turned a cross-profile collision into "wait for the other
            # profile's build" instead of no index at all; now `enqueue` never
            # produces that collision, and this branch is reached only for a
            # same-owner duplicate, which is exactly what it handles.
            existing = await Job.find_one(active_index_job_query(reference.id))
            if existing is not None:
                depends_on.append(existing.id)
                # Linked as shared: this run depends on the build but did not
                # cause it. Showing it as reused beats omitting it (a gap where
                # the index came from) or claiming credit for another run's work.
                await run_service.link_job(
                    run.id, existing.id, RunJobRole.INDEX, shared=True
                )

    # Any input that was offloaded is fetched first, and the alignment waits on
    # it via depends_on -- the same shape as the index build above. Done before
    # `_resolve_readable`, which refuses a remote object by design: the refusal
    # is the right answer for a caller that cannot queue work, and the wrong
    # one here, where fetching is exactly what we can do about it.
    #
    # The mate and the reference are included because any of the three can be
    # remote independently, and a launch that fetched only the R1 would fail
    # at the next resolve with the other two still missing.
    for _input in (primary_set.r1, mate, reference):
        if _input is None:
            continue
        _fetch_job_id = await ensure_local(_input, owner=owner)
        if _fetch_job_id is not None and _fetch_job_id not in depends_on:
            depends_on.append(_fetch_job_id)

    r1_digest, r1_path = await _resolve_readable(primary_set.r1)
    payload: dict = {
        "object_id": str(primary_set.r1.id),
        "project_id": str(primary_set.r1.project_id),
        "reference_object_id": str(reference.id),
        "reference_name": reference.name,
        "r1_name": primary_set.r1.name,
        "aligner": aligner.value,
        "params": align_params.as_dict(),
        "read_group": rg.as_dict(),
        "output_name": _bam_name(primary_set.r1.name, rg.sample),
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

    if extra_sets:
        extra_payload = []
        for extra_set in extra_sets:
            extra_payload.append(await _extra_set_payload(extra_set))
        payload["extra_reads"] = extra_payload

    expected = primary_set.r1.facts.get("read_count_estimate")
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

    # Every set member id, R1s then R2s, in order -- so two launches that
    # differ only in their additional sets are distinct jobs.
    dedup_key = "align:" + ":".join(
        [
            *(str(s.r1.id) for s in read_sets),
            *(str(s.r2.id) if s.r2 else "-" for s in read_sets),
            str(reference.id),
            _params_fingerprint(payload["params"]),
        ]
    )

    align_mem_mb = await declared_align_mem_mb(
        aligner=aligner,
        reference_bases=reference_bases_for(reference),
        threads=align_params.threads,
        sort_memory_mb=align_params.sort_memory_mb,
        building_index=False,
        input_bytes=total_input_bytes,
    )

    # The value actually enqueued, which is not the number the banding above
    # saw: it is recomputed with building_index=False and floored at
    # MIN_DECLARED_MEM_MB. Checking the enqueued value is the point (#478).
    refuse_if_over_budget(
        declared_mb=align_mem_mb,
        budget_mb=admission_budget_mb or 0,
        resource_override=resource_override,
    )

    job = await queue.enqueue(
        "align_reads",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        # The user's thread count, exactly as trim_reads declares it, and a
        # memory reservation from the estimator rather than a flat 8 GB that
        # was right for one aligner on one genome size.
        #
        # Recomputed with `building_index=False` rather than reusing the
        # `estimate` above, which is deliberately not the same number: that one
        # answers "can this whole operation fit on the host" and so includes
        # the index build, while this job only ever loads a finished index --
        # the build is a separate job with its own reservation. Reusing it
        # would reserve bowtie2's 3x and HISAT2's 4x build multiplier for
        # every alignment against a not-yet-indexed reference.
        resources=JobResources(
            cpu=align_params.threads,
            mem_mb=align_mem_mb,
            io=IoClass.HEAVY,
        ),
        max_attempts=2,
        dedup_key=dedup_key,
        project_id=primary_set.r1.project_id,
        object_id=primary_set.r1.id,
        depends_on=depends_on,
        resource_override=resource_override,
    )
    if job is None:
        # The run describes work that will not happen, so it must not linger in
        # the activity view claiming otherwise. The index build it may have
        # queued is left alone: that work is real and the earlier run owns it.
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "An identical alignment is already queued or running",
            details={"object_id": str(primary_set.r1.id)},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.ALIGN)

    log.info(
        "align_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        object_id=str(primary_set.r1.id),
        reference_id=str(reference.id),
        aligner=aligner.value,
        index_job_id=str(index_job.id) if index_job else None,
        waiting_on=[str(d) for d in depends_on],
    )
    return job


def _alignment_label(
    reads: DataObject,
    mate: DataObject | None,
    reference: DataObject,
    extra_sets: list[ReadSet] | None = None,
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
    if extra_sets:
        left += f" +{len(extra_sets)} read set{'s' if len(extra_sets) != 1 else ''}"
    return f"{left} → {reference.name}"


def _alignment_inputs(
    reads: DataObject,
    mate: DataObject | None,
    reference: DataObject,
    extra_sets: list[ReadSet] | None = None,
) -> list[RunInput]:
    inputs = [RunInput(object_id=reads.id, name=reads.name, role=RunInputRole.READS)]
    if mate is not None:
        inputs.append(RunInput(object_id=mate.id, name=mate.name, role=RunInputRole.MATE))
    for extra in extra_sets or []:
        # One entry per file, not per set: a set's R1 and mate are separate
        # files feeding separate streams, and a run's `inputs` has always
        # listed files. The roles say which stream each feeds.
        inputs.append(
            RunInput(
                object_id=extra.r1.id,
                name=extra.r1.name,
                role=RunInputRole.EXTRA_READS,
            )
        )
        if extra.r2 is not None:
            inputs.append(
                RunInput(
                    object_id=extra.r2.id,
                    name=extra.r2.name,
                    role=RunInputRole.EXTRA_MATE,
                )
            )
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


async def launch_bam_stats(*, object_id: PydanticObjectId, owner: str):
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
    from app.services import object_service

    tools.require(tools.samtools())

    bam = await object_service.get_object(object_id, owner=owner)
    _check_bam_stats_callable(bam)

    bai = await _sidecar_of_role(bam, SidecarRole.BAI)
    if bai is None:
        if not bam.blob_sha256:
            raise ValidationError(
                f"{bam.name!r} has no stored content yet (status={bam.status.value})"
            )
        index_job = await queue.enqueue(
            "index_bam",
            owner=owner,
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
        owner=owner,
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


async def launch_transcript_qc(
    *, object_id: PydanticObjectId, gtf_object_id: PydanticObjectId, owner: str
):
    """Queue RNA-seq transcript QC for a BAM against a chosen annotation.

    On demand rather than automatic: applicability is inferred (see
    services/transcript_qc_gating), and an automatic job on a mislabelled DNA
    BAM would burn a full pass to render a meaningless curve. The GTF is
    chosen by the caller rather than guessed, for the same reason.
    """
    from app.queue import queue
    from app.services import object_service
    from app.services.transcript_qc_gating import applicability

    bam = await object_service.get_object(object_id, owner=owner)
    _check_bam_stats_callable(bam)

    # pysam's .fetch() (used to walk the BAM per-contig) requires an index,
    # unlike run_bam_stats which shells out to samtools with -a. Missing
    # index is checked separately from _check_bam_stats_callable, matching
    # how launch_bam_stats/launch_variant_calling separate "wrong file type"
    # from "fixable precondition".
    bai = await _sidecar_of_role(bam, SidecarRole.BAI)
    if bai is None:
        raise ValidationError(
            f"{bam.name!r} has no index (.bai). Compute results (coverage) "
            "will index it automatically -- run that first, then retry "
            "transcript QC.",
            details={"object_id": str(bam.id)},
        )

    got = applicability({"metadata": bam.metadata, "facts": bam.facts})
    if not (got.gene_body or got.feature_distribution):
        raise ValidationError(
            "This BAM doesn't look like RNA-seq (checked molecule type, "
            "assay, and aligner) -- transcript QC isn't offered for it.",
            details={"object_id": str(bam.id)},
        )

    gtf = await object_service.get_object(gtf_object_id, owner=owner)
    if gtf.project_id != bam.project_id:
        raise ValidationError("The annotation must be in the same project as the BAM.")

    bam_digest, bam_path = await _resolve_readable(bam)
    bai_digest, bai_path = await _resolve_readable(bai)
    gtf_digest, gtf_path = await _resolve_readable(gtf)
    bam_path = bam_path or str(blob_path(bam_digest))
    bai_path = bai_path or str(blob_path(bai_digest))
    gtf_path = gtf_path or str(blob_path(gtf_digest))

    job = await queue.enqueue(
        "run_transcript_qc",
        owner=owner,
        payload={
            "object_id": str(bam.id),
            "project_id": str(bam.project_id),
            "bam_name": bam.name,
            "bam_path": bam_path,
            "bai_path": bai_path,
            "gtf_path": gtf_path,
            "gtf_name": gtf.name,
        },
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
        max_attempts=2,
        # Keyed by both, so re-running against a different annotation is a
        # different job rather than a silently deduped no-op.
        dedup_key=f"transcriptqc:{bam.id}:{gtf.id}",
        project_id=bam.project_id,
        object_id=bam.id,
    )
    if job is None:
        raise ConflictError(
            "Transcript QC is already queued or running for this file",
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


async def launch_vcf_stats(*, object_id: PydanticObjectId, owner: str):
    """Queue the Results computation for a VCF: call-set summary statistics
    and the per-variant table.

    Read-only, like launch_bam_stats: no derived objects, just facts merged
    onto the object plus a TSV and a SQLite database on disk.
    """
    from app.queue import queue
    from app.services import object_service

    tools.require(tools.bcftools())

    vcf = await object_service.get_object(object_id, owner=owner)
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
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"vcf_stats:{vcf.id}",
        project_id=vcf.project_id,
        object_id=vcf.id,
    )


_ANNOTATION_STATS_FORMATS = (
    FormatKind.GFF,
    FormatKind.GTF,
    FormatKind.BED,
    FormatKind.GENBANK,
)


def _check_annotation_stats_callable(obj) -> None:
    """Whether an Annotation Results computation can run against this object.

    Refuse early and with a reason a person can act on, the same posture
    `_check_vcf_stats_callable` documents.
    """
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready for results (status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind not in _ANNOTATION_STATS_FORMATS:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not an annotation file "
            f"(GFF, GTF, BED, or GenBank)",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )


def should_auto_analyze_annotation(
    *, kind: FormatKind, sidecar_of, facts: dict
) -> bool:
    """Whether ingest should analyze this object without being asked.

    Public and parameter-wise rather than object-wise so both trigger sites
    and their tests can call it without constructing a DataObject.

    The sidecar exclusion is load-bearing, not defensive. Every object on
    this machine's real database whose format.kind is BED is a `.fai` or a
    STAR `.ann` index that the detector called BED -- 8 of them. A `.fai` is
    not an annotation however it parses, and analyzing one writes a database
    of garbage intervals under a name nobody will recognize.

    A file already analyzed *with* a reference is skipped; one analyzed
    without a reference is not, because that is exactly the result trigger 2
    exists to repair.
    """
    if kind not in _ANNOTATION_STATS_FORMATS:
        return False
    if sidecar_of is not None:
        return False
    if not facts.get("annotation_stats_status"):
        return True
    return facts.get("annotation_contig_lengths_known") is not True


async def launch_annotation_stats(*, object_id: PydanticObjectId, owner: str):
    """Queue the Results computation for a GFF/GTF/BED.

    Read-only, like launch_vcf_stats: no derived objects, just facts merged
    onto the object plus a SQLite database on disk.

    Requires no external tool -- the parse is pure Python -- so unlike the
    variant path there is no `tools.require` here.
    """
    from app.queue import queue
    from app.services import object_service

    ann = await object_service.get_object(object_id, owner=owner)
    _check_annotation_stats_callable(ann)

    digest, path = await _resolve_readable(ann)
    # run_annotation_stats (HandlerMode.THREAD) reads ctx.payload["annotation_path"]
    # directly -- unlike the SUBPROCESS handlers, it has no digest-based blob
    # resolution of its own, so the launcher must always hand it a real path.
    path = path or str(blob_path(digest))

    # Contig lengths for the coverage denominators AND the track viewer's
    # axis. Taken from the annotation's own facts when ingest recorded them,
    # else from `resolve_annotation_reference`'s two tiers (provenance, then
    # matching NCBI assembly accession) -- not `_reference_for_annotation`
    # alone, which only covers the first tier. An annotation downloaded from
    # NCBI with no `derived_from` (the common case: nothing in-app derived
    # it) would otherwise never resolve a reference here even though the
    # accession match succeeds everywhere else this resolver is used. Absent
    # is fine: coverage is reported as null rather than zero for a contig of
    # unknown length, and the track viewer shows its own refusal reason.
    lengths = ann.facts.get("reference_lengths") or {}
    if not lengths:
        resolved = await resolve_annotation_reference(ann)
        if resolved.reference is not None:
            lengths = resolved.reference.facts.get("sequence_lengths") or {}

    payload: dict = {
        "object_id": str(ann.id),
        "project_id": str(ann.project_id),
        "format_kind": str(ann.format.kind.value),
        "contig_lengths": [[name, length] for name, length in lengths.items()],
        # Recorded rather than re-derived from contig_lengths downstream:
        # ingest's backfill queries this as a flat field, and an $elemMatch
        # over annotation_per_contig could not tell "no reference resolved"
        # apart from "reference resolved but missing this contig".
        "contig_lengths_known": bool(lengths),
        "annotation_path": path,
    }
    if digest:
        payload["annotation_sha256"] = digest

    return await queue.enqueue(
        "run_annotation_stats",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"annotation_stats:{ann.id}",
        project_id=ann.project_id,
        object_id=ann.id,
    )


async def ensure_annotation_stats(
    *, object_id: PydanticObjectId, owner: str
) -> PydanticObjectId | None:
    """Compute the results sidecar if this annotation has none.

    Returns the job id the caller must depend on, or `None` if the sidecar
    already exists and nothing needs to run. `export_annotation_subset`
    raises PermanentError without `features.db`, and only
    `launch_annotation_stats` writes one -- the canvas export node holds
    itself behind whatever this returns, via `depends_on`, so a graph cannot
    fail purely for want of a precomputed sidecar. This mirrors
    `launch_alignment` holding an alignment behind an unindexed reference's
    `build_index` job: both are THREAD handlers on the same worker pool, so
    merely enqueueing the prerequisite first is not enough to order them --
    only a real `depends_on` on the queue does that.

    A no-op when the database is already on disk, which is the common case:
    ingest analyzes annotations automatically.
    """
    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if db_path.exists():
        return None

    job = await launch_annotation_stats(object_id=object_id, owner=owner)
    if job is not None:
        return job.id

    # Deduplicated away: an identical stats job is already queued or
    # running for this object. Depend on *that* one rather than racing it.
    existing = await Job.find_one(active_annotation_stats_job_query(object_id))
    return existing.id if existing is not None else None


async def launch_annotation_export(
    *,
    object_id: PydanticObjectId,
    owner: str,
    filters: dict,
    output_name: str,
    depends_on: list[PydanticObjectId] | None = None,
):
    """Queue a subset export for a GFF/GTF/BED annotation.

    Unlike launch_annotation_stats this *does* derive an object, so the job's
    result goes through _apply_export_annotation_subset.

    `depends_on` holds this job back until every listed job succeeds -- the
    canvas export node passes the id `ensure_annotation_stats` returned, the
    same way `launch_alignment` threads its index-build job id through here.
    """
    from app.queue import queue

    ann = await object_service.get_object(object_id, owner=owner)
    _check_annotation_stats_callable(ann)

    # A node left with the name box blank still has to produce a sensibly
    # named object rather than failing on a missing argument.
    if not output_name:
        output_name = f"{ann.name} (subset)"

    digest, path = await _resolve_readable(ann)
    # Same reasoning as launch_annotation_stats: the THREAD handler reads
    # ctx.payload["annotation_path"] directly and does no blob resolution.
    path = path or str(blob_path(digest))

    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"

    return await queue.enqueue(
        "export_annotation_subset",
        owner=owner,
        payload={
            "object_id": str(ann.id),
            "annotation_path": path,
            "db_path": str(db_path),
            "format_kind": str(ann.format.kind.value),
            "filters": filters,
            "output_name": output_name,
        },
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
        max_attempts=2,
        project_id=ann.project_id,
        object_id=ann.id,
        depends_on=depends_on or [],
    )


async def launch_materialize_annotation_edits(
    *, object_id: PydanticObjectId, owner: str
):
    """Queue materialization of pending annotation edits into a derived object.

    Issue #297. The handler reads all AnnotationEdit documents for the source
    object, rewrites the edited columns in each tagged source line, and writes
    a derived annotation object.
    """
    from app.models.annotation_edit import AnnotationEdit
    from app.queue import queue

    ann = await object_service.get_object(object_id, owner=owner)
    _check_annotation_stats_callable(ann)

    digest, path = await _resolve_readable(ann)
    path = path or str(blob_path(digest))

    edits = await AnnotationEdit.find(
        AnnotationEdit.object_id == ann.id
    ).to_list()
    if not edits:
        raise ValidationError("No pending edits to materialize")

    return await queue.enqueue(
        "materialize_annotation_edits",
        owner=owner,
        payload={
            "object_id": str(ann.id),
            "annotation_path": path,
            "annotation_name": ann.name,
            "format_kind": str(ann.format.kind.value),
            "project_id": str(ann.project_id),
        },
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
        max_attempts=2,
        project_id=ann.project_id,
        object_id=ann.id,
    )


async def existing_extracted_sequence(
    object_id: PydanticObjectId,
) -> DataObject | None:
    """The FASTA reference already extracted from this GenBank, if any.

    The whole of #348's idempotency guard. Deliberately a query rather than a
    fact on the GenBank: a stored "already extracted" flag would go stale the
    moment the user deleted the reference, and deletion here is hard, not
    soft (`object_service.delete_object`), so asking the database is both
    simpler and self-healing. Keyed on derived_from and role, never on name,
    so renaming the reference does not produce a second extraction.

    Filtered by role because an exported annotation subset is also
    `derived_from` the same GenBank; only a REFERENCE is this file's
    extracted sequence.
    """
    return await DataObject.find(
        DataObject.derived_from == object_id,
        DataObject.role == ObjectRole.REFERENCE,
    ).first_or_none()


async def launch_extract_genbank_sequence(
    *, object_id: PydanticObjectId, owner: str
):
    """Queue extraction of a GenBank's ORIGIN sequence into a FASTA reference.

    Derives an object, so the job's result goes through
    `_apply_extract_genbank_sequence`. Refuses rather than queueing when one
    already exists: extraction takes no parameters, so a second run would
    write a byte-identical duplicate of a possibly very large reference and
    put two indistinguishable entries in every picker.
    """
    from app.queue import queue

    gb = await object_service.get_object(object_id, owner=owner)
    _check_annotation_stats_callable(gb)

    if gb.format.kind is not FormatKind.GENBANK:
        raise ValidationError(
            f"{gb.name!r} is {gb.format.kind.value}, not a GenBank file",
            details={"object_id": str(gb.id), "kind": gb.format.kind.value},
        )

    existing = await existing_extracted_sequence(gb.id)
    if existing is not None:
        raise ConflictError(
            f"{gb.name!r} already has an extracted sequence: {existing.name!r}",
            details={
                "object_id": str(gb.id),
                "reference_id": str(existing.id),
                "reference_name": existing.name,
            },
        )

    digest, path = await _resolve_readable(gb)
    # Same reasoning as launch_annotation_stats: the THREAD handler reads
    # ctx.payload["genbank_path"] directly and does no blob resolution.
    path = path or str(blob_path(digest))

    stem = Path(gb.name).stem
    # A .gbff.gz leaves a .gbff behind after one stem strip.
    if stem.endswith(".gbff") or stem.endswith(".gb"):
        stem = Path(stem).stem

    return await queue.enqueue(
        "extract_genbank_sequence",
        owner=owner,
        payload={
            "object_id": str(gb.id),
            "genbank_path": path,
            "output_name": f"{stem}.fna",
        },
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"genbank_sequence:{gb.id}",
        project_id=gb.project_id,
        object_id=gb.id,
    )


async def _reference_for_annotation(ann) -> DataObject | None:
    """The reference this annotation describes, from its provenance.

    Prefers an explicit REFERENCE role over bare FASTA format, matching
    `reference_for_bam` below: an annotation's parents may include a protein
    or CDS FASTA downloaded alongside the genome, and returning one of those
    gives the track viewer a coordinate axis for the wrong sequence.

    Best-effort: an annotation with no recorded reference still computes
    #257's stats, reporting per-contig counts without coverage fractions.
    """
    fallback: DataObject | None = None
    for parent_id in (ann.derived_from or []):
        parent = await DataObject.get(parent_id)
        if parent is None or parent.format.kind is not FormatKind.FASTA:
            continue
        if parent.role is ObjectRole.REFERENCE:
            return parent
        fallback = fallback or parent
    return fallback


@dataclass(frozen=True)
class AnnotationReference:
    """Which reference supplies an annotation's axis, or why none does.

    A reason rather than a bare None: the viewer tells the user what would
    fix it, since "no reference" is actionable and a blank panel is not.
    """

    reference: DataObject | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        assert (self.reference is None) != (self.reason is None), (
            "AnnotationReference must set exactly one of reference/reason"
        )


async def resolve_annotation_reference(ann) -> AnnotationReference:
    """The reference whose coordinates this annotation is drawn against.

    Two tiers, then refusal. The axis is a claim about what the coordinates
    mean, so a guess is worse than nothing: a wrong reference draws a ruler
    of the wrong length with features positioned against it, which looks
    authoritative and is false.

    Tier 1 is explicit provenance. Tier 2 matches `ncbi_assembly_accession`,
    the fact NCBI lookups write on both the genome and the annotation
    downloaded with it.

    Accessions compare by exact string equality: GCF_000001405.39 and .40 are
    different assemblies, and a GCA counterpart is a different record.

    Tier 2 requires `ObjectRole.REFERENCE`, not just `FormatKind.FASTA`. An
    NCBI genome download brings `protein.faa` and `cds_from_genomic.fna`
    alongside the genome FASTA, and all three carry the same accession --
    verified against this machine's real database, where filtering on format
    alone resolved 3 of 5 real annotations to `protein.faa` instead of the
    genome, because `list_objects`'s newest-first order has no reason to
    prefer one FASTA role over another. This is the same class of bug
    CLAUDE.md documents for `suggestion_service.py` counting `protein.faa`
    as an alignable reference -- and it was invisible to every unit test here
    because no fixture provided more than one same-accession FASTA candidate.
    """
    reference = await _reference_for_annotation(ann)
    if reference is not None:
        return AnnotationReference(reference=reference)

    accession = (ann.facts or {}).get("ncbi_assembly_accession")
    if accession:
        candidates = await object_service.list_objects(
            ann.project_id, owner=ann.owner, limit=500, status=ObjectStatus.READY
        )
        for obj in candidates:
            if obj.format.kind is not FormatKind.FASTA:
                continue
            if obj.role is not ObjectRole.REFERENCE:
                continue
            if obj.facts.get("ncbi_assembly_accession") == accession:
                return AnnotationReference(reference=obj)

    return AnnotationReference(
        reason=(
            f"No reference resolved for {ann.name}. Its provenance records no "
            "genome, and no reference in this project carries a matching NCBI "
            "assembly accession. The feature table and summary charts are "
            "still available."
        )
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


async def alignments_against(
    assembly: DataObject, *, owner: str
) -> tuple[list[DataObject], list[DataObject], list[DataObject]]:
    """BAMs aligned against this assembly, split by read chemistry.

    Returns `(short, long, unknown)`. The reverse of `reference_for_bam`:
    an alignment records its reference in `derived_from`, so "was this BAM
    aligned to this assembly?" is a lookup rather than a guess, and an
    uploaded BAM with no provenance is correctly excluded.

    `unknown` is returned rather than folded into `short`. Callers must
    refuse it: `read_chemistry_for_alignment` falls back to a short-read
    default for picking an alignment preset, which is right there and wrong
    here -- passing long reads as `-ngs` misdescribes the evidence rather
    than degrading.
    """
    from app.services import object_service

    candidates = [
        o
        for o in await object_service.list_objects(
            assembly.project_id, owner=owner, status=ObjectStatus.READY
        )
        if o.format.kind is FormatKind.BAM and assembly.id in o.derived_from
    ]

    short: list[DataObject] = []
    long_: list[DataObject] = []
    unknown: list[DataObject] = []
    for bam in candidates:
        chemistry = await read_chemistry_for_alignment(bam)
        if chemistry is align_runner.ReadChemistry.SHORT:
            short.append(bam)
        elif chemistry in (
            align_runner.ReadChemistry.HIFI,
            align_runner.ReadChemistry.CLR,
            align_runner.ReadChemistry.ONT_SIMPLEX,
            align_runner.ReadChemistry.ONT_DUPLEX,
        ):
            long_.append(bam)
        else:
            unknown.append(bam)
    return short, long_, unknown


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

    for sidecar in await object_service.list_sidecars(obj.id, owner=obj.owner):
        if sidecar.sidecar_role is role and sidecar.blob_sha256:
            return sidecar
    return None


async def _require_or_offer_install(
    tool, *, owner: str, install_optional: bool
) -> PydanticObjectId | None:
    """Assert a caller is usable, or -- for an on-demand tool that simply has
    not been pulled yet -- offer to install it instead of refusing outright.

    Returns the id of an install job the launch should `depends_on`, or
    `None` when nothing needs to be queued (the tool was already available).

    Three outcomes, matching `tools.InstallState`:

    - `INSTALLED` (or any BUNDLED tool, whose `install_state` is always
      `None`): nothing to do, `tools.require` passes it straight through.
    - `NOT_INSTALLED`, no consent: raise `ValidationError` naming the tool
      and its download size, in the same `details={"needs": ...}` shape the
      `.bai`/`.fai` refusals above already use -- the dialog reads `needs`
      to decide what to offer, and "install this tool" belongs in the same
      vocabulary as "index this BAM first" rather than a new one.
    - `NOT_INSTALLED`, with consent: enqueue the install job
      (`tool_install_service.install`, which already deduplicates an
      in-flight install for the same tool) and return its id, so the caller
      can chain `call_variants` behind it with `depends_on`. Genuinely
      broken (`UNKNOWN`, or unavailable with no install_state at all) still
      raises the ordinary `require()` error -- pressing "install" cannot fix
      an unreachable daemon, so that path must not pretend it can.
    """
    if tool.available:
        tools.require(tool)
        return None

    if tool.install_state is tools.InstallState.NOT_INSTALLED:
        meta = tools.TOOL_META.get(tool.name)
        if not install_optional:
            raise ValidationError(
                f"{tool.name} is not installed. It runs as a separate "
                f"container image and is downloaded on first use "
                f"(about {_format_gb(meta.download_bytes) if meta else '?'}).",
                details={
                    "tool": tool.name,
                    "needs": "install_tool",
                    "download_bytes": meta.download_bytes if meta else None,
                },
            )

        from app.services import tool_install_service

        job = await tool_install_service.install(tool_name=tool.name, owner=owner)
        return job.id

    # UNKNOWN, or unavailable with no install_state at all (every BUNDLED
    # tool that is simply missing/broken) -- a genuine fault. require()
    # raises PermanentError with the probe's own reason.
    tools.require(tool)
    return None


def _format_gb(download_bytes: int | None) -> str:
    if not download_bytes:
        return "a few GB"
    return f"{download_bytes / 1_000_000_000:.1f} GB"


async def launch_variant_calling(
    *,
    bam_id: PydanticObjectId,
    owner: str,
    reference_id: PydanticObjectId | None = None,
    caller: str | None = None,
    params: dict | None = None,
    # Consent to a multi-gigabyte download. Distinct from every other launch
    # parameter here because it is not a choice about *what* to run -- it is
    # permission to spend bandwidth the user has not yet agreed to. Without
    # it, launch_variant_calling refuses naming the size (see
    # _require_or_offer_install); the dialog re-posts with this set once the
    # user has actually seen and accepted that number.
    install_optional: bool = False,
    resource_override: bool = False,
):
    """Queue a variant calling run over an aligned BAM.

    Unlike alignment, this does not build its missing indexes: it requires the
    `.bai` and the reference `.fai` to exist and refuses otherwise. Both are
    produced by jobs the user has already run (`index_bam`, `build_index`), and
    an actionable "run index_bam first" beats a job that sits blocked behind
    work the user did not ask for.
    """
    from app.queue import queue
    from app.services import object_service

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=VARIANT_CALLING_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    bam = await object_service.get_object(bam_id, owner=owner)
    _check_variant_callable(bam)

    # Refuse CLR before anything is enqueued. Raises ValidationError naming the
    # alternative; the dialog renders it rather than offering a caller.
    chemistry = await read_chemistry_for_alignment(bam)
    if chemistry is not None:
        variant_runner.caller_for_chemistry(chemistry)

    reference = await _resolve_variant_reference(bam, reference_id, owner=owner)

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
    install_job_id = None
    if merged.caller is variant_runner.VariantCaller.CLAIR3:
        tools.require(tools.clair3())
    elif merged.caller is variant_runner.VariantCaller.DEEPVARIANT:
        install_job_id = await _require_or_offer_install(
            tools.deepvariant(), owner=owner, install_optional=install_optional
        )
    else:
        tools.require(tools.bcftools())
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
        owner=owner,
        tool=merged.caller.value,
    )

    job = await queue.enqueue(
        "call_variants",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(
            cpu=merged.threads, mem_mb=VARIANT_CALLING_MEM_MB, io=IoClass.HEAVY
        ),
        max_attempts=2,
        dedup_key=_variant_dedup_key(bam_id=bam.id, params=merged.as_dict()),
        project_id=bam.project_id,
        object_id=bam.id,
        # The .bai and .fai are required above, so ordinarily there is
        # nothing left to wait for -- except when _require_or_offer_install
        # queued a pull for an on-demand caller (DeepVariant, with consent).
        # A failed install fails this job too rather than leaving it blocked
        # forever: queue.py's _failed_dependencies already covers that, the
        # same mechanism launch_alignment's index-build dependency relies on.
        depends_on=[install_job_id] if install_job_id is not None else [],
        resource_override=resource_override,
    )
    if job is None:
        await run_service.discard_run(run.id, owner=run.owner)
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
    bam: DataObject, reference_id: PydanticObjectId | None, *, owner: str
) -> DataObject:
    """The reference to call against: explicit if given, else the BAM's own.

    Only the explicit branch needs `owner`: it is the one taking an id straight
    from the request body. The inferred branch walks the BAM's own provenance,
    which cannot leave the profile the BAM is already in.
    """
    from app.services import object_service

    if reference_id is not None:
        reference = await object_service.get_object(reference_id, owner=owner)
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


# --- Structural variant calling ----------------------------------------------

STRUCTURAL_VARIANT_CALLING_MEM_MB = 8192


def _sv_dedup_key(*, bam_id, params: dict) -> str:
    """Identity of a structural variant calling request.

    No caller in the key, unlike `_variant_dedup_key`: Sniffles2 is the only
    SV caller this pipeline runs, so the params fingerprint alone identifies
    the request.
    """
    return f"call_structural_variants:{bam_id}:{_params_fingerprint(params)}"


async def _sv_payload(
    *,
    bam: DataObject,
    reference: DataObject,
    bai: DataObject,
    fai: DataObject,
    params: sniffles_runner.SnifflesParams,
) -> dict:
    """The call_structural_variants payload, with every input addressed by
    digest or path -- mirrors `_variant_payload`."""
    payload: dict = {
        "bam_object_id": str(bam.id),
        "reference_object_id": str(reference.id),
        "project_id": str(bam.project_id),
        "bam_name": bam.name,
        "reference_name": reference.name,
        "params": params.as_dict(),
        "output_name": f"{Path(bam.name).stem}.sniffles.vcf.gz",
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

    return payload


async def launch_structural_variant_calling(
    *,
    bam_id: PydanticObjectId,
    params: dict | None,
    owner: str,
    resource_override: bool = False,
):
    """Queue a Sniffles2 structural variant calling run over an aligned BAM.

    Mirrors `launch_variant_calling`'s structure: requires the `.bai` and the
    reference `.fai` to already exist rather than building them, and refuses
    up front rather than letting a doomed job reach the worker. The chemistry
    gate is the one thing this launcher checks that `launch_variant_calling`
    does not -- Sniffles2 is a long-read caller, and a short-read BAM would
    otherwise produce a junk callset with nothing saying so. Checked here, at
    the one place the API is actually reachable, rather than only on the
    suggestion card, which is advisory and skippable.
    """
    from app.queue import queue
    from app.services import object_service

    refuse_if_over_budget(
        declared_mb=STRUCTURAL_VARIANT_CALLING_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    bam = await object_service.get_object(bam_id, owner=owner)
    _check_variant_callable(bam)

    chemistry = await read_chemistry_for_alignment(bam)
    if not sniffles_runner.sv_calling_allowed_for(
        chemistry or align_runner.ReadChemistry.UNKNOWN
    ):
        raise ValidationError(
            f"{bam.name!r} does not look like long reads "
            f"(chemistry={chemistry.value if chemistry else 'unknown'}). "
            f"Structural variant calling needs a long-read alignment.",
            details={
                "bam_id": str(bam.id),
                "chemistry": chemistry.value if chemistry else None,
            },
        )

    reference = await _resolve_variant_reference(bam, None, owner=owner)

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

    tools.require(tools.sniffles())

    merged = sniffles_runner.SnifflesParams.from_dict(params)

    payload = await _sv_payload(
        bam=bam, reference=reference, bai=bai, fai=fai, params=merged
    )

    run = await run_service.create_run(
        kind=RunKind.STRUCTURAL_VARIANT_CALLING,
        project_id=bam.project_id,
        label=f"{bam.name} → structural variants (sniffles2)",
        inputs=[
            RunInput(object_id=bam.id, name=bam.name, role=RunInputRole.READS),
            RunInput(
                object_id=reference.id,
                name=reference.name,
                role=RunInputRole.REFERENCE,
            ),
        ],
        params=merged.as_dict(),
        owner=owner,
        tool="sniffles2",
    )

    job = await queue.enqueue(
        "call_structural_variants",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(
            cpu=merged.threads,
            mem_mb=STRUCTURAL_VARIANT_CALLING_MEM_MB,
            io=IoClass.HEAVY,
        ),
        max_attempts=2,
        dedup_key=_sv_dedup_key(bam_id=bam.id, params=merged.as_dict()),
        project_id=bam.project_id,
        object_id=bam.id,
    )
    if job is None:
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "An identical structural variant calling run is already queued "
            "or running",
            details={"bam_id": str(bam.id)},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.CALL_STRUCTURAL_VARIANTS)
    log.info(
        "structural_variant_calling_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        bam_id=str(bam.id),
    )
    return job


def _sv_merge_dedup_key(*, snf_ids: list[PydanticObjectId]) -> str:
    sorted_ids = sorted(str(i) for i in snf_ids)
    return f"merge_structural_variants:{','.join(sorted_ids)}"


async def launch_merge_structural_variants(
    *,
    snf_object_ids: list[PydanticObjectId],
    owner: str,
    output_name: str | None = None,
    resource_override: bool = False,
):
    """Queue a Sniffles2 --combine run across N single-sample .snf callsets."""
    from app.queue import queue
    from app.services import object_service

    if not snf_object_ids or len(snf_object_ids) < 2:
        raise ValidationError("At least two .snf callsets are required for merging")

    refuse_if_over_budget(
        declared_mb=STRUCTURAL_VARIANT_CALLING_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    snf_objects: list[DataObject] = []
    for sid in snf_object_ids:
        obj = await object_service.get_object(sid, owner=owner)
        if obj.sidecar_role != SidecarRole.SNF:
            raise ValidationError(f"{obj.name!r} is not an SNF sidecar file")
        snf_objects.append(obj)

    project_id = snf_objects[0].project_id
    for obj in snf_objects[1:]:
        if obj.project_id != project_id:
            raise ValidationError("All .snf callsets must belong to the same project")

    references: dict[str, tuple[str, PydanticObjectId]] = {}
    for obj in snf_objects:
        parent_vcf = await DataObject.get(obj.sidecar_of) if obj.sidecar_of else None
        ref_obj = None
        if parent_vcf and parent_vcf.derived_from:
            for p_id in parent_vcf.derived_from:
                p = await DataObject.get(p_id)
                if p and p.role == ObjectRole.REFERENCE:
                    ref_obj = p
                    break
        if ref_obj is not None:
            references[obj.name] = (ref_obj.name, ref_obj.id)

    ref_ids = {r[1] for r in references.values()}
    if len(ref_ids) > 1:
        ref_details = ", ".join(f"'{name}': reference '{ref[0]}'" for name, ref in references.items())
        raise ValidationError(
            f"Cannot merge SV callsets across differing reference assemblies: {ref_details}.",
            details={"references": {name: str(ref[1]) for name, ref in references.items()}},
        )

    ref_id = next(iter(ref_ids)) if ref_ids else None

    tools.require(tools.sniffles())

    snf_blobs: list[str] = []
    snf_names: list[str] = []
    for obj in snf_objects:
        _, path = await _resolve_readable(obj)
        if path:
            snf_blobs.append(path)
            snf_names.append(obj.name)

    payload = {
        "snf_object_ids": [str(i) for i in snf_object_ids],
        "reference_object_id": str(ref_id) if ref_id else None,
        "project_id": str(project_id),
        "snf_blobs": snf_blobs,
        "snf_names": snf_names,
        "output_name": output_name or "joint_calls.sniffles.vcf.gz",
    }

    run = await run_service.create_run(
        kind=RunKind.STRUCTURAL_VARIANT_CALLING,
        project_id=project_id,
        label=f"Merge {len(snf_objects)} SV callsets",
        inputs=[
            RunInput(object_id=obj.id, name=obj.name, role=RunInputRole.VARIANTS)
            for obj in snf_objects
        ],
        params={},
        owner=owner,
        tool="sniffles2",
    )

    job = await queue.enqueue(
        "merge_structural_variants",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(
            cpu=4, mem_mb=STRUCTURAL_VARIANT_CALLING_MEM_MB, io=IoClass.HEAVY
        ),
        max_attempts=2,
        dedup_key=_sv_merge_dedup_key(snf_ids=snf_object_ids),
        project_id=project_id,
        object_id=snf_objects[0].id,
        resource_override=resource_override,
    )
    if job is None:
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "An identical SV merge run is already queued or running",
            details={"snf_ids": [str(i) for i in snf_object_ids]},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.CALL_VARIANTS)
    log.info("merge_structural_variants_launched", job_id=str(job.id), count=len(snf_objects))
    return job


# --- Consequence annotation --------------------------------------------------

# ObjectRole.ANNOTATION spans GFF3, GTF and BED -- the role records intent
# and cannot say which interval format a file actually is. `bcftools csq`
# reads GFF3 only, so a BED blacklist tagged ANNOTATION would launch a job
# that dies in the worker on a parse error.
ANNOTATION_KINDS = {FormatKind.GFF}


async def taxid_for_vcf(vcf: DataObject) -> int | None:
    """The NCBI taxonomy ID of the organism a VCF was called against.

    Scopes the gene lookup behind the structure viewer. The taxid is not an
    optimisation there: gene symbols collide across organisms far more often
    than within one, so an unscoped query can return an unrelated protein
    with a residue highlighted at a position that means nothing. None
    therefore means "do not query", never "query without a species".

    `tax_id` is read from the reference's `metadata`, where the NCBI download
    path puts it -- not `facts`, which holds the assembly accession and the
    published statistics. All seven reference objects on this machine carry it
    in metadata and none in facts.

    Returns None for a reference that was never enriched from NCBI. The
    organism *name* is often still present, but resolving a name to a taxid is
    another remote lookup with its own failure modes, and a bare species name
    does not distinguish the strain a callset actually used.
    """
    for parent_id in vcf.derived_from or []:
        parent = await DataObject.get(parent_id)
        if parent is None or parent.role is not ObjectRole.REFERENCE:
            continue
        metadata = parent.metadata if isinstance(parent.metadata, dict) else {}
        raw = metadata.get("tax_id")
        if raw is None:
            continue
        # Metadata is hand-editable, so this field can hold anything. A bool
        # is excluded deliberately -- `int(True)` is 1, a real taxid.
        if isinstance(raw, bool):
            continue
        try:
            taxid = int(raw)
        except (TypeError, ValueError):
            continue
        return taxid if taxid > 0 else None
    return None


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
        vcf.project_id, owner=vcf.owner, limit=500, status=ObjectStatus.READY
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


async def launch_annotation(*, object_id: PydanticObjectId, owner: str):
    """Queue a consequence-annotation run for a VCF.

    Resolution goes through `resolve_annotation_inputs` rather than repeating
    the rules, so a launch cannot succeed where the card said it could not, or
    the reverse.
    """
    from app.queue import queue
    from app.services import object_service

    tools.require(tools.bcftools_csq())

    vcf = await object_service.get_object(object_id, owner=owner)

    inputs = await resolve_annotation_inputs(vcf)
    if not inputs.ok:
        raise ValidationError(inputs.reason)

    # The handler stages the reference the same way call_variants does --
    # materialized as a real sibling of its .fai, not a symlink `samtools
    # faidx` would resolve through -- so the .fai has to exist and be
    # resolvable here, exactly as launch_variant_calling requires one.
    fai = await _sidecar_of_role(inputs.reference, SidecarRole.FAI)
    if fai is None:
        raise ValidationError(
            f"Reference {inputs.reference.name!r} has no FASTA index (.fai). "
            f"Build its index first.",
            details={"reference_id": str(inputs.reference.id), "needs": "build_index"},
        )

    payload: dict = {
        "object_id": str(vcf.id),
        "reference_object_id": str(inputs.reference.id),
        "annotation_object_id": str(inputs.annotation.id),
        "project_id": str(vcf.project_id),
        "vcf_name": vcf.name,
        "reference_name": inputs.reference.name,
        "annotation_name": inputs.annotation.name,
    }
    for key, obj in (
        ("vcf", vcf),
        ("reference", inputs.reference),
        ("annotation", inputs.annotation),
        ("fai", fai),
    ):
        digest, path = await _resolve_readable(obj)
        if digest:
            payload[f"{key}_sha256"] = digest
        if path:
            payload[f"{key}_path"] = path

    return await queue.enqueue(
        "annotate_variants",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"annotate:{vcf.id}",
        project_id=vcf.project_id,
        object_id=vcf.id,
    )


# --- Expression: quantification and differential testing --------------------


def _check_quantifiable(obj: DataObject) -> None:
    """Whether a file can be counted.

    Format only. Whether the BAM is *RNA-seq* is not knowable from the bytes
    and is not checked: counting a DNA alignment against a gene annotation is
    a strange thing to do but not a wrong one, and refusing it would mean
    refusing legitimate uses (targeted panels, CDS-level coverage) to prevent
    a mistake the assignment rate reports anyway.
    """
    kind = obj.format.kind
    if kind not in (FormatKind.BAM, FormatKind.SAM, FormatKind.CRAM):
        raise ValidationError(
            f"{obj.name!r} is {kind.value if kind else 'an unknown format'}, "
            "not an alignment. Counting needs an aligned BAM.",
            details={"object_id": str(obj.id), "kind": str(kind)},
        )
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready ({obj.status.value}).",
            details={"object_id": str(obj.id)},
        )


def _is_annotation(obj: DataObject) -> bool:
    """Whether an object is a gene annotation featureCounts can read.

    Format first, role second -- and this is the way round that matters. Every
    annotation in a real project arrives from `download_assembly` with
    `role=None`, because the format already says GFF/GTF and the ingest path
    only sets a role where format cannot answer. A rule written as
    `role == ObjectRole.ANNOTATION` therefore matches *nothing* on a real
    library while passing any test that builds its objects by hand.

    Checked against the live database rather than reasoned about: of 29
    objects there, 4 were GFF/GTF and 0 carried the annotation role.

    GenBank does not qualify despite being eligible for annotation Results
    (`_ANNOTATION_STATS_FORMATS`) -- this predicate also gates
    `annotations_for_project`/`resolve_annotation`, which feed
    `launch_quantify`. featureCounts cannot parse a GenBank flat file, and
    `counts_runner.attributes_for_format` has no GenBank case: a raw `.gbff`
    reaching it would silently fall through to GTF's `-g gene_id`, the same
    "near zero rather than an error" trap that function's own docstring
    documents for a different mismatch. Broadening this later needs an
    `attributes_for_format` case and a real featureCounts-readable input, not
    just an enum member here.
    """
    if obj.status is not ObjectStatus.READY:
        return False
    return obj.format.kind in (FormatKind.GFF, FormatKind.GTF)


async def annotations_for_project(
    project_id: PydanticObjectId, *, owner: str
) -> list[DataObject]:
    """Every annotation in a project, GTF first.

    GTF first because featureCounts' conventional `-t exon -g gene_id` works
    on it and does not work on NCBI's GFF3 -- see
    `counts_runner.attributes_for_format`. NCBI ships both for an assembly, so
    preferring the GTF costs nothing and avoids the failure entirely on the
    common path.
    """
    from app.services import object_service

    objects = await object_service.list_objects(project_id, owner=owner)
    annotations = [o for o in objects if _is_annotation(o)]
    annotations.sort(
        key=lambda o: (0 if o.format.kind is FormatKind.GTF else 1, o.name)
    )
    return annotations


async def resolve_annotation(
    project_id: PydanticObjectId,
    annotation_id: PydanticObjectId | None,
    *,
    owner: str,
) -> DataObject:
    """The annotation to use: explicit if given, else the project's.

    Ambiguity is refused rather than guessed at when more than one distinct
    annotation is available and none was named. Counting -- or indexing --
    against the wrong one produces a plausible-looking result with no error:
    a full counts file with a low assignment rate for featureCounts, or a
    STAR index that improves junction sensitivity for the wrong gene set.
    Takes a project id rather than a BAM: the BAM only ever contributed its
    `project_id`, and STAR's index build has no BAM yet to hand in -- the
    whole point is building the index before alignment.
    """
    from app.services import object_service

    if annotation_id is not None:
        annotation = await object_service.get_object(annotation_id, owner=owner)
        if not _is_annotation(annotation):
            raise ValidationError(
                f"{annotation.name!r} is not a GTF or GFF annotation.",
                details={"object_id": str(annotation.id)},
            )
        if annotation.project_id != project_id:
            raise ValidationError(
                "The annotation must be in the same project as the target."
            )
        return annotation

    candidates = await annotations_for_project(project_id, owner=owner)
    if not candidates:
        raise ValidationError(
            "This project has no gene annotation. Download one with the "
            "assembly, or upload a GTF.",
            details={"project_id": str(project_id), "needs": "annotation"},
        )

    # One assembly's GFF and GTF are the same annotation in two formats, not
    # two choices -- picking the GTF is the whole point of the sort above.
    # Distinct *assemblies* are a real ambiguity and are refused.
    distinct = {_assembly_stem(o.name) for o in candidates}
    if len(distinct) > 1:
        raise ValidationError(
            "This project has more than one annotation and none was chosen: "
            f"{', '.join(sorted(distinct))}. Pick the one matching the "
            "reference in use.",
            details={
                "project_id": str(project_id),
                "needs": "annotation_id",
                "choices": [
                    {"id": str(o.id), "name": o.name} for o in candidates
                ],
            },
        )

    return candidates[0]


def _assembly_stem(filename: str) -> str:
    """The assembly a GFF/GTF belongs to, from its name.

    `GCF_000146045.2_R64_genomic.gtf` and `..._genomic.gff` are one annotation
    published in two formats; collapsing them here is what keeps the two-file
    NCBI download from reading as an ambiguous choice.
    """
    stem = Path(filename).name
    for suffix in (".gtf", ".gff3", ".gff"):
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)]
    return stem


async def paired_for_bam(bam: DataObject) -> bool:
    """Whether this BAM's reads are paired.

    Three sources, in descending order of trust, because getting it wrong
    doubles every count on paired data and nothing downstream notices:

    1. `properly_paired_reads > 0` in facts, written by `index_bam`. Definite.
    2. The alignment run's inputs -- a paired alignment has a MATE input. Also
       definite, and available for anything this app aligned even when
       index_bam has not run.
    3. False, for an uploaded BAM with neither. The dialog shows the value it
       chose and lets the user correct it, which is the honest handling of a
       question the file genuinely does not answer.

    Deliberately not answered by reading the BAM's records: `run_subprocess`
    streams a child to completion, so sampling the first thousand alignments
    would decompress the whole file to reach them.
    """
    from_facts = counts_runner.paired_from_facts(bam.facts)
    if from_facts is not None:
        return from_facts

    if bam.produced_by_job is not None:
        from app.models import PipelineRun

        run_id = await run_service.run_for_job(bam.produced_by_job)
        if run_id is not None:
            run = await PipelineRun.get(run_id)
            if run is not None:
                return any(i.role is RunInputRole.MATE for i in run.inputs)

    return False


async def default_count_params(
    bam: DataObject, annotation: DataObject
) -> dict:
    """The counting parameters this BAM and annotation imply.

    Both interesting values are derived rather than defaulted. Strandedness
    comes off the alignment's own `--rna-strandness`, so a user who answered
    the question once at alignment does not answer it again here and get it
    wrong; the feature/attribute pair comes from the annotation's format,
    where the conventional default is silently wrong for GFF3.
    """
    align_params = (bam.facts or {}).get("align_params") or {}
    strandedness = counts_runner.strandedness_for_align_params(align_params)
    feature_type, attribute = counts_runner.attributes_for_format(
        annotation.format.kind
    )

    return counts_runner.CountsParams(
        threads=settings.pipeline_default_threads,
        strandedness=(
            strandedness if strandedness is not None else counts_runner.UNSTRANDED
        ),
        paired=await paired_for_bam(bam),
        feature_type=feature_type,
        attribute=attribute,
    ).as_dict()


async def launch_quantify(
    *,
    bam_id: PydanticObjectId,
    owner: str,
    annotation_id: PydanticObjectId | None = None,
    params: dict | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue a per-gene count over one aligned BAM.

    Unlike variant calling this needs no index: featureCounts streams the BAM
    in the order it finds it and never seeks.
    """
    from app.queue import queue
    from app.services import object_service

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=QUANTIFY_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    bam = await object_service.get_object(bam_id, owner=owner)
    _check_quantifiable(bam)

    annotation = await resolve_annotation(bam.project_id, annotation_id, owner=owner)

    merged = counts_runner.CountsParams.from_dict({
        **(await default_count_params(bam, annotation)),
        **(params or {}),
    })

    tools.require(tools.featurecounts())

    payload: dict = {
        "object_id": str(bam.id),
        "annotation_object_id": str(annotation.id),
        "project_id": str(bam.project_id),
        "bam_name": bam.name,
        "annotation_name": annotation.name,
        "params": merged.as_dict(),
    }
    for key, obj in (("bam", bam), ("annotation", annotation)):
        digest, path = await _resolve_readable(obj)
        if digest:
            payload[f"{key}_sha256"] = digest
        if path:
            payload[f"{key}_path"] = path

    run = await run_service.create_run(
        kind=RunKind.QUANTIFY,
        project_id=bam.project_id,
        label=f"{bam.name} → counts ({annotation.name})",
        inputs=[
            RunInput(object_id=bam.id, name=bam.name, role=RunInputRole.ALIGNMENT),
            RunInput(
                object_id=annotation.id,
                name=annotation.name,
                role=RunInputRole.ANNOTATION,
            ),
        ],
        params=merged.as_dict(),
        owner=owner,
        tool="featurecounts",
    )

    job = await queue.enqueue(
        "quantify",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(
            cpu=merged.threads, mem_mb=QUANTIFY_MEM_MB, io=IoClass.LIGHT
        ),
        max_attempts=2,
        dedup_key=(
            f"quantify:{bam.id}:{annotation.id}:"
            f"{_params_fingerprint(merged.as_dict())}"
        ),
        project_id=bam.project_id,
        object_id=bam.id,
        resource_override=resource_override,
    )
    if job is None:
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "An identical quantification is already queued or running",
            details={"bam_id": str(bam.id), "annotation_id": str(annotation.id)},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.QUANTIFY)
    log.info(
        "quantify_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        bam_id=str(bam.id),
        strandedness=merged.strandedness,
        paired=merged.paired,
    )
    return job


# feature_coverage_handlers.run_feature_coverage only understands these two
# annotation shapes ("gff" or "bed" -- it raises PermanentError on anything
# else). GTF maps to the same "gff" string as GFF3: bedtools coverage and
# feature_coverage_runner's row parser only care about column POSITIONS
# (seq_id/type/start/end/strand/attributes at 0/2/3/4/6/8), which GTF and
# GFF3 share -- they differ only in how column 8's attribute string is
# punctuated (`key "value";` vs `key=value;`), which _gff_name handles for
# both. This is the opposite of featureCounts (counts_runner
# .attributes_for_format), which genuinely needs GTF's `-g gene_id`
# convention and cannot read GFF3's attribute names -- that asymmetry is why
# annotations_for_project sorts GTF first for quantify while feature_coverage
# is equally happy with either. BED is kept in the map for a format
# feature_coverage's own row parser already understands, even though
# _is_annotation (below) never actually resolves a BED here today -- see
# launch_feature_coverage for why that entry is harmless rather than reachable
# dead code.
_FEATURE_COVERAGE_ANNOTATION_FORMATS = {
    FormatKind.GFF: "gff",
    FormatKind.GTF: "gff",
    FormatKind.BED: "bed",
}


async def launch_feature_coverage(
    *,
    bam_id: PydanticObjectId,
    owner: str,
    annotation_id: PydanticObjectId | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue per-feature read coverage for one BAM against one annotation.

    Read-only, like bam_stats and vcf_stats: no derived object, just a report
    plus summary facts merged onto the BAM. Unlike launch_bam_stats, this
    needs a reference's `.fai` in addition to the BAM's own `.bai` -- and
    unlike launch_bam_stats's `.bai` (which it builds by chaining into
    index_bam when missing), there is no chaining precedent here for
    building a *reference's* index on the fly from a read-only launch path.
    Refusing with an actionable "index it first" / "build its index first"
    message, matching launch_variant_calling's precedent exactly, is simpler
    and keeps both required sidecars refused the same way.
    """
    from app.queue import queue
    from app.services import object_service

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=FEATURE_COVERAGE_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    bam = await object_service.get_object(bam_id, owner=owner)
    _check_bam_stats_callable(bam)

    reference = await _resolve_variant_reference(bam, None, owner=owner)
    annotation = await resolve_annotation(bam.project_id, annotation_id, owner=owner)

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

    annotation_format = _FEATURE_COVERAGE_ANNOTATION_FORMATS.get(annotation.format.kind)
    if annotation_format is None:
        raise ValidationError(
            f"{annotation.name!r} is {annotation.format.kind.value}, which "
            f"feature coverage cannot use -- it reads GFF, GTF, or BED.",
            details={"object_id": str(annotation.id), "kind": annotation.format.kind.value},
        )

    payload: dict = {
        "bam_id": str(bam.id),
        "bam_name": bam.name,
        "annotation_id": str(annotation.id),
        "annotation_name": annotation.name,
        "annotation_format": annotation_format,
        "project_id": str(bam.project_id),
    }
    for key, obj in (("bam", bam), ("annotation", annotation), ("fai", fai)):
        digest, path = await _resolve_readable(obj)
        if digest:
            payload[f"{key}_sha256"] = digest
        if path:
            payload[f"{key}_path"] = path

    job = await queue.enqueue(
        "feature_coverage",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=FEATURE_COVERAGE_MEM_MB, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"feature_coverage:{bam.blob_sha256}:{annotation.blob_sha256}",
        project_id=bam.project_id,
        object_id=bam.id,
        resource_override=resource_override,
    )
    if job is None:
        raise ConflictError(
            "Feature coverage is already queued or running for this BAM "
            "and annotation",
            details={"bam_id": str(bam.id), "annotation_id": str(annotation.id)},
        )

    log.info(
        "feature_coverage_launched",
        job_id=str(job.id),
        bam_id=str(bam.id),
        annotation_id=str(annotation.id),
    )
    return job


def sample_name_for(obj: DataObject) -> str:
    """The readable name for a counts object in a matrix or a results table.

    `sample_id` -- an existing common metadata field, not one invented for
    this -- if the user set one, else the file name with its pipeline suffixes
    stripped. `SRR1234567_trimmed.sorted.counts.tsv` is what the pipeline
    produces and is nobody's idea of a column header.
    """
    explicit = (obj.metadata or {}).get("sample_id")
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    return de_runner.counts_path_stem(obj.name)


async def counts_for_project(
    project_id: PydanticObjectId, *, owner: str
) -> list[DataObject]:
    """Every counts object in a project, newest last."""
    from app.services import object_service

    objects = await object_service.list_objects(project_id, owner=owner)
    counts = [
        o
        for o in objects
        if o.role is ObjectRole.COUNTS and o.status is ObjectStatus.READY
    ]
    counts.sort(key=lambda o: (o.created_at, o.name))
    return counts


async def differential_expression_defaults(
    project_id: PydanticObjectId, *, owner: str
) -> dict:
    """What the DE dialog opens with.

    The design is read from each counts object's `condition` metadata, which
    is where the metadata editor and the bulk edit bar already write -- so
    "tag these six as treated" is a gesture the user has already been able to
    make, on the reads, and it arrives here through the metadata each applier
    copies forward.

    Everything here is a *default*. Nothing refuses to open because the
    metadata is incomplete: the dialog is where a design gets finished, not
    where it has to have been finished already.
    """
    counts = await counts_for_project(project_id, owner=owner)

    samples = [
        {
            "object_id": str(o.id),
            "name": o.name,
            "sample": sample_name_for(o),
            "condition": str((o.metadata or {}).get("condition") or ""),
            "assigned_pct": (o.facts or {}).get("assigned_pct"),
            "genes_detected": (o.facts or {}).get("genes_detected"),
            # Surfaced so the dialog can warn before a run rather than after:
            # a sample counted against a different annotation cannot be merged
            # with the others, and finding that out from a failed job is a
            # worse experience than seeing it greyed out in the list.
            "annotation_sha256": (o.facts or {}).get("annotation_sha256"),
            "annotation_name": (o.facts or {}).get("annotation_name"),
        }
        for o in counts
    ]

    conditions = sorted({s["condition"] for s in samples if s["condition"]})

    return {
        "samples": samples,
        "conditions": conditions,
        # Two conditions is the common case and the only one this supports, so
        # pre-fill the contrast when there are exactly two. Alphabetical, with
        # the second as the test group -- arbitrary, and the dialog lets it be
        # swapped, which is cheaper than guessing which name means "treated".
        "contrast": (
            {"reference": conditions[0], "test": conditions[1]}
            if len(conditions) == 2
            else None
        ),
        "min_replicates": de_runner.MIN_REPLICATES,
        "available": tools.pydeseq2().available,
    }


async def launch_differential_expression(
    *,
    project_id: PydanticObjectId,
    owner: str,
    design: dict[str, str],
    contrast: dict,
    threads: int | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue a differential expression test over N counts objects.

    `design` maps counts object id to condition name. Validated fully here
    rather than in the handler: every failure mode this has -- a singleton
    group, a contrast naming a condition nobody is in, samples counted against
    different annotations -- is knowable before anything is enqueued, and a
    job that dies twenty seconds in with a KeyError from inside PyDESeq2 is a
    worse version of the same answer.
    """
    from app.queue import queue
    from app.services import object_service

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=DIFFERENTIAL_EXPRESSION_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    if not design:
        raise ValidationError("No samples were assigned to a condition.")

    test = str(contrast.get("test") or "")
    reference = str(contrast.get("reference") or "")
    if not test or not reference:
        raise ValidationError(
            "A contrast needs both a test and a reference condition."
        )

    objects: list[DataObject] = []
    for object_id in design:
        obj = await object_service.get_object(
            PydanticObjectId(object_id), owner=owner
        )
        if obj.role is not ObjectRole.COUNTS:
            raise ValidationError(
                f"{obj.name!r} is not a counts file.",
                details={"object_id": str(obj.id)},
            )
        if obj.project_id != project_id:
            raise ValidationError(
                f"{obj.name!r} is in a different project.",
                details={"object_id": str(obj.id)},
            )
        objects.append(obj)

    samples = [
        de_runner.SampleCounts(
            sample=sample_name_for(o),
            condition=str(design[str(o.id)]),
            counts={},
            annotation_sha256=(o.facts or {}).get("annotation_sha256"),
            object_id=str(o.id),
        )
        for o in objects
    ]

    # Both checks run before the enqueue. validate_design catches the design
    # errors; the annotation check here is the cheap half of what
    # de_runner.merge_counts will re-check on the real gene sets -- the
    # digests are known now, the gene sets are not.
    de_runner.validate_design(samples, test=test, reference=reference)

    digests = {s.annotation_sha256 for s in samples if s.annotation_sha256}
    if len(digests) > 1:
        raise ValidationError(
            "These samples were counted against different annotations, so "
            "their counts are not comparable. Re-quantify them against one "
            "annotation before testing.",
            details={"annotations": sorted(d for d in digests if d)},
        )

    tools.require(tools.pydeseq2())

    payload_samples = []
    for obj, sample in zip(objects, samples, strict=True):
        entry = {
            "counts_object_id": str(obj.id),
            "name": obj.name,
            "sample": sample.sample,
            "condition": sample.condition,
            "annotation_sha256": sample.annotation_sha256,
        }
        digest, path = await _resolve_readable(obj)
        if digest:
            entry["counts_sha256"] = digest
        if path:
            entry["counts_path"] = path
        payload_samples.append(entry)

    resolved_threads = threads or settings.pipeline_default_threads
    payload = {
        "project_id": str(project_id),
        "samples": payload_samples,
        "contrast": {"test": test, "reference": reference},
        "threads": resolved_threads,
    }

    run = await run_service.create_run(
        kind=RunKind.DIFFERENTIAL_EXPRESSION,
        project_id=project_id,
        # Not "a → b": this is the one run kind with N inputs, and naming them
        # all would produce a 400-character label. What a person needs to tell
        # two DE runs apart is the contrast and the size, not the file list.
        label=(
            f"{len(samples)} samples — {test} vs {reference}"
        ),
        inputs=[
            RunInput(object_id=o.id, name=o.name, role=RunInputRole.COUNTS)
            for o in objects
        ],
        params={
            "contrast": {"test": test, "reference": reference},
            "design": {s.sample: s.condition for s in samples},
            "threads": resolved_threads,
        },
        owner=owner,
        tool="pydeseq2",
    )

    job = await queue.enqueue(
        "differential_expression",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(
            cpu=resolved_threads, mem_mb=DIFFERENTIAL_EXPRESSION_MEM_MB, io=IoClass.LIGHT
        ),
        max_attempts=2,
        dedup_key=(
            f"de:{project_id}:{test}:{reference}:"
            f"{_params_fingerprint({k: v for k, v in sorted(design.items())})}"
        ),
        project_id=project_id,
        resource_override=resource_override,
    )
    if job is None:
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "An identical differential expression run is already queued or "
            "running",
            details={"project_id": str(project_id), "contrast": [test, reference]},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.TEST)
    log.info(
        "differential_expression_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        samples=len(samples),
        test=test,
        reference=reference,
    )
    return job


# --- De novo assembly --------------------------------------------------------


def _check_assemblable(obj: DataObject) -> None:
    """Whether these reads can be assembled at all.

    Format and status only. Chemistry is checked by the caller, because "no
    assembler for short reads" and "run QC first" are different answers and
    only one of them is the user's to fix.
    """
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready to assemble (status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind is not FormatKind.FASTQ:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not reads to assemble",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )


async def infer_genome_size(obj: DataObject) -> tuple[int | None, str | None]:
    """A genome size for the memory estimate, and the file it came from.

    Looks for a reference in the same project whose organism matches these
    reads', and takes its total length. That is a *measured* number from a
    real assembly of the same organism, which is the only kind of inference
    worth making here.

    Deliberately not derived from read volume. Total bases divided by an
    assumed coverage is a guess wearing a measurement's clothes, and it would
    be wrong by exactly the factor the user does not know.

    Returns (None, None) when nothing in the project can say -- the normal
    case for de novo assembly, and not an error.
    """
    from app.services import object_service

    organism = (obj.metadata or {}).get("organism")
    if not organism or not str(organism).strip():
        return None, None
    target = _organism_key(organism)
    if not target:
        return None, None

    candidates = await object_service.list_objects(
        obj.project_id, owner=obj.owner, limit=500, status=ObjectStatus.READY
    )
    for candidate in candidates:
        if candidate.format.kind is not FormatKind.FASTA:
            continue
        if candidate.role is not ObjectRole.REFERENCE:
            continue
        candidate_organism = (candidate.metadata or {}).get("organism")
        if not candidate_organism or _organism_key(candidate_organism) != target:
            continue

        facts = candidate.facts or {}
        # Only NCBI's assembly-level figure, never the file's own base count.
        #
        # Found against the real library: a project holds `protein.faa` and
        # `cds_from_genomic.fna` roled `reference`. The component table gets
        # this right today (protein -> PROTEIN, cds -> TRANSCRIPT), so those
        # rows are legacy data from before that fix -- but they exist, and a
        # role check alone therefore does not keep them out. Their
        # `total_bases` is the protein or CDS length: 2.9 Mb against a 12.1 Mb
        # yeast genome, which would silently under-estimate the memory a real
        # assembly needs.
        #
        # `ncbi_total_length` is safe from any of them, because it describes
        # the *assembly* rather than the file, and every component of one
        # download carries the same value. Dropping the `total_bases` fallback
        # costs inference on a hand-uploaded genome with no NCBI metadata,
        # which is the right trade: no estimate is a stated non-opinion, and a
        # wrong one is a number the user would reasonably act on.
        size = facts.get("ncbi_total_length")
        if size:
            # Name the assembly, not the file. The number describes the
            # assembly and every component of a download carries it, so
            # whichever component happens to sort first would otherwise get
            # the credit -- against the real library that is
            # `cds_from_genomic.fna`, and "genome size inferred from
            # cds_from_genomic.fna" invites exactly the doubt the label exists
            # to remove.
            assembly = facts.get("ncbi_assembly_name")
            accession = facts.get("ncbi_assembly_accession")
            if assembly and accession:
                return int(size), f"{assembly} ({accession})"
            return int(size), assembly or accession or candidate.name
    return None, None


def _organism_key(name) -> str:
    """Genus and species, for comparing two organism strings.

    Strain suffixes are why this exists rather than an equality check: SRA
    labels a run `Saccharomyces cerevisiae` while the assembly it came from is
    `Saccharomyces cerevisiae S288C`, and an exact match rejects the one
    reference in the project that could answer. Genome size does not vary
    meaningfully between strains of a species, so the first two words are the
    right granularity -- and stopping there also avoids matching two different
    species that share a genus, whose genomes can differ severalfold.
    """
    parts = str(name).strip().lower().split()
    return " ".join(parts[:2]) if len(parts) >= 2 else ""


async def resolve_assembly_mate(
    reads: DataObject, candidate: DataObject | None = None
) -> DataObject | None:
    """The R2 for these reads, or None to assemble single-end.

    Delegates discovery to `suggest_mate` above, which already prefers the
    persisted link, falls back to the filename convention, and accepts only
    CONFIRMED/NAME_ONLY verdicts. This wrapper adds exactly one thing:
    REJECTED_READ_IDS becomes a refusal instead of a silent single-end
    fallback.

    That difference is the point. For trimming, two mismatched files just mean
    "trim them separately", so `suggest_mate` returning None is right. For
    assembly, quietly assembling one half of what the user thinks is a pair
    produces a plausible result with no error -- worse than a refusal they can
    act on.
    """
    if candidate is None:
        return await suggest_mate(reads)

    verdict = pairing.verdict(
        pairing.PairInput(name=reads.name, facts=reads.facts, metadata=reads.metadata),
        pairing.PairInput(
            name=candidate.name, facts=candidate.facts, metadata=candidate.metadata
        ),
    )
    if verdict is pairing.Verdict.REJECTED_READ_IDS:
        raise ValidationError(
            f"{reads.name!r} and {candidate.name!r} look like a pair by name "
            "but their read IDs do not appear to be mates. Assembling them "
            "together would produce a misleading result.",
            details={"reads": reads.name, "mate": candidate.name},
        )
    if verdict in (pairing.Verdict.CONFIRMED, pairing.Verdict.NAME_ONLY):
        return candidate
    return None


async def default_assembly_params(obj: DataObject) -> dict:
    """The dialog's starting point for these reads.

    The assembler and input mode follow the inferred chemistry, and genome
    size is filled in from the project when something there can say. Every
    value is overridable; `genome_size_source` is what lets the dialog tell
    the user which of them BioFlow guessed.
    """
    chemistry = read_chemistry(obj)
    spec = assembler_registry.spec_for_chemistry(chemistry)
    if spec is None:
        raise ValidationError(
            "No assembler is available for these reads",
            details={"chemistry": chemistry.value if chemistry else None},
        )

    size, source_name = await infer_genome_size(obj)
    if spec.layout == "paired":
        # ABySS: no chemistry-graded mode to look up (mode_flags is empty by
        # design -- see ABYSS_SPEC), so the dialog's one real knob is k. Mirror
        # AbyssParams' own default rather than duplicating a magic number.
        params: dict = {
            "assembler": spec.assembler.value,
            "k": assembly_params_module.AbyssParams.k,
            "threads": 8,
        }
    else:
        params = {
            "assembler": spec.assembler.value,
            "mode": assembler_registry.mode_for_chemistry(spec, chemistry),
            "threads": 8,
            "iterations": 1,
        }
    if size is not None:
        params["genome_size"] = size
        params["genome_size_source"] = "inferred"
        params["genome_size_from"] = source_name
    return params


async def launch_assembly(
    *,
    object_id: PydanticObjectId,
    owner: str,
    params: dict | None = None,
    resource_override: bool = False,
    mate_object_id: PydanticObjectId | None = None,
    from_parameter_set: AppliedParameterSet | None = None,
) -> Job:
    """Queue a de novo assembly of one FASTQ, paired when we can identify both mates."""
    from app.queue import queue
    from app.queue.governor import LoadGovernor
    from app.services import object_service, run_service

    reads = await object_service.get_object(object_id, owner=owner)
    _check_assemblable(reads)

    chemistry = read_chemistry(reads)
    spec = assembler_registry.spec_for_chemistry(chemistry)
    if spec is None:
        raise ValidationError(
            f"{reads.name!r} has no known read chemistry. Run QC on it first "
            "-- the assembler's input mode depends on how accurate the reads "
            "are.",
            details={"object_id": str(reads.id)},
        )

    if not spec.available():
        raise ValidationError(
            spec.unavailable_reason or f"{spec.assembler.value} is not installed",
            details={"assembler": spec.assembler.value},
        )

    if params is None:
        params = await default_assembly_params(reads)
    parsed = assembly_params_module.from_dict(params)

    mate = None
    if spec.layout == "paired":
        explicit = None
        if mate_object_id is not None:
            explicit = await object_service.get_object(mate_object_id, owner=owner)
        mate = await resolve_assembly_mate(reads, candidate=explicit)

    # Bases, approximated from file size. FASTQ carries ~2 bytes per base
    # (sequence plus quality) before compression, and both mates contribute.
    # Only consumed by a model with a non-zero read coefficient, so this is
    # inert for Flye.
    read_bytes = (reads.size or 0) + (mate.size if mate else 0)
    read_bases = int(read_bytes / 2) if read_bytes else None

    # The memory guard, at launch rather than dispatch: governor.py does not
    # read a job's mem_mb, so declaring it reserves nothing. A missing genome
    # size yields no estimate and therefore no refusal -- see
    # estimate_assembly_mb on why that asymmetry is deliberate.
    heuristic_mb = resource_estimator.estimate_assembly_mb(
        assembler=parsed.assembler,
        genome_bases=parsed.genome_size,
        threads=parsed.threads,
        read_bases=read_bases,
    )
    resolved = await memory_estimate.resolve(
        job_type=JOB_TYPE_ASSEMBLE,
        input_bytes=reads.size or None,
        heuristic_mb=heuristic_mb,
        threads=parsed.threads,
    )
    estimate = resolved.mb

    # Hoisted from the enqueue below so it can be checked before we get there.
    # Outside the `estimate is not None` guard on purpose: an assembly nothing
    # can estimate declares the flat fallback and is banded by nothing at all,
    # which is the cleanest instance of #478.
    declared_mem_mb = estimate or UNKNOWN_ASSEMBLY_MEM_MB
    refuse_if_over_budget(
        declared_mb=declared_mem_mb,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    if estimate is not None:
        mem_budget_mb = int(LoadGovernor().mem_budget_bytes() / (1024 * 1024))
        band = resource_estimator.classify(
            estimated_mb=estimate,
            mem_budget_mb=mem_budget_mb,
            threads=parsed.threads,
            cpu_budget=None,
        )
        if band is resource_estimator.Band.BLOCK and not resource_override:
            from app.services import replan_service

            proposal = replan_service.replan(
                job_type=JOB_TYPE_ASSEMBLE,
                # as_dict() carries genome_size, but the proposer reads
                # genome_bases -- merged in under the name it expects rather
                # than renaming the field on AssemblyParams for one caller.
                params={
                    **parsed.as_dict(),
                    "genome_bases": parsed.genome_size,
                    "read_bases": read_bases,
                },
                budget_mb=mem_budget_mb,
                cpu_budget=LoadGovernor().cpu_budget() or 1,
            )
            raise ValidationError(
                f"This assembly needs about {estimate:,} MB "
                f"({resolved.detail}), more than the "
                f"{mem_budget_mb:,} MB available. Assembling a genome this "
                "size needs a bigger machine.",
                details={
                    "refusal": "estimate",
                    "estimate_mb": estimate,
                    "budget_mb": mem_budget_mb,
                    "estimate_source": resolved.source.value,
                    "detail": resolved.detail,
                    # Inlined rather than fetched by a follow-up request: this
                    # path is reactive, so the card renders from this response
                    # and a second round trip would show it half-populated.
                    "replan": replan_service.as_payload(proposal),
                },
            )

    digest, path = await _resolve_readable(reads)
    if not digest and not path:
        raise ValidationError(
            f"{reads.name!r} has no stored content yet "
            f"(status={reads.status.value})",
            details={"object_id": str(reads.id)},
        )

    mate_digest, mate_path = (None, None)
    if mate is not None:
        mate_digest, mate_path = await _resolve_readable(mate)

    assembly_inputs = [
        RunInput(object_id=reads.id, name=reads.name, role=RunInputRole.READS)
    ]
    if mate is not None:
        assembly_inputs.append(
            RunInput(object_id=mate.id, name=mate.name, role=RunInputRole.MATE)
        )

    run = await run_service.create_run(
        kind=RunKind.ASSEMBLY,
        project_id=reads.project_id,
        label=f"Assemble {reads.name}",
        inputs=assembly_inputs,
        params=parsed.as_dict(),
        owner=owner,
        tool=parsed.assembler.value,
        from_parameter_set=from_parameter_set,
    )

    payload: dict = {
        "object_id": str(reads.id),
        "project_id": str(reads.project_id),
        "reads_name": reads.name,
        "assembler": parsed.assembler.value,
        "params": parsed.as_dict(),
        # The memory ceiling for this run, derived from the same estimate that
        # decided it could proceed -- one number, not two that must agree.
        # ABySS spends it as a Bloom filter budget; SPAdes as `-m`.
        "memory_bytes": (estimate * 1024 * 1024) if estimate else None,
    }
    if digest:
        payload["reads_sha256"] = digest
    if path:
        payload["reads_path"] = path
    if mate is not None:
        payload["mate_name"] = mate.name
        if mate_digest:
            payload["mate_sha256"] = mate_digest
        if mate_path:
            payload["mate_path"] = mate_path

    job = await queue.enqueue(
        "assemble_reads",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        # cpu from the user's thread count, as trim and align both do. mem_mb
        # carries the estimate when there is one so the declaration is honest
        # for whenever the governor learns to read it.
        resources=JobResources(
            cpu=parsed.threads,
            mem_mb=declared_mem_mb,
            io=IoClass.HEAVY,
        ),
        # One attempt, matching the handler: a retried assembly costs hours and
        # fails identically.
        max_attempts=1,
        dedup_key="assemble:"
        + ":".join(
            [
                str(reads.id),
                str(mate.id) if mate else "-",
                _params_fingerprint(parsed.as_dict()),
            ]
        ),
        project_id=reads.project_id,
        object_id=reads.id,
        resource_override=resource_override,
    )
    if job is None:
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "An identical assembly is already queued or running for this file",
            details={"object_id": str(reads.id)},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.ASSEMBLE)
    log.info(
        "assembly_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        object_id=str(reads.id),
        assembler=parsed.assembler.value,
        mode=getattr(parsed, "mode", None),
        genome_size=parsed.genome_size,
        estimate_mb=estimate,
    )
    return job


# FASTA only, and not just any FASTA: `protein.faa` and
# `cds_from_genomic.fna` are FormatKind.FASTA too, and would pass a "does
# this look like a genome" sniff test. Excluded by role rather than
# name -- CLAUDE.md records this exact trap already costing the align card a
# green suite that shipped wrong.
COMPLETENESS_EXCLUDED_ROLES = {ObjectRole.PROTEIN, ObjectRole.TRANSCRIPT}


def _check_completeness_callable(obj: DataObject) -> None:
    """Whether this object is an assembly-shaped FASTA compleasm can score.

    Deliberately not gated on provenance: an uploaded assembly is as
    eligible as one this application produced. `role` is what excludes a
    protein or transcript FASTA, not `produced_by_job`.
    """
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready for completeness scoring "
            f"(status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind is not FormatKind.FASTA:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not a FASTA assembly",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )
    if obj.role in COMPLETENESS_EXCLUDED_ROLES:
        raise ValidationError(
            f"{obj.name!r} is a {obj.role.value} FASTA, not a genome assembly",
            details={"object_id": str(obj.id), "role": obj.role.value},
        )


async def launch_lineage_download(
    *, lineage: str, odb: str | None = None, owner: str
) -> Job:
    """Queue fetching one compleasm lineage dataset.

    A dependency of `launch_completeness`, not something it fetches inline --
    a completeness job must not depend on the network partway through. Called
    directly by the same name whether the caller is the completeness launch
    path (chaining automatically) or a user picking a lineage explicitly in
    the dialog.
    """
    from app.queue import queue

    tools.require(tools.compleasm())

    odb = odb or assembly_qc_registry.COMPLEASM_SPEC.odb

    return await queue.enqueue(
        "download_lineage",
        owner=owner,
        payload={"lineage": lineage, "odb": odb},
        job_class=JobClass.USER_INTERACTIVE,
        resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
        max_attempts=3,
        # One download per lineage+odb pair at a time, project-agnostic:
        # the dataset is shared across every project, so two projects
        # requesting the same lineage should collapse into the same job
        # rather than downloading it twice concurrently.
        dedup_key=f"download_lineage:{lineage}:{odb}",
    )


async def launch_kraken_db_download(*, db_key: str, owner: str) -> Job:
    """Queue fetching one Kraken2 classification database.

    A dependency of `launch_classify_reads`, not something it fetches
    inline -- a classification job must not depend on the network partway
    through, the same reasoning `launch_lineage_download` records.
    """
    from app.pipelines.kraken_db_registry import KRAKEN_DBS
    from app.queue import queue

    if db_key not in KRAKEN_DBS:
        raise ValidationError(
            f"Unknown Kraken2 database {db_key!r}",
            details={"db_key": db_key},
        )

    return await queue.enqueue(
        "download_kraken_db",
        owner=owner,
        payload={"db_key": db_key},
        job_class=JobClass.USER_INTERACTIVE,
        resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
        max_attempts=3,
        # One download per database at a time, project-agnostic: the store
        # is shared, so two projects requesting the same database collapse
        # into one job rather than downloading 7.5 GB twice concurrently.
        dedup_key=f"download_kraken_db:{db_key}",
    )


async def launch_completeness(
    *,
    object_id: PydanticObjectId,
    owner: str,
    lineage: str | None = None,
    odb: str | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue compleasm against one assembly.

    `lineage=None` infers from the object's `organism` metadata via
    `lineage_inference.infer_lineage`; a caller-supplied lineage (the
    dialog's override, once the user has changed it) always wins. Neither
    path guesses when there is truly nothing to go on -- an uploaded
    assembly with no organism metadata is a normal case, and the honest
    response is to ask the user to pick a lineage, not to score against a
    guessed domain.
    """
    from app.queue import queue
    from app.services import object_service

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=COMPLETENESS_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tool = tools.require(tools.compleasm())

    obj = await object_service.get_object(object_id, owner=owner)
    _check_completeness_callable(obj)

    odb = odb or assembly_qc_registry.COMPLEASM_SPEC.odb

    if lineage is None:
        organism = obj.metadata.get("organism") if obj.metadata else None
        lineage = lineage_inference.infer_lineage(organism)
        if lineage is None:
            raise ValidationError(
                f"{obj.name!r} has no organism metadata to infer a lineage "
                "from. Choose one to score completeness against.",
                details={"object_id": str(obj.id)},
            )

    from app.queue.lineage_handlers import lineage_present

    if not lineage_present(settings.lineages_dir, lineage, odb):
        raise ValidationError(
            f"The {lineage}_{odb} lineage dataset is not downloaded yet. "
            "Download it first, then score completeness.",
            details={"lineage": lineage, "odb": odb},
        )

    digest, path = await _resolve_readable(obj)
    if not digest and not path:
        raise ValidationError(
            f"{obj.name!r} has no stored content yet (status={obj.status.value})",
            details={"object_id": str(obj.id)},
        )

    payload: dict = {
        "object_id": str(obj.id),
        "assembly_name": obj.name,
        "lineage": lineage,
        "odb": odb,
    }
    if digest:
        payload["assembly_sha256"] = digest
    if path:
        payload["assembly_path"] = path

    job = await queue.enqueue(
        "assess_completeness",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=8, mem_mb=COMPLETENESS_MEM_MB, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=f"assess_completeness:{obj.id}:{lineage}:{odb}",
        project_id=obj.project_id,
        object_id=obj.id,
        resource_override=resource_override,
    )
    if job is None:
        raise ConflictError(
            "Completeness scoring is already queued or running for this "
            "assembly and lineage",
            details={"object_id": str(obj.id), "lineage": lineage},
        )

    log.info(
        "completeness_launched",
        job_id=str(job.id),
        object_id=str(obj.id),
        lineage=lineage,
        odb=odb,
        tool_version=tool.version,
    )
    return job


async def launch_classify_reads(
    *,
    object_id: PydanticObjectId,
    db_key: str,
    owner: str,
    mate_object_id: PydanticObjectId | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue Kraken2 classification for one FASTQ read set.

    Facts-only, no PipelineRun -- the launch_annotate_genome shape.  When
    the chosen database is not on disk, the download job is enqueued
    (deduped) and this job chains behind it via depends_on, the same shape
    launch_completeness uses for a missing lineage (spec K2-C2).  Memory is
    declared from the registry's known load size, never the fitted model
    (spec K2-C3).

    A concurrent request for the same missing database can race this one:
    `launch_kraken_db_download` calls `queue.enqueue`, which returns `None`
    when Mongo's dedup guard finds a non-terminal job already using the same
    `download_kraken_db:{db_key}` dedup key -- i.e. another caller already
    launched (or is launching) the same download. There is no cheap
    lookup-by-dedup-key helper in `queue.py` to fetch that existing job and
    depend on it instead, so this job's `depends_on` is simply left without
    the download entry in that case: the classify job races the download
    rather than waiting for it. `classify_reads` itself still fails cleanly
    if it loses that race (`db_present` check at the top of the handler), so
    the failure mode is "retry the job", not a crash.
    """
    from app.pipelines.kraken_db_registry import KRAKEN_DBS, db_present
    from app.queue import queue
    from app.services import object_service

    spec = KRAKEN_DBS.get(db_key)
    if spec is None:
        raise ValidationError(
            f"Unknown Kraken2 database {db_key!r}", details={"db_key": db_key}
        )

    refuse_if_over_budget(
        declared_mb=spec.mem_mb,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tools.require(tools.kraken2())

    obj = await object_service.get_object(object_id, owner=owner)
    if obj.format.kind is not FormatKind.FASTQ:
        raise ValidationError(
            "Classification runs on FASTQ reads",
            details={"object_id": str(obj.id), "format": obj.format.kind.value},
        )

    digest, path = await _resolve_readable(obj)
    if not digest and not path:
        raise ValidationError(
            f"{obj.name!r} has no stored content yet (status={obj.status.value})",
            details={"object_id": str(obj.id)},
        )

    payload: dict = {
        "object_id": str(obj.id),
        "db_key": db_key,
        "organism": (obj.metadata or {}).get("organism"),
        "mean_read_length": (obj.facts or {}).get("qc_mean_read_length"),
        "threads": 4,
    }
    if digest:
        payload["reads_sha256"] = digest
    if path:
        payload["reads_path"] = str(path)
    payload["reads_name"] = obj.name

    if mate_object_id is not None:
        mate = await object_service.get_object(mate_object_id, owner=owner)
        m_digest, m_path = await _resolve_readable(mate)
        if m_digest:
            payload["mate_sha256"] = m_digest
        if m_path:
            payload["mate_path"] = str(m_path)
        payload["mate_name"] = mate.name

    depends_on: list[PydanticObjectId] = []
    if not db_present(db_key):
        download = await launch_kraken_db_download(db_key=db_key, owner=owner)
        # download is None when a concurrent request already deduped onto an
        # existing download job -- see the docstring above. Nothing to
        # depend on in that case; the classify job races the download.
        if download is not None:
            depends_on.append(download.id)

    job = await queue.enqueue(
        "classify_reads",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=4, mem_mb=spec.mem_mb, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=f"classify_reads:{obj.id}:{db_key}",
        project_id=obj.project_id,
        object_id=obj.id,
        resource_override=resource_override,
        depends_on=depends_on,
    )
    if job is None:
        raise ConflictError(
            "Classification is already queued or running for this read set "
            "and database",
            details={"object_id": str(obj.id), "db_key": db_key},
        )

    log.info(
        "classify_reads_launched",
        job_id=str(job.id),
        object_id=str(obj.id),
        db_key=db_key,
        chained_download=bool(depends_on),
    )
    return job


async def launch_gc_tracks(
    *,
    object_id: PydanticObjectId,
    owner: str,
) -> Job:
    """Queue GC content and GC skew track computation for one assembly.

    Modelled on launch_completeness: single input, read-only, no run
    record, no new RunJobRole.  The result is facts merged onto the
    assembly, not a new object.
    """
    from app.queue import queue
    from app.services import object_service

    obj = await object_service.get_object(object_id, owner=owner)
    _check_completeness_callable(obj)  # same gates: FASTA, not protein/transcript

    digest, path = await _resolve_readable(obj)
    if not digest and not path:
        raise ValidationError(
            f"{obj.name!r} has no stored content yet (status={obj.status.value})",
            details={"object_id": str(obj.id)},
        )

    payload: dict = {
        "object_id": str(obj.id),
        "assembly_name": obj.name,
    }
    if digest:
        payload["assembly_sha256"] = digest
    if path:
        payload["assembly_path"] = path
    payload["compression"] = (obj.format.compression or "none").lower()

    job = await queue.enqueue(
        "analyze_gc_tracks",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=f"gc_tracks:{obj.id}",
        project_id=obj.project_id,
        object_id=obj.id,
    )
    if job is None:
        raise ConflictError(
            "GC track analysis is already queued or running for this assembly",
            details={"object_id": str(obj.id)},
        )

    log.info(
        "gc_tracks_launched",
        job_id=str(job.id),
        object_id=str(obj.id),
    )
    return job


async def launch_meryl_analysis(
    *,
    object_id: PydanticObjectId,
    owner: str,
    read_object_id: PydanticObjectId | None = None,
    k: int | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue meryl k-mer spectra and repeat-density analysis.

    Takes an assembly and optionally a read set. When ``read_object_id``
    is not given, auto-picks from the project — same logic as
    ``launch_assembly_qv``: prefer trimmed, refuse on multiple distinct
    read sets. The handler runs both analyses (spectra from reads, repeat
    density from assembly) in one job.

    Read-only: no derived object, no run record, facts merged onto the
    assembly.
    """
    from app.queue import queue
    from app.services import object_service, reference_assembly

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=MERYL_ANALYSIS_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tools.require(tools.meryl())

    assembly = await object_service.get_object(object_id, owner=owner)
    reference_assembly.check_draft_assembly(assembly)

    resolved_k = int(k) if k else DEFAULT_MERYL_K

    if read_object_id is None:
        candidates = reference_assembly.group_read_sets(
            [
                o
                for o in await object_service.list_objects(
                    assembly.project_id, owner=owner, status=ObjectStatus.READY
                )
                if o.format.kind is FormatKind.FASTQ
            ]
        )
        if not candidates:
            raise ValidationError(
                "K-mer spectra need the reads this assembly was built "
                "from, and this project has none",
                details={"object_id": str(assembly.id)},
            )
        if len(candidates) > 1:
            # Same trimmed-preference logic as launch_assembly_qv.
            trimmed_sets = [
                s for s in candidates
                if all(o.role == ObjectRole.TRIMMED_READS for o in s)
            ]
            raw_sets = [
                s for s in candidates
                if all(o.role != ObjectRole.TRIMMED_READS for o in s)
            ]
            if len(trimmed_sets) == 1 and len(raw_sets) >= 1 and (
                len(trimmed_sets) + len(raw_sets) == len(candidates)
            ):
                candidates = trimmed_sets
            else:
                raise ValidationError(
                    "This project has several read sets; name the one to "
                    "analyse",
                    details={
                        "object_id": str(assembly.id),
                        "candidates": [
                            [str(o.id) for o in group] for group in candidates
                        ],
                    },
                )
        chosen = candidates[0]
    else:
        primary = await object_service.get_object(read_object_id, owner=owner)
        if primary.project_id != assembly.project_id:
            raise ValidationError(
                "Reads and assembly must be in the same project",
                details={
                    "object_id": str(assembly.id),
                    "read_object_id": str(primary.id),
                },
            )
        chosen = [primary]
        mate_id = getattr(primary, "mate_object_id", None)
        if mate_id is not None:
            chosen.append(await object_service.get_object(mate_id, owner=owner))

    read_obj = chosen[0]

    # Reuse cached meryl read database if available.
    read_db_path = await _materialize_meryl_cache(read_obj, resolved_k, owner=owner)

    asm_digest, asm_path = await _resolve_readable(assembly)
    payload: dict = {
        "object_id": str(assembly.id),
        "assembly_name": assembly.name,
        "k": resolved_k,
        "threads": 4,
        "read_object_id": str(read_obj.id),
        "read_object_name": read_obj.name,
    }
    if asm_digest:
        payload["assembly_sha256"] = asm_digest
    if asm_path:
        payload["assembly_path"] = asm_path

    # Pass sequence_lengths for windowing repeat density.
    sequence_lengths = (assembly.facts or {}).get("sequence_lengths")
    if sequence_lengths and isinstance(sequence_lengths, dict):
        payload["sequence_lengths"] = sequence_lengths

    reads_payload = []
    for r in chosen:
        digest, path = await _resolve_readable(r)
        entry: dict = {"read_name": r.name}
        if digest:
            entry["read_sha256"] = digest
        if path:
            entry["read_path"] = path
        reads_payload.append(entry)
    payload["reads"] = reads_payload

    if read_db_path is not None:
        payload["read_db_path"] = str(read_db_path)

    job = await queue.enqueue(
        "analyze_meryl_tracks",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=4, mem_mb=MERYL_ANALYSIS_MEM_MB, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=f"analyze_meryl_tracks:{assembly.id}",
        project_id=assembly.project_id,
        object_id=assembly.id,
        resource_override=resource_override,
    )
    if job is None:
        raise ConflictError(
            "Meryl analysis is already queued or running for this assembly",
            details={"object_id": str(assembly.id)},
        )

    log.info(
        "meryl_analysis_launched",
        job_id=str(job.id),
        object_id=str(assembly.id),
        read_object_id=str(read_obj.id),
        k=resolved_k,
        cached=read_db_path is not None,
    )
    return job


async def launch_annotate_genome(
    *,
    object_id: PydanticObjectId,
    owner: str,
    resource_override: bool = False,
) -> Job:
    """Queue a Bakta genome annotation for one bacterial assembly.

    Modelled on ``launch_gc_tracks``: single input, read-only, no run
    record, no new RunJobRole.  The result is facts (gene density) merged
    onto the assembly, not a new object.  The GFF3 and GenBank files are
    stored as pipeline artifacts alongside the job log.

    Gated on the same rules as the suggestion card: FASTA, not
    protein/transcript, and a known bacterial organism.
    """
    from app.queue import queue
    from app.services import object_service

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=ANNOTATE_GENOME_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tools.require(tools.bakta())

    obj = await object_service.get_object(object_id, owner=owner)
    _check_completeness_callable(obj)

    organism = obj.metadata.get("organism") if obj.metadata else None
    if not organism:
        raise ValidationError(
            "Genome annotation needs a known organism to improve accuracy. "
            "Set one on the assembly's metadata tab.",
            details={"object_id": str(obj.id)},
        )

    import app.pipelines.organism_taxonomy as ot
    if ot.classify_organism(organism) is not ot.OrganismClass.BACTERIA:
        raise ValidationError(
            f"Bakta annotates bacterial and archaeal genomes. "
            f"{organism!r} is not a known prokaryote.",
            details={"object_id": str(obj.id), "organism": organism},
        )

    digest, path = await _resolve_readable(obj)
    if not digest and not path:
        raise ValidationError(
            f"{obj.name!r} has no stored content yet (status={obj.status.value})",
            details={"object_id": str(obj.id)},
        )

    # Split organism into genus/species/strain for Bakta's --genus / --species flags.
    organism_parts: dict = {}
    if organism and isinstance(organism, str):
        parts = organism.split()
        if len(parts) >= 1:
            organism_parts["genus"] = parts[0]
        if len(parts) >= 2:
            organism_parts["species"] = parts[1]
        if len(parts) >= 3:
            organism_parts["strain"] = " ".join(parts[2:])

    payload: dict = {
        "object_id": str(obj.id),
        "assembly_name": obj.name,
        "organism": organism_parts,
        "threads": 8,
    }
    if digest:
        payload["assembly_sha256"] = digest
    if path:
        payload["assembly_path"] = path

    # Pass sequence_lengths for gene density windowing -- same pattern as
    # launch_meryl_analysis.
    sequence_lengths = (obj.facts or {}).get("sequence_lengths")
    if sequence_lengths and isinstance(sequence_lengths, dict):
        payload["sequence_lengths"] = sequence_lengths

    job = await queue.enqueue(
        "annotate_genome",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=8, mem_mb=ANNOTATE_GENOME_MEM_MB, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=f"annotate_genome:{obj.id}",
        project_id=obj.project_id,
        object_id=obj.id,
        resource_override=resource_override,
    )
    if job is None:
        raise ConflictError(
            "Genome annotation is already queued or running for this assembly",
            details={"object_id": str(obj.id)},
        )

    log.info(
        "annotate_genome_launched",
        job_id=str(job.id),
        object_id=str(obj.id),
        organism=organism,
    )
    return job


async def launch_consensus(
    *,
    bam_object_id: PydanticObjectId,
    owner: str,
    primer_bed_object_id: PydanticObjectId | None = None,
    min_quality: int | None = None,
    min_freq: float | None = None,
    min_depth: int | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue an iVar consensus run against one alignment.

    The reference is never a caller-supplied argument -- it is resolved from
    the BAM's own provenance via `reference_assembly.resolve_alignment_target_for_bam`,
    the same rule the foundation (#21) built for exactly this: a BAM aligned
    to one sequence must not be treated as evidence about a different one,
    however plausible the pairing looks. `primer_bed_object_id` is the only
    optional input; when it is absent, the job skips primer trimming rather
    than refusing to run, since a non-amplicon viral alignment (metagenomic,
    bait-capture) is a legitimate consensus target with no primer scheme.
    """
    from app.queue import queue
    from app.services import object_service, reference_assembly, run_service

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=CONSENSUS_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tool = tools.require(tools.ivar())

    bam = await object_service.get_object(bam_object_id, owner=owner)
    reference = await reference_assembly.resolve_alignment_target_for_bam(
        bam, owner=owner
    )

    bam_digest, bam_path = await _resolve_readable(bam)
    ref_digest, ref_path = await _resolve_readable(reference)

    payload: dict = {
        "bam_object_id": str(bam.id),
        "bam_name": bam.name,
        "reference_object_id": str(reference.id),
        "reference_name": reference.name,
        "min_quality": min_quality if min_quality is not None else 20,
        "min_freq": min_freq if min_freq is not None else 0.0,
        "min_depth": min_depth if min_depth is not None else 10,
    }
    if bam_digest:
        payload["bam_sha256"] = bam_digest
    if bam_path:
        payload["bam_path"] = bam_path
    if ref_digest:
        payload["reference_sha256"] = ref_digest
    if ref_path:
        payload["reference_path"] = ref_path

    if primer_bed_object_id is not None:
        primer_bed = await object_service.get_object(
            primer_bed_object_id, owner=owner
        )
        # Checked here, before enqueue, rather than left for the handler:
        # iVar's own behaviour on a mismatched primer scheme is to trim
        # nothing and exit 0, producing an untrimmed consensus that looks
        # like a successful trimmed one (see reference_assembly.check_primer_bed
        # and GitHub #48).
        reference_assembly.check_primer_bed(primer_bed, reference)
        bed_digest, bed_path = await _resolve_readable(primer_bed)
        payload["primer_bed_object_id"] = str(primer_bed.id)
        payload["primer_bed_name"] = primer_bed.name
        if bed_digest:
            payload["primer_bed_sha256"] = bed_digest
        if bed_path:
            payload["primer_bed_path"] = bed_path
        primer_inputs = [
            RunInput(
                object_id=primer_bed.id,
                name=primer_bed.name,
                role=RunInputRole.PRIMERS,
            )
        ]
    else:
        primer_inputs = []

    run = await run_service.create_run(
        kind=RunKind.REFERENCE_ASSEMBLY,
        project_id=bam.project_id,
        label=f"Consensus {bam.name}",
        inputs=[
            RunInput(
                object_id=bam.id, name=bam.name, role=RunInputRole.ALIGNMENT
            ),
            RunInput(
                object_id=reference.id,
                name=reference.name,
                role=RunInputRole.REFERENCE,
            ),
            *primer_inputs,
        ],
        # The resolved defaults rather than the caller's Nones, so the record
        # says what iVar was actually run with -- payload already holds them.
        params={
            k: payload[k] for k in ("min_quality", "min_freq", "min_depth")
        },
        owner=owner,
        tool="ivar",
    )

    job = await queue.enqueue(
        "consensus_from_alignment",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        # Single-threaded pileup walk -- iVar does not parallelize, so more
        # CPU would idle. mem_mb matches the handler's own budget (see
        # reference_assembly_handlers.consensus_from_alignment): 8192, not
        # 4096, after a real run against a 26Mb T. brucei reference
        # OOM-killed at the lower number. io=HEAVY: I/O against a
        # high-coverage amplicon BAM is the other real cost.
        resources=JobResources(cpu=2, mem_mb=CONSENSUS_MEM_MB, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=f"consensus:{bam.id}:{primer_bed_object_id or 'noprimers'}",
        project_id=bam.project_id,
        object_id=bam.id,
        resource_override=resource_override,
    )
    if job is None:
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "Consensus calling is already queued or running for this "
            "alignment",
            details={"object_id": str(bam.id)},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.CONSENSUS)
    log.info(
        "consensus_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        bam_id=str(bam.id),
        reference_id=str(reference.id),
        primers=bool(primer_bed_object_id),
        tool_version=tool.version,
    )
    return job


def _read_bases(obj: DataObject) -> int | None:
    """Total bases in a FASTQ, from fastp's own count when QC has run.

    None when it has not. That None propagates all the way to the
    `--careful` decision, which deliberately takes the non-careful path on
    unknown depth rather than guessing -- see
    `polypolish_runner.params_for_depth`.
    """
    before = (obj.facts or {}).get("qc_before_filtering") or {}
    total = before.get("total_bases")
    return int(total) if total else None


async def launch_polish(
    *,
    draft_object_id: PydanticObjectId,
    owner: str,
    reads_object_id: PydanticObjectId | None = None,
    mate_object_id: PydanticObjectId | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue a Polypolish run: short reads correcting a draft assembly.

    Unlike `launch_consensus`, there is no BAM to validate a target against.
    Polypolish requires all-alignment SAM, which `align_reads` does not
    produce, so the handler aligns these reads to this draft itself -- which
    makes the alignment target correct by construction rather than by check.
    The epic's provenance requirement is discharged by recording the aligner
    on the output object, not by refusing a mismatched input that cannot
    exist here.

    Reads are resolved from the project when not named explicitly, and only
    when the choice is unambiguous: polishing an assembly with the wrong
    sample's reads is a silent corruption, so one candidate set means launch
    and several means refuse. `reference_assembly.short_read_sets` is what
    decides which candidates are eligible, and it excludes long-read files
    even when their inferred chemistry claims otherwise.
    """
    from app.queue import queue
    from app.services import object_service, reference_assembly, run_service

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=POLISH_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tool = tools.require(tools.polypolish())
    tools.require(tools.bwa_mem2())

    draft = await object_service.get_object(draft_object_id, owner=owner)
    reference_assembly.check_draft_assembly(draft)

    if reads_object_id is None:
        candidates = reference_assembly.short_read_sets(
            await object_service.list_objects(
                draft.project_id, owner=owner, status=ObjectStatus.READY
            )
        )
        if not candidates:
            raise ValidationError(
                "Polishing needs short reads, and this project has none",
                details={"draft_id": str(draft.id)},
            )
        if len(candidates) > 1:
            raise ValidationError(
                "This project has several short-read sets; name the one to "
                "polish with",
                details={
                    "draft_id": str(draft.id),
                    "candidates": [
                        [str(o.id) for o in group] for group in candidates
                    ],
                },
            )
        chosen = candidates[0]
    else:
        chosen = [await object_service.get_object(reads_object_id, owner=owner)]
        if mate_object_id is not None:
            chosen.append(
                await object_service.get_object(mate_object_id, owner=owner)
            )
        for obj in chosen:
            if not reference_assembly.is_short_read(obj):
                raise ValidationError(
                    f"{obj.name!r} is not short-read data; Polypolish "
                    "corrects a draft using short reads and running it on "
                    "long reads would degrade the assembly",
                    details={"object_id": str(obj.id)},
                )

    draft_digest, draft_path = await _resolve_readable(draft)
    payload: dict = {
        "draft_object_id": str(draft.id),
        "draft_name": draft.name,
        "threads": 8,
    }
    if draft_digest:
        payload["draft_sha256"] = draft_digest
    if draft_path:
        payload["draft_path"] = draft_path

    for slot, obj in zip(("reads", "mate"), chosen, strict=False):
        digest, path = await _resolve_readable(obj)
        payload[f"{slot}_object_id"] = str(obj.id)
        payload[f"{slot}_name"] = obj.name
        if digest:
            payload[f"{slot}_sha256"] = digest
        if path:
            payload[f"{slot}_path"] = path

    # Depth decides --careful, so it is computed here rather than in the
    # handler: the handler sees paths, not the objects carrying the facts.
    assembly_length = (draft.facts or {}).get("total_bases")
    read_bases = [b for b in (_read_bases(o) for o in chosen) if b]
    payload["depth"] = polypolish_runner.estimate_depth(
        read_bases=sum(read_bases) if read_bases else None,
        assembly_length=int(assembly_length) if assembly_length else None,
    )

    run = await run_service.create_run(
        kind=RunKind.REFERENCE_ASSEMBLY,
        project_id=draft.project_id,
        label=f"Polish {draft.name}",
        inputs=[
            RunInput(
                object_id=draft.id,
                name=draft.name,
                role=RunInputRole.DRAFT_ASSEMBLY,
            ),
            *[
                RunInput(object_id=obj.id, name=obj.name, role=role)
                for role, obj in zip(
                    (RunInputRole.READS, RunInputRole.MATE), chosen, strict=False
                )
            ],
        ],
        params={"threads": payload["threads"], "depth": payload["depth"]},
        owner=owner,
        tool="polypolish",
    )

    job = await queue.enqueue(
        "polish_assembly",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        # Sized for bwa-mem2, not for Polypolish -- see the handler's own
        # note on why peak RSS here scales with the draft rather than the
        # reads.
        resources=JobResources(cpu=8, mem_mb=POLISH_MEM_MB, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=f"polish:{draft.id}:{chosen[0].id}",
        project_id=draft.project_id,
        object_id=draft.id,
        resource_override=resource_override,
    )
    if job is None:
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "Polishing is already queued or running for this assembly",
            details={"object_id": str(draft.id)},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.POLISH)
    log.info(
        "polish_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        draft_id=str(draft.id),
        read_files=len(chosen),
        depth=payload["depth"],
        tool_version=tool.version,
    )
    return job


async def launch_polish_long(
    *,
    draft_object_id: PydanticObjectId,
    owner: str,
    reads_object_id: PydanticObjectId | None = None,
    bacteria: bool = False,
    resource_override: bool = False,
) -> Job:
    """Queue a Medaka run: long reads correcting a draft assembly.

    Same provenance shape as `launch_polish` -- the handler aligns the reads
    to the draft itself, so the alignment target is correct by construction
    rather than by check. Unlike Polypolish, the aligner is not ours to
    name: Medaka resolves its own minimap2 preset from the model.

    Reads are resolved from the project when not named explicitly, and only
    when the choice is unambiguous. `reference_assembly.long_read_sets` is
    what decides which candidates are eligible, and there is no mate slot --
    ONT and PacBio data is unpaired.

    `bacteria` opts into ONT's bacterial-methylation model. It is a
    parameter rather than an inference: nothing in the object graph reliably
    says a draft is a bacterial isolate, and ONT ships that model as a
    research release.
    """
    from app.queue import queue
    from app.services import object_service, reference_assembly, run_service

    refuse_if_over_budget(
        declared_mb=POLISH_LONG_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tool = tools.require(tools.medaka())

    draft = await object_service.get_object(draft_object_id, owner=owner)
    reference_assembly.check_draft_assembly(draft)

    if reads_object_id is None:
        candidates = reference_assembly.long_read_sets(
            await object_service.list_objects(
                draft.project_id, owner=owner, status=ObjectStatus.READY
            )
        )
        if not candidates:
            raise ValidationError(
                "Polishing with Medaka needs long reads, and this project "
                "has none",
                details={"draft_id": str(draft.id)},
            )
        if len(candidates) > 1:
            raise ValidationError(
                "This project has several long-read sets; name the one to "
                "polish with",
                details={
                    "draft_id": str(draft.id),
                    "candidates": [
                        [str(o.id) for o in group] for group in candidates
                    ],
                },
            )
        chosen = candidates[0][0]
    else:
        chosen = await object_service.get_object(reads_object_id, owner=owner)
        if not reference_assembly.is_long_read_for_polishing(chosen):
            raise ValidationError(
                f"{chosen.name!r} is not long-read data; Medaka corrects a "
                "draft using the long reads it was assembled from, and its "
                "models are trained on long-read error profiles",
                details={"object_id": str(chosen.id)},
            )

    draft_digest, draft_path = await _resolve_readable(draft)
    payload: dict = {
        "draft_object_id": str(draft.id),
        "draft_name": draft.name,
        "threads": 8,
        "bacteria": bacteria,
    }
    if draft_digest:
        payload["draft_sha256"] = draft_digest
    if draft_path:
        payload["draft_path"] = draft_path

    reads_digest, reads_path = await _resolve_readable(chosen)
    payload["reads_object_id"] = str(chosen.id)
    payload["reads_name"] = chosen.name
    if reads_digest:
        payload["reads_sha256"] = reads_digest
    if reads_path:
        payload["reads_path"] = reads_path

    run = await run_service.create_run(
        kind=RunKind.REFERENCE_ASSEMBLY,
        project_id=draft.project_id,
        label=f"Polish {draft.name} (Medaka)",
        inputs=[
            RunInput(
                object_id=draft.id,
                name=draft.name,
                role=RunInputRole.DRAFT_ASSEMBLY,
            ),
            RunInput(
                object_id=chosen.id,
                name=chosen.name,
                role=RunInputRole.READS,
            ),
        ],
        params={"threads": payload["threads"], "bacteria": bacteria},
        owner=owner,
        tool="medaka",
    )

    job = await queue.enqueue(
        "polish_long_assembly",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(
            cpu=8, mem_mb=POLISH_LONG_MEM_MB, io=IoClass.HEAVY
        ),
        max_attempts=1,
        dedup_key=f"polish_long:{draft.id}:{chosen.id}",
        project_id=draft.project_id,
        object_id=draft.id,
        resource_override=resource_override,
    )
    if job is None:
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "Medaka polishing is already queued or running for this assembly",
            details={"object_id": str(draft.id)},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.POLISH)
    log.info(
        "polish_long_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        draft_id=str(draft.id),
        reads_id=str(chosen.id),
        bacteria=bacteria,
        tool_version=tool.version,
    )
    return job


async def launch_scaffold(
    *,
    draft_object_id: PydanticObjectId,
    owner: str,
    reference_object_id: PydanticObjectId | None = None,
    divergence: str | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue a RagTag run: order and orient a draft assembly's contigs
    against a reference.

    Same provenance shape as `launch_polish`: RagTag invokes minimap2 itself
    (verified from its own log), so there is no BAM to check and the
    alignment target is correct by construction. Recorded as facts on the
    output, not validated at launch -- see reference_assembly_handlers'
    module docstring for why this slice and Polypolish share that shape
    while iVar does not.

    Unlike polishing, the reference must be named or unambiguous: a project
    holding two reference-role FASTA (the ordinary case -- the real yeast
    project carries both the GCA and GCF genomic FASTA for one organism) is
    a real ambiguity, not an edge case, so `reference_object_id` is expected
    to arrive from a dialog's chooser rather than resolved silently the way
    `launch_polish` resolves reads.
    """
    from app.queue import queue
    from app.services import object_service, reference_assembly, run_service

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=SCAFFOLD_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tool = tools.require(tools.ragtag())

    draft = await object_service.get_object(draft_object_id, owner=owner)
    reference_assembly.check_draft_assembly(draft)

    if reference_object_id is None:
        candidates = [
            o
            for o in await object_service.list_objects(
                draft.project_id, owner=owner, status=ObjectStatus.READY
            )
            if o.role is ObjectRole.REFERENCE and o.format.kind is FormatKind.FASTA
        ]
        if not candidates:
            raise ValidationError(
                "Scaffolding needs a reference assembly, and this project "
                "has none",
                details={"draft_id": str(draft.id)},
            )
        if len(candidates) > 1:
            raise ValidationError(
                "This project has several reference assemblies; name the "
                "one to scaffold against",
                details={
                    "draft_id": str(draft.id),
                    "candidates": [str(o.id) for o in candidates],
                },
            )
        reference = candidates[0]
    else:
        reference = await object_service.get_object(
            reference_object_id, owner=owner
        )

    reference_assembly.check_reference_assembly(reference)

    if reference.id == draft.id:
        raise ValidationError(
            "The draft and the reference cannot be the same object",
            details={"object_id": str(draft.id)},
        )

    draft_digest, draft_path = await _resolve_readable(draft)
    ref_digest, ref_path = await _resolve_readable(reference)

    divergence = divergence or ragtag_runner.Divergence.SAME_SPECIES
    payload: dict = {
        "draft_object_id": str(draft.id),
        "draft_name": draft.name,
        "reference_object_id": str(reference.id),
        "reference_name": reference.name,
        "divergence": divergence,
        "threads": 4,
    }
    if draft_digest:
        payload["draft_sha256"] = draft_digest
    if draft_path:
        payload["draft_path"] = draft_path
    if ref_digest:
        payload["reference_sha256"] = ref_digest
    if ref_path:
        payload["reference_path"] = ref_path

    run = await run_service.create_run(
        kind=RunKind.REFERENCE_ASSEMBLY,
        project_id=draft.project_id,
        label=f"Scaffold {draft.name} against {reference.name}",
        inputs=[
            RunInput(
                object_id=draft.id,
                name=draft.name,
                role=RunInputRole.DRAFT_ASSEMBLY,
            ),
            RunInput(
                object_id=reference.id,
                name=reference.name,
                role=RunInputRole.REFERENCE,
            ),
        ],
        params={"divergence": divergence, "threads": payload["threads"]},
        owner=owner,
        tool="ragtag",
    )

    job = await queue.enqueue(
        "scaffold_assembly",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        # Sized for minimap2's whole-genome alignment, not for RagTag's own
        # graph work -- see the handler's own note.
        resources=JobResources(cpu=4, mem_mb=SCAFFOLD_MEM_MB, io=IoClass.LIGHT),
        max_attempts=1,
        dedup_key=f"scaffold:{draft.id}:{reference.id}",
        project_id=draft.project_id,
        object_id=draft.id,
        resource_override=resource_override,
    )
    if job is None:
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "Scaffolding is already queued or running for this assembly "
            "against this reference",
            details={"object_id": str(draft.id)},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.SCAFFOLD)
    log.info(
        "scaffold_launched",
        job_id=str(job.id),
        run_id=str(run.id),
        draft_id=str(draft.id),
        reference_id=str(reference.id),
        divergence=divergence,
        tool_version=tool.version,
    )
    return job


async def launch_misassembly_qc(
    *,
    draft_object_id: PydanticObjectId,
    owner: str,
    reference_object_id: PydanticObjectId | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue a QUAST run: reference-based misassembly QC for one assembly.

    Same reference-resolution shape as `launch_scaffold`, since both take an
    assembly-shaped draft plus a reference and both treat "a project holds
    more than one reference-role FASTA" as the ordinary case rather than an
    edge case -- see `launch_scaffold`'s own docstring. `reference_object_id`
    is expected to arrive from a dialog's chooser in the ambiguous case; the
    Actions card only fires when exactly one candidate exists, so it never
    needs to supply this argument at all.

    Read-only: this never produces a new object, only facts merged onto the
    draft. `--min-contig` is deliberately not a parameter here -- QUAST's
    default (500) is what every run in this application uses, so counts
    across runs stay comparable; the value is recorded as a fact regardless,
    so a report's contig count can still be explained against it.
    """
    from app.queue import queue
    from app.services import object_service, reference_assembly

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=MISASSEMBLY_QC_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tool = tools.require(tools.quast())

    draft = await object_service.get_object(draft_object_id, owner=owner)
    reference_assembly.check_draft_assembly(draft)

    if reference_object_id is None:
        candidates = [
            o
            for o in await object_service.list_objects(
                draft.project_id, owner=owner, status=ObjectStatus.READY
            )
            if o.role is ObjectRole.REFERENCE
            and o.format.kind is FormatKind.FASTA
            and o.id != draft.id
        ]
        if not candidates:
            raise ValidationError(
                "Misassembly QC needs a reference assembly, and this "
                "project has none",
                details={"draft_id": str(draft.id)},
            )
        if len(candidates) > 1:
            raise ValidationError(
                "This project has several reference assemblies; name the "
                "one to check against",
                details={
                    "draft_id": str(draft.id),
                    "candidates": [str(o.id) for o in candidates],
                },
            )
        reference = candidates[0]
    else:
        reference = await object_service.get_object(
            reference_object_id, owner=owner
        )

    reference_assembly.check_reference_assembly(reference)

    if reference.project_id != draft.project_id:
        raise ValidationError(
            "The draft and the reference must be in the same project"
        )

    if reference.id == draft.id:
        # QUAST would happily report a perfect assembly against itself --
        # the most misleading possible success, and nothing else in the
        # validation stack above catches this specific pairing.
        raise ValidationError(
            "The draft and the reference cannot be the same object",
            details={"object_id": str(draft.id)},
        )

    draft_digest, draft_path = await _resolve_readable(draft)
    ref_digest, ref_path = await _resolve_readable(reference)

    payload: dict = {
        "object_id": str(draft.id),
        "reference_object_id": str(reference.id),
        "reference_name": reference.name,
        "threads": 4,
    }
    if draft_digest:
        payload["assembly_sha256"] = draft_digest
    if draft_path:
        payload["assembly_path"] = draft_path
    if ref_digest:
        payload["reference_sha256"] = ref_digest
    if ref_path:
        payload["reference_path"] = ref_path

    job = await queue.enqueue(
        "assess_misassemblies",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=4, mem_mb=MISASSEMBLY_QC_MEM_MB, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=f"assess_misassemblies:{draft.id}:{reference.id}",
        project_id=draft.project_id,
        object_id=draft.id,
        resource_override=resource_override,
    )
    if job is None:
        raise ConflictError(
            "Misassembly QC is already queued or running for this "
            "assembly against this reference",
            details={"object_id": str(draft.id)},
        )

    log.info(
        "misassembly_qc_launched",
        job_id=str(job.id),
        draft_id=str(draft.id),
        reference_id=str(reference.id),
        tool_version=tool.version,
    )
    return job


async def launch_synteny(
    *,
    draft_object_id: PydanticObjectId,
    owner: str,
    reference_object_id: PydanticObjectId | None = None,
    divergence: str | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue a minimap2 run: whole-genome alignment of a draft assembly
    against a reference, for a synteny dot-plot.

    Same reference-resolution shape as `launch_scaffold` and
    `launch_misassembly_qc`, since all three take an assembly-shaped draft
    plus a reference and treat "a project holds more than one reference-role
    FASTA" as the ordinary case rather than an edge case -- the real yeast
    project carries both the GCA and GCF genomic FASTA for one organism, so
    picking one silently would be a guess, not a resolution.
    `reference_object_id` is expected to arrive from a dialog's chooser in
    the ambiguous case.

    Read-only: this never produces a new object, only facts (the parsed PAF
    alignment) merged onto the draft -- matches `launch_misassembly_qc` and
    `launch_completeness`, not `launch_scaffold`, so there is no run record
    and no `run_service.link_job` call.
    """
    from app.queue import queue
    from app.services import object_service, reference_assembly

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=SYNTENY_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tool = tools.require(tools.minimap2())

    draft = await object_service.get_object(draft_object_id, owner=owner)
    reference_assembly.check_draft_assembly(draft)

    if reference_object_id is None:
        candidates = [
            o
            for o in await object_service.list_objects(
                draft.project_id, owner=owner, status=ObjectStatus.READY
            )
            if o.role is ObjectRole.REFERENCE
            and o.format.kind is FormatKind.FASTA
            and o.id != draft.id
        ]
        if not candidates:
            raise ValidationError(
                "Synteny analysis needs a reference assembly, and this "
                "project has none",
                details={"draft_id": str(draft.id)},
            )
        if len(candidates) > 1:
            raise ValidationError(
                "This project has several reference assemblies; name the "
                "one to compare against",
                details={
                    "draft_id": str(draft.id),
                    "candidates": [str(o.id) for o in candidates],
                },
            )
        reference = candidates[0]
    else:
        reference = await object_service.get_object(
            reference_object_id, owner=owner
        )

    reference_assembly.check_reference_assembly(reference)

    if reference.project_id != draft.project_id:
        raise ValidationError(
            "The draft and the reference must be in the same project"
        )

    if reference.id == draft.id:
        raise ValidationError(
            "The draft and the reference cannot be the same object",
            details={"object_id": str(draft.id)},
        )

    draft_digest, draft_path = await _resolve_readable(draft)
    ref_digest, ref_path = await _resolve_readable(reference)

    divergence = divergence or ragtag_runner.Divergence.SAME_SPECIES
    payload: dict = {
        "object_id": str(draft.id),
        "draft_name": draft.name,
        "reference_object_id": str(reference.id),
        "reference_name": reference.name,
        "divergence": divergence,
        "threads": 4,
    }
    if draft_digest:
        payload["draft_sha256"] = draft_digest
    if draft_path:
        payload["draft_path"] = draft_path
    if ref_digest:
        payload["reference_sha256"] = ref_digest
    if ref_path:
        payload["reference_path"] = ref_path

    job = await queue.enqueue(
        "analyze_synteny",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=4, mem_mb=SYNTENY_MEM_MB, io=IoClass.LIGHT),
        max_attempts=1,
        dedup_key=f"analyze_synteny:{draft.id}:{reference.id}",
        project_id=draft.project_id,
        object_id=draft.id,
        resource_override=resource_override,
    )
    if job is None:
        raise ConflictError(
            "Synteny analysis is already queued or running for this "
            "assembly against this reference",
            details={"object_id": str(draft.id)},
        )

    log.info(
        "synteny_launched",
        job_id=str(job.id),
        draft_id=str(draft.id),
        reference_id=str(reference.id),
        divergence=divergence,
        tool_version=tool.version,
    )
    return job


async def launch_assembly_error_qc(
    *,
    object_id: PydanticObjectId,
    owner: str,
    ngs_bam_id: PydanticObjectId | None = None,
    sms_bam_id: PydanticObjectId | None = None,
    break_chimera: bool = False,
    resource_override: bool = False,
) -> Job:
    """Queue a CRAQ run: reference-free assembly error detection.

    Auto-pairs when unambiguous -- exactly one short-read BAM and/or exactly
    one long-read BAM against this assembly -- and refuses otherwise, the
    same "ambiguity is a chooser, not a guess" rule `launch_misassembly_qc`
    follows for references. Explicit ids come from the dialog.

    Read-only unless `break_chimera`, which is opt-in per run and never set
    by the Actions card.
    """
    from app.queue import queue
    from app.services import object_service, reference_assembly

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=ASSEMBLY_ERROR_QC_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tool = tools.require(tools.craq())

    assembly = await object_service.get_object(object_id, owner=owner)
    reference_assembly.check_draft_assembly(assembly)

    if ngs_bam_id is None and sms_bam_id is None:
        short, long_, _unknown = await alignments_against(assembly, owner=owner)
        if not short and not long_:
            raise ValidationError(
                "Assembly error detection needs reads aligned to this "
                "assembly, and this project has none",
                details={"object_id": str(assembly.id)},
            )
        if len(short) > 1 or len(long_) > 1:
            raise ValidationError(
                "This assembly has several alignments; name the ones to use",
                details={
                    "short": [str(o.id) for o in short],
                    "long": [str(o.id) for o in long_],
                },
            )
        ngs_bam = short[0] if short else None
        sms_bam = long_[0] if long_ else None
    else:
        ngs_bam = (
            await object_service.get_object(ngs_bam_id, owner=owner)
            if ngs_bam_id
            else None
        )
        sms_bam = (
            await object_service.get_object(sms_bam_id, owner=owner)
            if sms_bam_id
            else None
        )

    payload: dict = {
        "object_id": str(assembly.id),
        "threads": 4,
        "break_chimera": break_chimera,
    }

    asm_digest, asm_path = await _resolve_readable(assembly)
    if asm_digest:
        payload["assembly_sha256"] = asm_digest
    if asm_path:
        payload["assembly_path"] = asm_path

    for bam, prefix, bai_prefix in (
        (ngs_bam, "ngs_bam", "ngs_bai"),
        (sms_bam, "sms_bam", "sms_bai"),
    ):
        if bam is None:
            continue
        # Validated provenance, not trust: a BAM aligned to some *other*
        # assembly would produce clipping signals that describe the wrong
        # sequence and read as errors in this one.
        if assembly.id not in bam.derived_from:
            raise ValidationError(
                f"{bam.name} was not aligned against this assembly",
                details={"bam_id": str(bam.id), "object_id": str(assembly.id)},
            )
        digest, path = await _resolve_readable(bam)
        if digest:
            payload[f"{prefix}_sha256"] = digest
        if path:
            payload[f"{prefix}_path"] = path
        payload[f"{prefix}_object_id"] = str(bam.id)

        # BioFlow's storage is content-addressed, so a BAM and its .bai
        # are separate DataObjects with no path relationship -- resolve
        # the sidecar explicitly, the same way `launch_bam_stats` already
        # does (`bai_sha256`/`bai_path`). The handler reads
        # `{bai_prefix}_sha256`/`{bai_prefix}_path` via
        # `_resolve_input(payload, bai_prefix)` (called with `"ngs_bai"` /
        # `"sms_bai"`, NOT `f"{prefix}_bai"`) and raises PermanentError if
        # neither resolves -- a missing index is not silently tolerated.
        bai = await _sidecar_of_role(bam, SidecarRole.BAI)
        if bai is not None:
            bai_digest, bai_path = await _resolve_readable(bai)
            if bai_digest:
                payload[f"{bai_prefix}_sha256"] = bai_digest
            if bai_path:
                payload[f"{bai_prefix}_path"] = bai_path

    dedup = f"assess_assembly_errors:{assembly.id}:{ngs_bam.id if ngs_bam else '-'}"
    dedup += f":{sms_bam.id if sms_bam else '-'}:{break_chimera}"

    job = await queue.enqueue(
        "assess_assembly_errors",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=4, mem_mb=ASSEMBLY_ERROR_QC_MEM_MB, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=dedup,
        project_id=assembly.project_id,
        object_id=assembly.id,
        resource_override=resource_override,
    )
    if job is None:
        raise ConflictError("This assembly error QC job is already queued")

    log.info(
        "assembly_error_qc_launched",
        job_id=str(job.id),
        object_id=str(assembly.id),
        ngs_bam_id=str(ngs_bam.id) if ngs_bam else None,
        sms_bam_id=str(sms_bam.id) if sms_bam else None,
        break_chimera=break_chimera,
        tool_version=tool.version,
    )
    return job


async def _materialize_meryl_cache(
    read_object: DataObject, k: int, *, owner: str
) -> Path | None:
    """Reassemble a cached MERYL_DB sidecar group for `read_object` at `k`
    into a directory on local disk, or return None if no usable cache exists.

    Task 4's applier (`_apply_assess_assembly_qv` in `app/queue/results.py`)
    ingests every file inside a fresh meryl database as its own sidecar on
    the read object, tagged `SidecarRole.MERYL_DB` and named
    `f"{db_dir.name}__{relative_path_with_slashes_as_underscoreunderscore}"`,
    with `facts={"meryl_db_k": k, "meryl_db_name": db_dir.name}`. This is the
    read side of that scheme -- nothing else in the codebase reassembles a
    flat sidecar group back into a directory the way STAR's index does for
    aligners, so this is new logic, not a reuse of an existing helper.

    Grouped by `meryl_db_name` first (a read object could in principle carry
    more than one cached database if k ever changed across runs), then
    filtered to the group whose every member's `meryl_db_k` matches `k`.
    Meryl's own `qv.sh` reads k back out of the database rather than taking
    it as an argument, so a database built at a different k cannot serve a
    run wanting this one -- it is simply a different, unusable database.

    A member with no resolvable name-encoded relative path, a group whose
    size does not match the `meryl_db_expected_count` the applier stamped on
    each member, or a group where resolving any member's bytes fails, is
    treated as a broken cache: this function returns None (never raises) so
    the caller falls back to a full rebuild. Correctness over cleverness --
    a rebuild costs time; silently running Merqury against a partial
    database would produce a confidently wrong QV rather than a visible
    error, and there's no way to detect that afterward from the QV number
    alone.
    """
    from app.services import object_service

    try:
        sidecars = await object_service.list_sidecars(read_object.id, owner=owner)
    except Exception:  # noqa: BLE001 - a lookup failure means "no cache", not a crash
        log.warning(
            "meryl_cache_lookup_failed", read_object_id=str(read_object.id), exc_info=True
        )
        return None

    groups: dict[str, list[DataObject]] = {}
    for sc in sidecars:
        if sc.sidecar_role is not SidecarRole.MERYL_DB:
            continue
        facts = sc.facts or {}
        name = facts.get("meryl_db_name")
        if not name:
            continue
        groups.setdefault(name, []).append(sc)

    for db_name, members in groups.items():
        if not members:
            continue
        if any((m.facts or {}).get("meryl_db_k") != k for m in members):
            continue

        # The applier stamps the total file count it intended to ingest on
        # every member that made it (`meryl_db_expected_count`). Comparing
        # against the group's actual size is the only way to tell a complete
        # database from one a partial ingest silently shrank -- without it, a
        # database that lost files to `meryl_db_partially_applied` looks
        # identical to a genuinely complete one, and this function would
        # materialize a truncated database that Merqury would score as if it
        # were whole. A group with no `meryl_db_expected_count` at all
        # predates this field and is treated the same as a mismatch: unknown
        # completeness is not evidence of completeness.
        expected_counts = {(m.facts or {}).get("meryl_db_expected_count") for m in members}
        if len(expected_counts) != 1 or expected_counts != {len(members)}:
            log.warning(
                "meryl_cache_group_incomplete",
                read_object_id=str(read_object.id),
                db_name=db_name,
                found=len(members),
                expected=next(iter(expected_counts), None),
            )
            continue

        prefix = f"{db_name}__"
        dest_dir = settings.tmp_dir / "meryl_cache" / str(read_object.id) / db_name
        try:
            if dest_dir.exists():
                shutil.rmtree(dest_dir, ignore_errors=True)
            dest_dir.mkdir(parents=True, exist_ok=True)

            linked_any = False
            for member in members:
                if not member.name.startswith(prefix):
                    # Reverse of the applier's own flattening rule -- a
                    # member whose stored name does not carry this
                    # database's prefix cannot be placed correctly, and
                    # guessing would risk writing a file under the wrong
                    # relative path inside the reconstructed database.
                    log.warning(
                        "meryl_cache_member_name_mismatch",
                        read_object_id=str(read_object.id),
                        db_name=db_name,
                        member_name=member.name,
                    )
                    raise ValueError("unrecoverable member name")

                relative = member.name[len(prefix) :].replace("__", "/")
                if not relative or ".." in Path(relative).parts:
                    log.warning(
                        "meryl_cache_member_path_unsafe",
                        read_object_id=str(read_object.id),
                        db_name=db_name,
                        member_name=member.name,
                    )
                    raise ValueError("unsafe member path")

                digest, ext_path = await _resolve_readable(member)
                if ext_path:
                    source = Path(ext_path)
                elif digest:
                    source = blob_path(digest)
                else:
                    raise ValueError("member has no resolvable content")
                if not source.exists():
                    raise ValueError(f"blob missing on disk: {source}")

                target = dest_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.unlink(missing_ok=True)
                target.symlink_to(source)
                linked_any = True

            if not linked_any:
                raise ValueError("no members linked")
        except Exception as e:  # noqa: BLE001 - any reconstruction failure -> rebuild, never crash
            log.warning(
                "meryl_cache_materialize_failed",
                read_object_id=str(read_object.id),
                db_name=db_name,
                k=k,
                error=str(e),
            )
            shutil.rmtree(dest_dir, ignore_errors=True)
            return None

        log.info(
            "meryl_cache_hit",
            read_object_id=str(read_object.id),
            db_name=db_name,
            k=k,
            members=len(members),
        )
        return dest_dir

    return None


async def launch_qv_qc(
    object_id: PydanticObjectId,
    *,
    owner: str,
    read_object_id: PydanticObjectId | None = None,
    k: int | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue a Merqury QV run: reference-free k-mer base accuracy for one
    assembly, scored against the reads it came from.

    Ambiguity is unavailable, not a guess, the same rule
    `launch_assembly_error_qc` and `build_polish_card` both follow: an
    explicit `read_object_id` is always honored, and with none given this
    auto-picks only when the project holds exactly one read set. A wrong
    pairing here would not error -- it would produce a plausible, confidently
    wrong QV number for a genome the reads never came from.

    Unlike polish, this is not restricted to short reads: Merqury's k-mer
    comparison works for any read chemistry, so every read set in the
    project -- grouped by `reference_assembly.group_read_sets`, the same
    mate-pairing logic `short_read_sets` itself is built on -- is a
    candidate.

    A cached MERYL_DB sidecar group on the chosen read set at this exact `k`
    is reused via `_materialize_meryl_cache`; otherwise `read_db_path` is
    left unset and the handler builds a fresh database (and Task 4's
    applier caches it for next time).
    """
    from app.queue import queue
    from app.services import object_service, reference_assembly

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=QV_QC_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tools.require(tools.meryl())
    tool = tools.require(tools.merqury())

    assembly = await object_service.get_object(object_id, owner=owner)
    reference_assembly.check_draft_assembly(assembly)

    if read_object_id is None:
        candidates = reference_assembly.group_read_sets(
            [
                o
                for o in await object_service.list_objects(
                    assembly.project_id, owner=owner, status=ObjectStatus.READY
                )
                if o.format.kind is FormatKind.FASTQ
            ]
        )
        if not candidates:
            raise ValidationError(
                "QV assessment needs the reads this assembly was built "
                "from, and this project has none",
                details={"object_id": str(assembly.id)},
            )
        if len(candidates) > 1:
            raise ValidationError(
                "This project has several read sets; name the one to score "
                "QV against",
                details={
                    "object_id": str(assembly.id),
                    "candidates": [
                        [str(o.id) for o in group] for group in candidates
                    ],
                },
            )
        chosen = candidates[0]
    else:
        primary = await object_service.get_object(read_object_id, owner=owner)
        # get_object scopes by owner, not project -- a read set from another
        # project of the same owner would otherwise enqueue without error
        # and score this assembly's QV against reads it has nothing to do
        # with, exactly the "plausible, confidently wrong" outcome this
        # function's docstring warns about. The auto-pick branch above is
        # safe by construction (candidates are drawn from
        # assembly.project_id already); this is the one path where a wrong
        # pairing is actually reachable.
        if primary.project_id != assembly.project_id:
            raise ValidationError(
                "Reads and assembly must be in the same project",
                details={
                    "object_id": str(assembly.id),
                    "read_object_id": str(primary.id),
                },
            )
        chosen = [primary]
        mate_id = getattr(primary, "mate_object_id", None)
        if mate_id is not None:
            chosen.append(await object_service.get_object(mate_id, owner=owner))

    resolved_k = int(k) if k else DEFAULT_MERYL_K
    read_obj = chosen[0]

    read_db_path = await _materialize_meryl_cache(read_obj, resolved_k, owner=owner)

    asm_digest, asm_path = await _resolve_readable(assembly)
    payload: dict = {
        "object_id": str(assembly.id),
        "k": resolved_k,
        "threads": 4,
        "read_object_id": str(read_obj.id),
        "read_object_name": read_obj.name,
    }
    if asm_digest:
        payload["assembly_sha256"] = asm_digest
    if asm_path:
        payload["assembly_path"] = asm_path

    reads_payload = []
    for r in chosen:
        digest, path = await _resolve_readable(r)
        # name rides along so the handler can link this file under its own
        # extension rather than a hardcoded one -- meryl (like every other
        # read-consuming tool here) detects gzip by suffix, and a plain
        # FASTQ linked as .fastq.gz silently counts zero k-mers rather than
        # erroring, confirmed against a real DRR1066343_1.fastq run.
        entry: dict = {"read_name": r.name}
        if digest:
            entry["read_sha256"] = digest
        if path:
            entry["read_path"] = path
        reads_payload.append(entry)
    payload["reads"] = reads_payload

    if read_db_path is not None:
        payload["read_db_path"] = str(read_db_path)

    job = await queue.enqueue(
        "assess_assembly_qv",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        # Matches the handler's own @handler(...) registration -- see
        # assess_assembly_qv's docstring for the real-data measurement this
        # figure is based on.
        resources=JobResources(cpu=4, mem_mb=QV_QC_MEM_MB, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=f"assess_assembly_qv:{assembly.id}:{read_obj.id}:{resolved_k}",
        project_id=assembly.project_id,
        object_id=assembly.id,
        resource_override=resource_override,
    )
    if job is None:
        raise ConflictError("This QV assessment job is already queued")

    log.info(
        "assembly_qv_launched",
        job_id=str(job.id),
        object_id=str(assembly.id),
        read_object_id=str(read_obj.id),
        k=resolved_k,
        cached=read_db_path is not None,
        tool_version=tool.version,
    )
    return job


async def _gci_candidates(
    long_: list[DataObject],
) -> tuple[list[DataObject], list[DataObject]]:
    """Split `alignments_against`'s `long_` bucket into GCI's two slots.

    `alignments_against` folds HIFI/CLR/ONT_SIMPLEX/ONT_DUPLEX into one
    "long-read" bucket, which is right for CRAQ (one long-read slot) and
    not fine-grained enough for GCI (two slots, and CLR refused outright --
    see `gci_slot_for_chemistry`). Any BAM whose chemistry resolves to
    `None` here -- CLR, or truly unknown -- is dropped rather than folded
    into either bucket, so it can never be silently auto-paired.
    """
    hifi_candidates: list[DataObject] = []
    nano_candidates: list[DataObject] = []
    for bam in long_:
        chemistry = await read_chemistry_for_alignment(bam)
        slot = gci_slot_for_chemistry(chemistry)
        if slot == "hifi":
            hifi_candidates.append(bam)
        elif slot == "nano":
            nano_candidates.append(bam)
    return hifi_candidates, nano_candidates


def _group_gci_candidates_by_aligner(
    candidates: list[DataObject],
) -> dict[str, list[DataObject]]:
    """Group one GCI slot's candidates by which aligner produced them.

    Two aligners means two usable HiFi BAMs against one assembly becomes
    the *routine* case (minimap2 + winnowmap, same reads, cross-checking
    each other) rather than the ambiguous one `launch_continuity_qc`'s
    "several long-read alignments; name the ones to use" refusal was
    written for. That refusal still applies when it is genuinely ambiguous
    -- two BAMs from the *same* aligner, which is not a cross-check, it is
    two candidates with no way to tell which one is meant. Grouping by
    `aligned_by` is what lets the caller tell the two cases apart: every
    group holding exactly one BAM is safe to pass all of, any group holding
    more than one is the refusal case.

    A BAM with no `aligned_by` (register-in-place, or predating the field)
    groups under `"unknown"` rather than being merged into whichever named
    aligner happens to also be present -- an unknown-provenance BAM sharing
    a group with a real "minimap2" entry would silently treat two unrelated
    alignments as safe to combine.
    """
    groups: dict[str, list[DataObject]] = {}
    for bam in candidates:
        key = str((bam.facts or {}).get("aligned_by") or "unknown")
        groups.setdefault(key, []).append(bam)
    return groups


async def launch_continuity_qc(
    *,
    object_id: PydanticObjectId,
    owner: str,
    hifi_bam_ids: list[PydanticObjectId] | None = None,
    nano_bam_ids: list[PydanticObjectId] | None = None,
    map_qual: int | None = None,
    plot: bool | None = None,
    resource_override: bool = False,
) -> Job:
    """Queue a GCI run: long-read assembly continuity inspection.

    Auto-pairs when unambiguous. "Unambiguous" now means "at most one usable
    BAM per aligner per slot", not "at most one BAM per slot" -- two
    aligners (minimap2 and, when installed, winnowmap) make two usable HiFi
    BAMs against one assembly the routine case, not the ambiguous one: they
    are the same reads, meant to be paired for GCI's own cross-check
    recommendation, not two candidates to choose between. The refusal this
    function still raises -- "several long-read alignments; name the ones
    to use" -- fires only when a single aligner contributed more than one
    BAM to a slot, which is the case with no way to tell which one is
    meant. See `_group_gci_candidates_by_aligner`.

    Unlike CRAQ, "usable" excludes CLR: `gci_slot_for_chemistry` refuses it,
    and a CLR BAM found among the project's long-read alignments simply
    never becomes a candidate, in either the auto-pair or the explicit-id
    path.

    Read-only: no derived object, only facts and (optionally) a depth plot.
    """
    from app.queue import queue
    from app.queue.assembly_qc_handlers import GCI_PLOT_MAX_CONTIGS
    from app.services import object_service, reference_assembly

    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=CONTINUITY_QC_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )

    tool = tools.require(tools.gci())

    assembly = await object_service.get_object(object_id, owner=owner)
    reference_assembly.check_draft_assembly(assembly)

    hifi_bam_ids = hifi_bam_ids or []
    nano_bam_ids = nano_bam_ids or []

    if not hifi_bam_ids and not nano_bam_ids:
        short, long_, _unknown = await alignments_against(assembly, owner=owner)
        hifi_candidates, nano_candidates = await _gci_candidates(long_)

        if not hifi_candidates and not nano_candidates:
            if short and not long_:
                raise ValidationError(
                    "GCI needs long reads aligned to this assembly; only "
                    "short-read alignments are available",
                    details={"object_id": str(assembly.id)},
                )
            if long_:
                raise ValidationError(
                    "This assembly has long-read alignments, but none are "
                    "HiFi or ONT -- GCI cannot use PacBio CLR reads",
                    details={"object_id": str(assembly.id)},
                )
            raise ValidationError(
                "Continuity inspection needs long reads aligned to this "
                "assembly, and this project has none",
                details={"object_id": str(assembly.id)},
            )

        hifi_groups = _group_gci_candidates_by_aligner(hifi_candidates)
        nano_groups = _group_gci_candidates_by_aligner(nano_candidates)
        ambiguous = {
            aligner: [str(o.id) for o in group]
            for groups in (hifi_groups, nano_groups)
            for aligner, group in groups.items()
            if len(group) > 1
        }
        if ambiguous:
            raise ValidationError(
                "This assembly has several long-read alignments from the "
                "same aligner; name the ones to use",
                details={"by_aligner": ambiguous},
            )
        hifi_bams = [group[0] for group in hifi_groups.values()]
        nano_bams = [group[0] for group in nano_groups.values()]
    else:
        hifi_bams = [
            await object_service.get_object(bam_id, owner=owner)
            for bam_id in hifi_bam_ids
        ]
        nano_bams = [
            await object_service.get_object(bam_id, owner=owner)
            for bam_id in nano_bam_ids
        ]
        # The explicit-id path must enforce the same chemistry routing the
        # auto-pair path enforces, or a dialog client could pass any BAM id
        # under `hifi_bam_ids` regardless of what it actually is -- silently
        # bypassing the CLR refusal that is this task's whole point.
        for bams, ids_field, expected_slot in (
            (hifi_bams, "hifi_bam_ids", "hifi"),
            (nano_bams, "nano_bam_ids", "nano"),
        ):
            for bam in bams:
                if assembly.id not in bam.derived_from:
                    raise ValidationError(
                        f"{bam.name} was not aligned against this assembly",
                        details={
                            "bam_id": str(bam.id),
                            "object_id": str(assembly.id),
                        },
                    )
                chemistry = await read_chemistry_for_alignment(bam)
                slot = gci_slot_for_chemistry(chemistry)
                if slot != expected_slot:
                    raise ValidationError(
                        f"{bam.name} is not a {expected_slot.upper()} "
                        f"alignment ({ids_field} expects {expected_slot})",
                        details={
                            "bam_id": str(bam.id),
                            "object_id": str(assembly.id),
                            "chemistry": chemistry.value if chemistry else None,
                        },
                    )

    if not hifi_bams and not nano_bams:
        raise ValidationError(
            "Continuity inspection needs long reads aligned to this "
            "assembly, and this project has none",
            details={"object_id": str(assembly.id)},
        )

    contig_count = (assembly.facts or {}).get("sequence_count")
    if contig_count is None:
        contig_count = (assembly.facts or {}).get("sequence_count_estimate")
    want_plot = bool(plot)
    allow_plot = want_plot and contig_count is not None and contig_count <= GCI_PLOT_MAX_CONTIGS

    payload: dict = {
        "object_id": str(assembly.id),
        "threads": 8,
        "map_qual": map_qual if map_qual is not None else 30,
        "plot": allow_plot,
    }

    asm_digest, asm_path = await _resolve_readable(assembly)
    if asm_digest:
        payload["assembly_sha256"] = asm_digest
    if asm_path:
        payload["assembly_path"] = asm_path

    for bams, slot_key in ((hifi_bams, "hifi_bams"), (nano_bams, "nano_bams")):
        entries: list[dict] = []
        for bam in bams:
            # Validated provenance, not trust -- same reasoning as
            # `launch_assembly_error_qc`.
            if assembly.id not in bam.derived_from:
                raise ValidationError(
                    f"{bam.name} was not aligned against this assembly",
                    details={"bam_id": str(bam.id), "object_id": str(assembly.id)},
                )
            entry: dict = {
                "object_id": str(bam.id),
                "aligned_by": (bam.facts or {}).get("aligned_by"),
            }
            digest, path = await _resolve_readable(bam)
            if digest:
                entry["bam_sha256"] = digest
            if path:
                entry["bam_path"] = path

            # BAM and its .bai are separate content-addressed DataObjects --
            # resolve the sidecar explicitly, the same way
            # `launch_assembly_error_qc` does.
            bai = await _sidecar_of_role(bam, SidecarRole.BAI)
            if bai is not None:
                bai_digest, bai_path = await _resolve_readable(bai)
                if bai_digest:
                    entry["bai_sha256"] = bai_digest
                if bai_path:
                    entry["bai_path"] = bai_path
            entries.append(entry)
        payload[slot_key] = entries

    dedup_hifi = ":".join(sorted(str(b.id) for b in hifi_bams)) or "-"
    dedup_nano = ":".join(sorted(str(b.id) for b in nano_bams)) or "-"
    dedup = f"assess_assembly_continuity:{assembly.id}:{dedup_hifi}:{dedup_nano}"

    job = await queue.enqueue(
        "assess_assembly_continuity",
        owner=owner,
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=8, mem_mb=CONTINUITY_QC_MEM_MB, io=IoClass.HEAVY),
        max_attempts=1,
        dedup_key=dedup,
        project_id=assembly.project_id,
        object_id=assembly.id,
        resource_override=resource_override,
    )
    if job is None:
        raise ConflictError("This assembly continuity QC job is already queued")

    log.info(
        "assembly_continuity_qc_launched",
        job_id=str(job.id),
        object_id=str(assembly.id),
        hifi_bam_ids=[str(b.id) for b in hifi_bams],
        nano_bam_ids=[str(b.id) for b in nano_bams],
        map_qual=payload["map_qual"],
        plot=payload["plot"],
        tool_version=tool.version,
    )
    return job


async def launch_project_export(
    *,
    project_id: PydanticObjectId,
    owner: str,
    threshold_bytes: int | None = None,
    job_class: JobClass = JobClass.USER_BACKGROUND,
) -> Job:
    """Queue an export of a project and its descendants to one archive.

    Raises rather than returning None when the project does not exist or
    belongs to another profile: an export is an explicit user action, and
    silence would look like a broken button.
    """
    from app.queue import queue
    from app.services import export_service, project_service

    project = await project_service.get_project(project_id, owner=owner)

    # No dedup_key is passed, so queue.enqueue's dedup-collision path (its
    # only source of a None return) can never trigger here -- the return
    # type is safely `Job`, unlike launch_continuity_qc nearby, which does
    # pass one and must guard for None.
    return await queue.enqueue(
        "project_export",
        owner=owner,
        payload={
            "project_id": str(project.id),
            "owner": owner,
            "threshold_bytes": (
                threshold_bytes
                if threshold_bytes is not None
                else export_service.DEFAULT_BLOB_THRESHOLD_BYTES
            ),
        },
        job_class=job_class,
        project_id=project.id,
    )


def _parse_fai(fai_path: Path) -> list[tuple[str, int]]:
    """Read a .fai file into (name, length) tuples."""
    result = []
    with open(fai_path) as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    length = int(parts[1])
                except ValueError:
                    raise ValidationError(
                        f"Malformed .fai line {lineno} in {fai_path}: "
                        f"expected integer length, got {parts[1]!r}"
                    ) from None
                result.append((parts[0], length))
    return result
