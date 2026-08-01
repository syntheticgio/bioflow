"""Launching SRA downloads.

The same shape as `pipeline_service`: resolve what was asked for, validate it,
build the payloads, and create the run that groups the resulting jobs. Kept out
of the router so the launch rules are testable without HTTP.
"""

from beanie import PydanticObjectId

from app.errors import ConflictError, ValidationError
from app.logging import get_logger
from app.metadata import sra_resolver
from app.models import (
    DataObject,
    IoClass,
    JobClass,
    JobResources,
    RunJobRole,
    RunKind,
)
from app.pipelines import tools
from app.services import run_service

log = get_logger(__name__)

# One user action should not be able to queue an unbounded amount of work. A
# hundred runs is already a multi-terabyte request; past that it is far more
# likely a select-all misclick than an intent.
MAX_RUNS_PER_REQUEST = 100


async def already_downloaded(
    project_id: PydanticObjectId, accessions: list[str], *, owner: str
) -> set[str]:
    """Which of these runs the project already holds.

    Matched on the `sra_run` metadata key that ingest enrichment writes, so a
    file downloaded here *or* uploaded by hand and enriched at ingest both
    count. The resolver surfaces this so the checklist can grey them out rather
    than letting someone spend an hour re-fetching what they have.

    Owner-filtered even though `project_id` already narrows this hard. A
    project does not span profiles today, so the owner clause is redundant --
    but the failure it prevents is a *grey-out*, which is the one answer here
    a user cannot argue with: a checklist that greys a run out because some
    other profile downloaded it tells this profile it has a file it does not
    have, and the run it then declines to fetch is one it will go looking for
    later. Belt and braces, on the query whose wrong answer is silent.
    """
    if not accessions:
        return set()

    objects = await DataObject.find(
        DataObject.project_id == project_id,
        DataObject.owner == owner,
        {"metadata.sra_run": {"$in": accessions}},
    ).to_list()
    return {
        str(o.metadata.get("sra_run"))
        for o in objects
        if o.metadata.get("sra_run")
    }


async def launch_download(
    *,
    project_id: PydanticObjectId,
    run_accessions: list[str],
    owner: str,
    run_qc: bool = True,
):
    """Queue one download job per selected run, grouped into a single run.

    Per-run jobs rather than one job for the batch: a study download is hours
    of work, and one unavailable accession must not lose the other forty. They
    fail and retry independently, and the `PipelineRun` is what makes them read
    as the single action the user took.

    `owner` gates the project lookup. This route spends hours of network and
    terabytes of disk, and the objects it creates land in whichever project it
    was pointed at -- so an unscoped lookup here would let one profile fill
    another profile's project, which is the most expensive version of the
    mistake this partition exists to prevent.
    """
    from app.queue import queue
    from app.services import project_service

    tools.require(tools.fasterq_dump())

    # The selection is validated before the project is fetched: these checks
    # need no database, and answering "no runs selected" should not depend on
    # a round trip that tells us nothing about the answer.
    accessions = _clean_accessions(run_accessions)
    if not accessions:
        raise ValidationError("No runs selected to download")
    if len(accessions) > MAX_RUNS_PER_REQUEST:
        raise ValidationError(
            f"Too many runs in one request: {len(accessions)}. "
            f"The limit is {MAX_RUNS_PER_REQUEST} -- select fewer, or download "
            "in batches.",
            details={"requested": len(accessions), "limit": MAX_RUNS_PER_REQUEST},
        )

    invalid = [a for a in accessions if sra_resolver.classify(a) != "run"]
    if invalid:
        raise ValidationError(
            f"Not run accessions: {', '.join(invalid[:5])}. Only runs "
            "(SRR/ERR/DRR) can be downloaded -- resolve a study to its runs "
            "first.",
            details={"accessions": invalid[:20]},
        )

    # Resolved for the refusal, not for the value: a project the caller does
    # not own raises NotFoundError here, before any of the work below.
    await project_service.get_project(project_id, owner=owner)

    # Resolved once for the whole batch so each job's payload carries its own
    # platform and size. The handler needs the size for its disk pre-flight and
    # the applier needs the platform to pick a QC tool, and neither can query
    # the database or NCBI from a worker thread.
    metadata_by_run = await _resolve_metadata(accessions)

    run = await run_service.create_run(
        kind=RunKind.SRA_DOWNLOAD,
        project_id=project_id,
        label=_download_label(accessions),
        inputs=[],  # Nothing in the project is an input; the source is NCBI.
        params={
            "accessions": accessions,
            "run_qc": run_qc,
            "source": "ncbi_sra",
        },
        # The caller's profile. The project lookup above is scoped to it, so
        # this is the project's owner too -- the download lands where the
        # person who asked for it can see it.
        owner=owner,
    )

    job_ids: list[str] = []
    skipped: list[str] = []
    for accession in accessions:
        meta = metadata_by_run.get(accession, {})
        payload = {
            "accession": accession,
            "project_id": str(project_id),
            "run_qc": run_qc,
            "metadata": meta.get("metadata", {}),
            "platform": meta.get("platform") or "UNKNOWN",
        }
        if meta.get("bytes"):
            payload["bytes_estimate"] = meta["bytes"]

        job = await queue.enqueue(
            "download_sra_run",
            owner=owner,
            payload=payload,
            # USER_INTERACTIVE: someone clicked a button and is watching for
            # the file to appear. The work is IO-bound waiting on NCBI rather
            # than CPU, so it does not belong in the compute class.
            job_class=JobClass.USER_INTERACTIVE,
            resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
            max_attempts=3,
            # Keyed on (run, project) so a double-submit collapses, while the
            # same run stays downloadable into a second project.
            dedup_key=f"sra_download:{accession}:{project_id}",
            project_id=project_id,
        )
        if job is None:
            # Already queued or running from an earlier click. Not an error:
            # the user's intent is satisfied by the job that already exists.
            skipped.append(accession)
            continue

        await run_service.link_job(run.id, job.id, RunJobRole.DOWNLOAD)
        job_ids.append(str(job.id))

    if not job_ids:
        # Every selected run was already in flight, so this run describes no
        # work and must not linger in the activity view implying otherwise.
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "Every selected run is already downloading",
            details={"accessions": skipped},
        )

    log.info(
        "sra_download_launched",
        run_id=str(run.id),
        project_id=str(project_id),
        queued=len(job_ids),
        skipped=len(skipped),
        run_qc=run_qc,
    )
    return run, job_ids, skipped


async def _resolve_metadata(accessions: list[str]) -> dict[str, dict]:
    """Per-run platform, size, and ingest metadata, keyed by accession.

    Best-effort: a resolution failure costs the disk pre-flight and the QC tool
    choice, which both degrade to sensible defaults, so it must not block a
    download the user explicitly asked for.
    """
    out: dict[str, dict] = {}
    for accession in accessions:
        try:
            resolution = await sra_resolver.resolve_cached(accession)
        except Exception as e:  # noqa: BLE001 - metadata is an optimization
            log.warning("sra_metadata_failed", accession=accession, error=str(e))
            continue
        run = next((r for r in resolution.runs if r.accession == accession), None)
        if run is None:
            continue
        out[accession] = {
            "platform": run.platform,
            "bytes": run.bytes,
            "metadata": _ingest_metadata(run),
        }
    return out


def _ingest_metadata(run: sra_resolver.RunInfo) -> dict:
    """Metadata to stamp on the ingested FASTQ.

    Reuses `SraMetadata.to_metadata`'s vocabulary rather than inventing a
    second one, so a downloaded file is annotated identically to an uploaded
    file that was enriched at ingest -- and stays findable by the same search.
    """
    from app.metadata.sra import SraMetadata

    return SraMetadata(
        run=run.accession,
        experiment=run.experiment,
        sample=run.sample,
        study=run.study,
        bioproject=run.bioproject,
        biosample=run.biosample,
        organism=run.organism,
        platform=run.platform,
        instrument=run.instrument,
        library_strategy=run.library_strategy,
        library_source=run.library_source,
        library_layout=run.library_layout,
        total_spots=run.spots,
        total_bases=run.bases,
        sample_attributes=run.sample_attributes,
    ).to_metadata()


def _clean_accessions(raw: list[str]) -> list[str]:
    """Normalized, deduplicated, order-preserving.

    Order matters only for the label; deduplication matters because the queue
    would otherwise reject the second copy as a duplicate and report it as
    "already downloading", which is a confusing way to say "you listed it
    twice".
    """
    seen: set[str] = set()
    out: list[str] = []
    for value in raw or []:
        accession = (value or "").strip().upper()
        if accession and accession not in seen:
            seen.add(accession)
            out.append(accession)
    return out


def _download_label(accessions: list[str]) -> str:
    """A one-line description of the request, built at launch.

    Stored rather than derived so the run stays describable after its jobs are
    TTL-pruned -- the same reason `PipelineRun.params` is denormalized.
    """
    if len(accessions) == 1:
        return f"Download {accessions[0]} from SRA"
    return f"Download {len(accessions)} runs from SRA ({accessions[0]}…)"
