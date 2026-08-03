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
from typing import Literal

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import OwnerDep
from app.logging import get_logger
from app.metadata import ncbi_assembly, ncbi_assembly_components, ncbi_taxonomy, sra_resolver
from app.metadata.ncbi_taxonomy import AssemblyPage
from app.services import ncbi_assembly_service, sra_service

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


class OrganismSuggestionOut(BaseModel):
    sci_name: str
    tax_id: int
    common_name: str | None = None
    rank: str | None = None
    group_name: str | None = None


class OrganismSuggestResponse(BaseModel):
    suggestions: list[OrganismSuggestionOut] = Field(default_factory=list)


class OrganismAssemblySummary(BaseModel):
    """A row in an organism's assembly list.

    Deliberately lighter than `AssemblyResolveResponse`: `components` requires
    a CLI shellout per accession, which is fine for one resolved assembly but
    not for a page of up to 20. Picking one assembly for its full component
    picker goes back through the existing `/ncbi/resolve` accession path.
    """

    accession: str | None = None
    organism: str | None = None
    tax_id: int | None = None
    strain: str | None = None
    assembly_name: str | None = None
    assembly_level: str | None = None
    submitter: str | None = None
    release_date: str | None = None
    # "reference genome" or "representative genome" -- NCBI's own pick for
    # this organism, shown as a badge. None for every other assembly.
    refseq_category: str | None = None
    total_length: int | None = None
    scaffold_count: int | None = None
    gc_percent: float | None = None
    already_downloaded: bool = False


class OrganismSearchRequest(BaseModel):
    tax_id: int
    # NCBI's `esearch ... [Organism]` field matches on the name, not the
    # numeric tax_id -- `9606[Organism]` matches nothing, `Homo sapiens[Organism]`
    # matches the SRA archive. The assembly search takes the tax_id directly,
    # so both are needed.
    sci_name: str
    project_id: PydanticObjectId | None = None
    assembly_page_token: str | None = None
    sra_offset: int = 0
    page_size: int = 20
    # ILLUMINA | PACBIO_SMRT | OXFORD_NANOPORE, or None for everything. Applies
    # only to sequencing runs -- a genome assembly has no sequencing platform
    # of its own, it is downstream of whatever reads built it.
    platform_filter: str | None = None
    # NCBI's own assembly_level vocabulary: "Complete Genome" / "Chromosome" /
    # "Scaffold" / "Contig". Applies only to the assembly list.
    assembly_level: str | None = None
    # Which table the caller actually wants back. "both" is the initial
    # search -- a quick look at each list side by side, so each is capped to
    # INITIAL_SECTION_LIMIT regardless of `page_size`. Clicking "Next" on
    # either table's own pager switches to that table alone: the user has
    # shown they only care about that list, and re-fetching (and re-rendering)
    # the other one on every page turn would be wasted work and a shifting
    # layout for no reason.
    section: Literal["both", "assemblies", "sra"] = "both"


class OrganismSearchResponse(BaseModel):
    tax_id: int
    sci_name: str | None = None
    assemblies: list[OrganismAssemblySummary] = Field(default_factory=list)
    assemblies_next_page_token: str | None = None
    sra_runs: list[RunInfoOut] = Field(default_factory=list)
    sra_total_count: int = 0
    sra_next_offset: int | None = None
    error: str | None = None


class AssemblyDownloadRequest(BaseModel):
    project_id: PydanticObjectId
    accession: str
    components: list[str] = Field(default_factory=lambda: ["genome"])


class AssemblyAccepted(BaseModel):
    run_id: str
    download_job_ids: list[str]


async def sra_resolve(body: SraResolveRequest, owner: OwnerDep) -> SraResolveResponse:
    """Resolve any INSDC accession to the runs beneath it.

    Read-only and starts nothing. Cached in Redis for an hour, because the
    drill-down UI revisits the same accession as the user moves back and forth.

    A resolution that finds nothing is a 200 with `error` set rather than a
    404: "no runs found for this accession" is a result the dialog renders, not
    a failed request.

    The accession lookup itself is public NCBI data and needs no profile. The
    owner is here for the `already_downloaded` cross-check below, which reads
    the caller's own library to decide which runs to grey out.
    """
    resolution = await sra_resolver.resolve_cached(
        body.accession, platform_filter=body.platform_filter
    )

    present: set[str] = set()
    if body.project_id is not None and resolution.runs:
        present = await sra_service.already_downloaded(
            body.project_id, [r.accession for r in resolution.runs], owner=owner
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


async def sra_download(body: SraDownloadRequest, owner: OwnerDep) -> SraAccepted:
    """Download selected runs from SRA.

    202 rather than 201: this accepts the work and returns immediately. One
    download job per run, grouped under a single `PipelineRun`; each ingests
    its output and chains QC through the applier.
    """
    run, job_ids, skipped = await sra_service.launch_download(
        project_id=body.project_id,
        run_accessions=body.run_accessions,
        owner=owner,
        run_qc=body.run_qc,
    )
    return SraAccepted(
        run_id=str(run.id), download_job_ids=job_ids, skipped=skipped
    )


@router.post("/resolve", response_model=NcbiResolveResponse)
async def ncbi_resolve(body: SraResolveRequest, owner: OwnerDep) -> NcbiResolveResponse:
    """Resolve any NCBI accession -- sequencing data or a published assembly.

    Read-only and starts nothing. A resolution that finds nothing is a 200
    with `error` set rather than a 404: "nothing found for this accession" is
    a result the dialog renders, not a failed request.

    Owner-scoped for the same narrow reason as `sra_resolve`: both branches
    end in an "already downloaded" check against the caller's own library.
    """
    kind = sra_resolver.classify(body.accession) or "unknown"

    if kind == "assembly":
        return NcbiResolveResponse(
            kind=kind,
            assembly=await _resolve_assembly(
                body.accession, body.project_id, owner=owner
            ),
        )

    return NcbiResolveResponse(kind=kind, sra=await sra_resolve(body, owner))


async def _resolve_assembly(
    accession: str, project_id: PydanticObjectId | None, *, owner: str
) -> AssemblyResolveResponse:
    """The assembly branch: one record plus what it offers for download.

    Both lookups are synchronous network calls, so they run in a worker thread
    rather than blocking the event loop -- `component_availability` shells out
    to the CLI, which is the slower of the two.
    """
    accession = accession.strip().upper()

    meta = await asyncio.to_thread(ncbi_assembly.lookup, accession)
    if meta is None:
        return AssemblyResolveResponse(
            accession=accession,
            error=(
                f"No assembly record found for {accession} at NCBI. Check the "
                "accession, including its version suffix."
            ),
        )

    availability = await asyncio.to_thread(
        ncbi_assembly.component_availability, accession
    )
    if availability is None:
        # The CLI could not answer, so fall back to what the API report says.
        # Coarser, but better than offering every component blindly.
        availability = list(
            ncbi_assembly_components.from_report(
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
        present = await ncbi_assembly_service.already_downloaded(
            project_id, accession, owner=owner
        )

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
async def download_assembly(
    body: AssemblyDownloadRequest, owner: OwnerDep
) -> AssemblyAccepted:
    """Download an assembly's selected components.

    202 rather than 201: this accepts the work and returns immediately.
    """
    run, job_ids = await ncbi_assembly_service.launch_download(
        project_id=body.project_id,
        accession=body.accession,
        components=body.components,
        owner=owner,
    )
    return AssemblyAccepted(run_id=str(run.id), download_job_ids=job_ids)


@router.get("/organism-suggest", response_model=OrganismSuggestResponse)
async def organism_suggest(q: str) -> OrganismSuggestResponse:
    """Autocomplete candidates for a partially typed organism name.

    A thin proxy over NCBI's `taxon_suggest`, so all NCBI traffic stays
    server-side and under the same throttle as every other lookup here.
    Public NCBI data, so no owner scoping is needed -- there is no
    already-downloaded cross-check to make against a suggestion list.
    """
    suggestions = await asyncio.to_thread(ncbi_taxonomy.suggest_organisms, q)
    return OrganismSuggestResponse(
        suggestions=[OrganismSuggestionOut(**s.as_dict()) for s in suggestions]
    )


# How many rows each table shows on the initial combined search -- a quick
# side-by-side look, not a full paginated browse. Clicking "Next" on either
# table switches `section` to that table alone, at which point `page_size`
# (not this) governs how many rows come back.
INITIAL_SECTION_LIMIT = 5


@router.post("/organism-search", response_model=OrganismSearchResponse)
async def organism_search(
    body: OrganismSearchRequest, owner: OwnerDep
) -> OrganismSearchResponse:
    """Paginated assemblies and sequencing runs for a resolved organism.

    Two independent, real server-side-paginated lists rather than one merged
    page: assemblies page by NCBI's own `page_token` cursor, SRA runs page by
    `esearch` offset, and neither pagination scheme fits the other's result
    shape.

    `section` decides which of the two lists is actually fetched: "both" is
    the initial search (each capped to `INITIAL_SECTION_LIMIT`), while paging
    either table's own pager narrows to that table alone so the other one
    doesn't refetch and re-render on every page turn.
    """
    want_assemblies = body.section in ("both", "assemblies")
    want_sra = body.section in ("both", "sra")
    page_size = INITIAL_SECTION_LIMIT if body.section == "both" else body.page_size

    assembly_page, (uids, sra_total) = await asyncio.gather(
        asyncio.to_thread(
            ncbi_taxonomy.search_assemblies_by_taxon,
            body.tax_id,
            page_token=body.assembly_page_token,
            page_size=page_size,
            assembly_level=body.assembly_level,
        )
        if want_assemblies
        else _noop(AssemblyPage()),
        asyncio.to_thread(
            sra_resolver.search_runs_by_organism,
            body.sci_name,
            retstart=body.sra_offset,
            retmax=page_size,
            platform_filter=body.platform_filter,
        )
        if want_sra
        else _noop(([], 0)),
    )

    assemblies: list[OrganismAssemblySummary] = []
    for meta in assembly_page.assemblies:
        present = False
        if body.project_id is not None and meta.accession:
            present = await ncbi_assembly_service.already_downloaded(
                body.project_id, meta.accession, owner=owner
            )
        assemblies.append(
            OrganismAssemblySummary(
                accession=meta.accession,
                organism=meta.organism,
                tax_id=meta.tax_id,
                strain=meta.strain,
                assembly_name=meta.assembly_name,
                assembly_level=meta.assembly_level,
                submitter=meta.submitter,
                release_date=meta.release_date,
                refseq_category=meta.refseq_category,
                total_length=meta.total_length,
                scaffold_count=meta.scaffold_count,
                gc_percent=meta.gc_percent,
                already_downloaded=present,
            )
        )

    sra_runs: list[RunInfoOut] = []
    if uids:
        packages = await asyncio.to_thread(sra_resolver.fetch_packages, uids)
        runs = []
        for package in packages:
            try:
                runs.extend(sra_resolver.runs_from_package(package))
            except Exception as e:  # noqa: BLE001 - one bad package must not lose the rest
                log.warning("organism_search_package_failed", error=str(e))

        present_runs: set[str] = set()
        if body.project_id is not None and runs:
            present_runs = await sra_service.already_downloaded(
                body.project_id, [r.accession for r in runs], owner=owner
            )
        sra_runs = [
            RunInfoOut(**r.as_dict(), already_downloaded=r.accession in present_runs)
            for r in runs
        ]

    sra_next_offset = (
        body.sra_offset + len(uids)
        if want_sra and body.sra_offset + len(uids) < sra_total
        else None
    )

    return OrganismSearchResponse(
        tax_id=body.tax_id,
        sci_name=body.sci_name,
        assemblies=assemblies,
        assemblies_next_page_token=assembly_page.next_page_token if want_assemblies else None,
        sra_runs=sra_runs,
        sra_total_count=sra_total if want_sra else 0,
        sra_next_offset=sra_next_offset,
        error=None
        if (assemblies or sra_runs)
        else f"No assemblies or sequencing runs found for tax_id {body.tax_id}.",
    )


async def _noop(value):
    """An already-known value, wrapped so it can sit beside a real task in
    `asyncio.gather` when the caller only wants one of the two sections."""
    return value
