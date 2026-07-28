"""NCBI SRA endpoints: resolving an accession, and downloading its runs.

Request and response models live here rather than in `schemas.py`, matching
`pipelines.py`. `schemas.py` holds what several routers share; nothing else
consumes these.
"""

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.logging import get_logger
from app.metadata import sra_resolver
from app.services import sra_service

log = get_logger(__name__)

router = APIRouter(prefix="/sra", tags=["sra"])


class SraResolveRequest(BaseModel):
    accession: str
    # ILLUMINA | PACBIO_SMRT | OXFORD_NANOPORE, or None for everything.
    platform_filter: str | None = None
    # Which project to check for runs already present. Optional: resolving is
    # useful before a project is chosen, it just cannot mark anything.
    project_id: PydanticObjectId | None = None


class RunInfoOut(BaseModel):
    accession: str
    experiment: str | None = None
    sample: str | None = None
    study: str | None = None
    bioproject: str | None = None
    biosample: str | None = None
    platform: str | None = None
    instrument: str | None = None
    library_strategy: str | None = None
    library_layout: str | None = None
    library_source: str | None = None
    spots: int | None = None
    bases: int | None = None
    bytes: int | None = None
    organism: str | None = None
    title: str | None = None
    sample_attributes: dict = Field(default_factory=dict)
    # Set when this project already holds the run. The checklist greys these
    # out rather than hiding them: "you already have this" is information.
    already_downloaded: bool = False


class HierarchyNodeOut(BaseModel):
    accession: str
    kind: str
    title: str | None = None
    platform: str | None = None
    organism: str | None = None
    child_count: int = 0
    total_bases: int | None = None


class SraResolveResponse(BaseModel):
    accession: str
    kind: str
    title: str | None = None
    organism: str | None = None
    hierarchy: list[HierarchyNodeOut] = Field(default_factory=list)
    runs: list[RunInfoOut] = Field(default_factory=list)
    total_run_count: int = 0
    total_bytes_estimate: int | None = None
    truncated: bool = False
    error: str | None = None


class SraDownloadRequest(BaseModel):
    project_id: PydanticObjectId
    run_accessions: list[str]
    run_qc: bool = True


class SraAccepted(BaseModel):
    run_id: str
    download_job_ids: list[str]
    # Runs whose download was already in flight, so no new job was created.
    # Reported rather than silently dropped: the count would otherwise not
    # match what the user selected.
    skipped: list[str] = Field(default_factory=list)


@router.post("/resolve", response_model=SraResolveResponse)
async def sra_resolve(body: SraResolveRequest) -> SraResolveResponse:
    """Resolve any INSDC accession to the runs beneath it.

    Read-only and starts nothing. Cached in Redis for an hour, because the
    drill-down UI revisits the same accession as the user moves back and forth.

    A resolution that finds nothing is a 200 with `error` set rather than a
    404: "no runs found for this accession" is a result the dialog renders, not
    a failed request.
    """
    resolution = await sra_resolver.resolve_cached(
        body.accession, platform_filter=body.platform_filter
    )

    present: set[str] = set()
    if body.project_id is not None and resolution.runs:
        present = await sra_service.already_downloaded(
            body.project_id, [r.accession for r in resolution.runs]
        )

    return SraResolveResponse(
        accession=resolution.accession,
        kind=resolution.kind,
        title=resolution.title,
        organism=resolution.organism,
        hierarchy=[HierarchyNodeOut(**h.as_dict()) for h in resolution.hierarchy],
        runs=[
            RunInfoOut(
                **run.as_dict(), already_downloaded=run.accession in present
            )
            for run in resolution.runs
        ],
        total_run_count=resolution.total_run_count,
        total_bytes_estimate=resolution.total_bytes_estimate,
        truncated=resolution.truncated,
        error=resolution.error,
    )


@router.post(
    "/download", response_model=SraAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def sra_download(body: SraDownloadRequest) -> SraAccepted:
    """Download selected runs from SRA.

    202 rather than 201: this accepts the work and returns immediately. One
    download job per run, grouped under a single `PipelineRun`; each ingests
    its output and chains QC through the applier.
    """
    run, job_ids, skipped = await sra_service.launch_download(
        project_id=body.project_id,
        run_accessions=body.run_accessions,
        run_qc=body.run_qc,
    )
    return SraAccepted(
        run_id=str(run.id), download_job_ids=job_ids, skipped=skipped
    )
