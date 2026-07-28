"""Launching pipeline runs.

Sits between the API and the queue: resolves which files a run will read,
validates that they can actually be trimmed, and builds the payload the
handler expects. Kept out of the router so the launch rules are testable
without HTTP.
"""

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
    ObjectStatus,
    RunInput,
    RunInputRole,
    RunJobRole,
    RunKind,
    SidecarRole,
)
from app.pipelines import align_runner, aligners, fastp_runner, pairing, tools
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


def default_params() -> dict:
    """Server-owned defaults, so the form does not encode its own."""
    params = fastp_runner.TrimParams(threads=settings.pipeline_default_threads)
    return params.as_dict()


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
):
    """Queue a trim run over one object, or an R1/R2 pair.

    `paired=False` forces single-end treatment even when a mate is known, which
    is the escape hatch for a pair that should not be trimmed together.
    """
    from app.queue import queue

    tools.require(tools.fastp())

    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")
    _check_fastq_ready(obj)

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
    payload: dict = {
        "object_id": str(obj.id),
        "project_id": str(obj.project_id),
        "r1_name": obj.name,
        "params": fastp_runner.TrimParams.from_dict(
            {"threads": settings.pipeline_default_threads, **(params or {})}
        ).as_dict(),
    }
    if r1_digest:
        payload["r1_sha256"] = r1_digest
    if r1_path:
        payload["r1_path"] = r1_path

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


def suggested_preset(sam_pl: str) -> str:
    """The minimap2 preset matching a platform, defaulting to short-read."""
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
    preset = suggested_preset(platform)

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
    return tools.bwa_mem2() if aligner is Aligner.BWA_MEM2 else tools.minimap2()


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

    align_params = align_runner.AlignParams.from_dict(
        {**default_align_params(), **(params or {})}
    )
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
    status = await reference_index_status(reference)
    needs_index = not status.get(aligner.value) or not status.get("fai")
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
