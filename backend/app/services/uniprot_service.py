"""Launching a UniProt download.

The same shape as `ncbi_assembly_service`: validate the request, build the
payload, and create the run that groups the resulting job. Kept out of the
router so the launch rules are testable without HTTP.

One job for both download shapes, because one UniProt endpoint serves both:
a whole proteome and a hand-picked set differ only in the query string.
Unlike `ncbi_assembly_service` there is no `tools.require` here -- there is no
binary to find, only an HTTP GET.
"""

import re

from beanie import PydanticObjectId

from app.errors import ConflictError, ValidationError
from app.logging import get_logger
from app.metadata import uniprot
from app.models import IoClass, JobClass, JobResources, RunJobRole, RunKind
from app.services import run_service

log = get_logger(__name__)

# A GET query string of OR clauses. UniProt accepts a long one but not an
# unbounded one, and the failure past the limit is an opaque HTTP error
# rather than a message about having asked for too much.
MAX_ACCESSIONS = 500

_PROTEOME_ID = re.compile(r"^UP\d{9}$")


def validate_request(*, proteome_id: str | None, accessions: list[str]) -> None:
    """The request names something downloadable.

    Stricter than the resolver's classification, which guesses at what a user
    typed. By this point the frontend has sent a specific thing, so anything
    malformed is a bug rather than an ambiguous input, and queueing it would
    surface as a UniProt error long after the click.
    """
    if not proteome_id and not accessions:
        raise ValidationError(
            "A download needs a proteome or at least one accession.",
        )

    if proteome_id and accessions:
        # Not merely redundant -- the two disagree downstream. `download_query`
        # gives accessions precedence while `download_label` and
        # `output_filename` give the proteome precedence, so a request naming
        # both produces a file named for a whole proteome that holds only the
        # picked entries. Nothing later can detect that, which is why it is
        # refused here rather than resolved by a precedence rule.
        raise ValidationError(
            "A download names either a proteome or a set of accessions, "
            "not both.",
            details={"proteome_id": proteome_id, "accessions": accessions},
        )

    if proteome_id and not _PROTEOME_ID.match(proteome_id):
        raise ValidationError(
            f"{proteome_id!r} is not a proteome identifier. Expected the "
            "UP000000000 form.",
            details={"proteome_id": proteome_id},
        )

    if len(accessions) > MAX_ACCESSIONS:
        raise ValidationError(
            f"{len(accessions)} accessions is more than the {MAX_ACCESSIONS} "
            "that can be fetched at once. Download in batches.",
            details={"count": len(accessions)},
        )

    for accession in accessions:
        if not uniprot.is_valid_accession(accession):
            raise ValidationError(
                f"{accession!r} is not a UniProt accession.",
                details={"accession": accession},
            )


def download_label(
    *,
    proteome_id: str | None,
    accessions: list[str],
    organism: str | None,
    protein_count: int | None,
) -> str:
    """A one-line description, built at launch.

    Stored rather than derived so the run stays describable after its jobs
    are TTL-pruned -- the same reason `PipelineRun.params` is denormalized.
    This is also where the two download shapes are distinguished, since they
    share one `RunKind`.
    """
    if proteome_id:
        parts = [f"Download {proteome_id}"]
        if organism:
            parts.append(f"({organism})")
        if protein_count:
            parts.append(f"— {protein_count:,} proteins")
        return " ".join(parts)

    if len(accessions) == 1:
        return f"Download {accessions[0]} from UniProt"
    return f"Download {len(accessions)} proteins from UniProt"


def output_filename(
    *, proteome_id: str | None, accessions: list[str], reviewed_only: bool
) -> str:
    """What the ingested file is called.

    The reviewed suffix is not decoration. Human reviewed and human
    unreviewed differ roughly sevenfold, and once both are sitting in a
    project under the same name there is nothing to tell them apart.
    """
    if proteome_id:
        suffix = "reviewed" if reviewed_only else "all"
        return f"{proteome_id}_{suffix}.fasta"
    if len(accessions) == 1:
        return f"{accessions[0]}.fasta"
    return f"uniprot_{len(accessions)}_proteins.fasta"


async def launch_download(
    *,
    project_id: PydanticObjectId,
    proteome_id: str | None,
    accessions: list[str],
    reviewed_only: bool,
    owner: str,
    organism: str | None = None,
    protein_count: int | None = None,
):
    """Queue the download and the run that groups it.

    `owner` gates the project lookup, as in `ncbi_assembly_service.launch_download`:
    the FASTA lands in whichever project it was pointed at, and an unscoped
    lookup would let one profile deposit a proteome into another profile's
    library.
    """
    from app.queue import queue
    from app.services import project_service

    validate_request(proteome_id=proteome_id, accessions=accessions)

    # Resolved for the refusal, not for the value: a project the caller does
    # not own raises NotFoundError here, before any network work.
    await project_service.get_project(project_id, owner=owner)

    query = uniprot.download_query(
        proteome_id=proteome_id, accessions=accessions, reviewed_only=reviewed_only
    )
    filename = output_filename(
        proteome_id=proteome_id, accessions=accessions, reviewed_only=reviewed_only
    )

    run = await run_service.create_run(
        kind=RunKind.UNIPROT_DOWNLOAD,
        project_id=project_id,
        label=download_label(
            proteome_id=proteome_id,
            accessions=accessions,
            organism=organism,
            protein_count=protein_count,
        ),
        inputs=[],  # Nothing in the project is an input; the source is UniProt.
        params={
            "proteome_id": proteome_id,
            "accessions": accessions,
            "reviewed_only": reviewed_only,
            "query": query,
            "source": "uniprot",
        },
        # The caller's profile. The project lookup above is scoped to it, so
        # this is the project's owner too -- the download lands where the
        # person who asked for it can see it.
        owner=owner,
    )

    payload = {
        "project_id": str(project_id),
        "query": query,
        "filename": filename,
        "proteome_id": proteome_id,
        "accessions": accessions,
        "reviewed_only": reviewed_only,
        "organism": organism,
    }

    job = await queue.enqueue(
        "download_uniprot",
        owner=owner,
        payload=payload,
        job_class=JobClass.USER_INTERACTIVE,
        # Far lighter than the assembly download's HEAVY: a yeast proteome is
        # 3.9 MB and human-with-TrEMBL is the worst realistic case.
        resources=JobResources(cpu=1, mem_mb=256, io=IoClass.LIGHT),
        max_attempts=3,
        # Keyed on (query, project) so a double-click collapses, while the
        # same proteome stays downloadable into a second project.
        dedup_key=f"uniprot_download:{query}:{project_id}",
        project_id=project_id,
    )

    if job is None:
        # Already queued or running from an earlier click, so this run
        # describes no work and must not linger in the activity view.
        await run_service.discard_run(run.id, owner=run.owner)
        raise ConflictError(
            "That download is already running",
            details={"query": query},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.DOWNLOAD)

    log.info(
        "uniprot_download_launched",
        run_id=str(run.id),
        project_id=str(project_id),
        query=query,
    )
    return run, [str(job.id)]
