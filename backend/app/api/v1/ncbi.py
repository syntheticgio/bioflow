"""NCBI endpoints: resolving any accession, and downloading what it names.

Request and response models live here rather than in `schemas.py`, matching
`pipelines.py`. `schemas.py` holds what several routers share; nothing else
consumes these.

This absorbed `sra.py`'s original content when assemblies joined it: one
accession box now dispatches to either the sequencing-data resolver or the
assembly resolver, and `/sra/*` stays mounted separately as a thin alias so
nothing already pointed at those paths breaks.
"""

import asyncio

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.logging import get_logger
from app.metadata import assembly, assembly_components, sra_resolver
from app.services import assembly_service, sra_service

log = get_logger(__name__)

router = APIRouter(prefix="/ncbi", tags=["ncbi"])


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


class ComponentOut(BaseModel):
    key: str
    label: str
    role: str
    available: bool
    size_bytes: int | None = None
    reason: str | None = None


class AssemblyResolveResponse(BaseModel):
    accession: str
    organism: str | None = None
    tax_id: int | None = None
    strain: str | None = None
    assembly_name: str | None = None
    assembly_level: str | None = None
    submitter: str | None = None
    release_date: str | None = None
    bioproject: str | None = None
    paired_accession: str | None = None
    total_length: int | None = None
    scaffold_count: int | None = None
    contig_count: int | None = None
    gc_percent: float | None = None
    scaffold_n50: int | None = None
    components: list[ComponentOut] = Field(default_factory=list)
    already_downloaded: bool = False
    error: str | None = None


class NcbiResolveResponse(BaseModel):
    """One accession, two possible answers.

    Two nullable branches with an explicit `kind` rather than one merged
    model: merging would make most fields nullable and the frontend would
    branch on the shape anyway. This way the branch is named.
    """

    kind: str
    sra: SraResolveResponse | None = None
    assembly: AssemblyResolveResponse | None = None


class AssemblyDownloadRequest(BaseModel):
    project_id: PydanticObjectId
    accession: str
    components: list[str] = Field(default_factory=lambda: ["genome"])


class AssemblyAccepted(BaseModel):
    run_id: str
    download_job_ids: list[str]


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


@router.post("/resolve", response_model=NcbiResolveResponse)
async def ncbi_resolve(body: SraResolveRequest) -> NcbiResolveResponse:
    """Resolve any NCBI accession -- sequencing data or a published assembly.

    Read-only and starts nothing. A resolution that finds nothing is a 200
    with `error` set rather than a 404: "nothing found for this accession" is
    a result the dialog renders, not a failed request.
    """
    kind = sra_resolver.classify(body.accession) or "unknown"

    if kind == "assembly":
        return NcbiResolveResponse(
            kind=kind,
            assembly=await _resolve_assembly(body.accession, body.project_id),
        )

    return NcbiResolveResponse(kind=kind, sra=await sra_resolve(body))


async def _resolve_assembly(
    accession: str, project_id: PydanticObjectId | None
) -> AssemblyResolveResponse:
    """The assembly branch: one record plus what it offers for download.

    Both lookups are synchronous network calls, so they run in a worker thread
    rather than blocking the event loop -- `component_availability` shells out
    to the CLI, which is the slower of the two.
    """
    accession = accession.strip().upper()

    meta = await asyncio.to_thread(assembly.lookup, accession)
    if meta is None:
        return AssemblyResolveResponse(
            accession=accession,
            error=(
                f"No assembly record found for {accession} at NCBI. Check the "
                "accession, including its version suffix."
            ),
        )

    availability = await asyncio.to_thread(
        assembly.component_availability, accession
    )
    if availability is None:
        # The CLI could not answer, so fall back to what the API report says.
        # Coarser, but better than offering every component blindly.
        availability = list(
            assembly_components.from_report(
                {
                    "annotation_info": {"name": meta.assembly_name}
                    if meta.assembly_name
                    else None,
                    "paired_accession": meta.paired_accession,
                }
            ).values()
        )

    present = False
    if project_id is not None:
        present = await assembly_service.already_downloaded(project_id, accession)

    return AssemblyResolveResponse(
        accession=meta.accession or accession,
        organism=meta.organism,
        tax_id=meta.tax_id,
        strain=meta.strain,
        assembly_name=meta.assembly_name,
        assembly_level=meta.assembly_level,
        submitter=meta.submitter,
        release_date=meta.release_date,
        bioproject=meta.bioproject,
        paired_accession=meta.paired_accession,
        total_length=meta.total_length,
        scaffold_count=meta.scaffold_count,
        contig_count=meta.contig_count,
        gc_percent=meta.gc_percent,
        scaffold_n50=meta.scaffold_n50,
        components=[ComponentOut(**c.as_dict()) for c in availability],
        already_downloaded=present,
    )


@router.post(
    "/download-assembly",
    response_model=AssemblyAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def download_assembly(body: AssemblyDownloadRequest) -> AssemblyAccepted:
    """Download an assembly's selected components.

    202 rather than 201: this accepts the work and returns immediately.
    """
    run, job_ids = await assembly_service.launch_download(
        project_id=body.project_id,
        accession=body.accession,
        components=body.components,
    )
    return AssemblyAccepted(run_id=str(run.id), download_job_ids=job_ids)
