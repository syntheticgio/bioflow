"""Launching an assembly download.

The same shape as `sra_service`: validate the request, build the payload, and
create the run that groups the resulting job. Kept out of the router so the
launch rules are testable without HTTP.

One job rather than one per component: the CLI fetches them in a single
package, so splitting would mean four downloads of overlapping data.
"""

from beanie import PydanticObjectId

from app.errors import ConflictError, ValidationError
from app.logging import get_logger
from app.metadata import ncbi_assembly, ncbi_assembly_components
from app.models import (
    DataObject,
    IoClass,
    JobClass,
    JobResources,
    ObjectRole,
    RunJobRole,
    RunKind,
)
from app.pipelines import tools
from app.services import run_service

log = get_logger(__name__)


def validate_selection(accession: str, components: list[str]) -> list[str]:
    """The components to fetch, normalized and ordered.

    Genome is forced in and unknown names are dropped: both are frontend bugs
    rather than intents, and failing a download over either would be a worse
    answer than quietly doing the sensible thing.
    """
    if not ncbi_assembly.is_valid_accession(accession or ""):
        raise ValidationError(
            f"{accession!r} is not an assembly accession. Expected a GenBank "
            "(GCA_000000000.0) or RefSeq (GCF_000000000.0) accession, "
            "including the version suffix.",
            details={"accession": accession},
        )

    requested = {c.strip().lower() for c in components or []}
    selected = [k for k in ncbi_assembly_components.COMPONENT_ORDER if k in requested]
    if "genome" not in selected:
        selected.insert(0, "genome")
    return selected


def download_label(accession: str, components: list[str]) -> str:
    """A one-line description, built at launch.

    Stored rather than derived so the run stays describable after its jobs are
    TTL-pruned -- the same reason `PipelineRun.params` is denormalized.
    """
    if len(components) == 1:
        return f"Download {accession} from NCBI"
    return f"Download {accession} from NCBI ({len(components)} components)"


async def already_downloaded(
    project_id: PydanticObjectId, accession: str, *, owner: str
) -> bool:
    """Whether this project already holds this assembly's genome.

    Narrowed to the reference role on purpose: a project holding only the
    protein FASTA from this assembly does not have the genome, and answering
    yes would hide the download the user actually wants.

    Matched on `assembly_accession`, which ingest enrichment also writes, so a
    hand-uploaded reference counts too.

    Owner-filtered for the same reason as `sra_service.already_downloaded`:
    `project_id` already narrows this to one profile in practice, but a false
    "yes" here is what disables the download button, and a user told they
    already hold a genome they do not hold has no way to find out otherwise
    from this screen.
    """
    existing = await DataObject.find_one(
        DataObject.project_id == project_id,
        DataObject.owner == owner,
        DataObject.role == ObjectRole.REFERENCE,
        {"metadata.assembly_accession": accession},
    )
    return existing is not None


async def launch_download(
    *,
    project_id: PydanticObjectId,
    accession: str,
    components: list[str],
    owner: str,
):
    """Queue the download and the run that groups it.

    `owner` gates the project lookup, as in `sra_service.launch_download`: the
    reference this fetches lands in whichever project it was pointed at, and
    an unscoped lookup would let one profile deposit a multi-gigabyte genome
    into another profile's library.
    """
    import asyncio

    from app.queue import queue
    from app.services import project_service

    tools.require(tools.datasets())

    accession = (accession or "").strip().upper()
    selected = validate_selection(accession, components)

    # Resolved for the refusal, not for the value: a project the caller does
    # not own raises NotFoundError here, before any network work below.
    await project_service.get_project(project_id, owner=owner)

    # Fetched once here so the handler's disk pre-flight and the ingest
    # metadata are both available: the handler runs in a worker thread and can
    # reach neither the database nor an await.
    #
    # Both calls are synchronous and slow enough to block the event loop --
    # `lookup` is a blocking HTTP request and `component_availability` shells
    # out with up to a 60-second timeout -- so they run in a worker thread,
    # matching `sra_resolver.resolve_cached`.
    meta = await asyncio.to_thread(ncbi_assembly.lookup, accession)
    availability = await asyncio.to_thread(ncbi_assembly.component_availability, accession) or []
    estimate = sum(
        c.size_bytes or 0
        for c in availability
        if c.key in set(selected) and c.size_bytes
    )

    run = await run_service.create_run(
        kind=RunKind.ASSEMBLY_DOWNLOAD,
        project_id=project_id,
        label=download_label(accession, selected),
        inputs=[],  # Nothing in the project is an input; the source is NCBI.
        params={
            "accession": accession,
            "components": selected,
            "source": "ncbi_datasets",
        },
        # The caller's profile. The project lookup above is scoped to it, so
        # this is the project's owner too -- the download lands where the
        # person who asked for it can see it.
        owner=owner,
    )

    payload = {
        "accession": accession,
        "project_id": str(project_id),
        "components": selected,
        "metadata": meta.to_metadata() if meta else {},
        "facts": meta.to_facts() if meta else {},
    }
    if estimate:
        payload["bytes_estimate"] = estimate

    job = await queue.enqueue(
        "download_assembly",
        owner=owner,
        payload=payload,
        job_class=JobClass.USER_INTERACTIVE,
        resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
        max_attempts=3,
        # Keyed on (accession, project) so a double-click collapses, while the
        # same assembly stays downloadable into a second project.
        dedup_key=f"assembly_download:{accession}:{project_id}",
        project_id=project_id,
    )

    if job is None:
        # Already queued or running from an earlier click, so this run
        # describes no work and must not linger in the activity view.
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            f"{accession} is already downloading",
            details={"accession": accession},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.DOWNLOAD)

    log.info(
        "assembly_download_launched",
        run_id=str(run.id),
        project_id=str(project_id),
        accession=accession,
        components=selected,
    )
    return run, [str(job.id)]
