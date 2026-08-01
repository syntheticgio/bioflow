"""UniProt endpoints: resolving what the user typed, and downloading it.

Request and response models live here rather than in `schemas.py`, matching
`ncbi.py` and `pipelines.py`. `schemas.py` holds what several routers share;
nothing else consumes these.

Separate from `ncbi.py` deliberately. Folding UniProt into that router's one
accession box was possible -- the namespaces do not collide -- but its
question, "is this SRA or an assembly?", is coherent because it is about one
provider. Adding "or is it UniProt?" makes one field the door to everything.
"""

import asyncio

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import OwnerDep
from app.errors import ValidationError
from app.logging import get_logger
from app.metadata import uniprot
from app.services import uniprot_service

log = get_logger(__name__)

router = APIRouter(prefix="/uniprot", tags=["uniprot"])


class ResolveRequest(BaseModel):
    query: str
    project_id: PydanticObjectId | None = None


class ProteomeOut(BaseModel):
    id: str
    name: str
    taxon_id: int | None = None
    strain: str | None = None
    protein_count: int | None = None
    is_reference: bool = False
    busco_score: int | None = None
    # The NCBI assembly this proteome's genome came from. Rendered as a link
    # to the other dialog rather than a combined download.
    genome_assembly: str | None = None
    # Both counts, so the reviewed/unreviewed difference is visible before
    # the download rather than after it. Roughly sevenfold for human.
    reviewed_count: int | None = None
    total_count: int | None = None


class ProteinOut(BaseModel):
    accession: str
    entry_id: str | None = None
    name: str | None = None
    organism: str | None = None
    length: int | None = None
    reviewed: bool = False


class ResolveResponse(BaseModel):
    # "proteome" | "proteins" | "empty"
    kind: str
    proteome: ProteomeOut | None = None
    # Other proteomes for the same organism. Populated for both branches: on
    # a reference hit these sit behind a disclosure, and on a species-level
    # taxon with no reference proteome they are the whole answer.
    candidates: list[ProteomeOut] = Field(default_factory=list)
    needs_picker: bool = False
    proteins: list[ProteinOut] = Field(default_factory=list)
    message: str | None = None


class DownloadRequest(BaseModel):
    project_id: PydanticObjectId
    proteome_id: str | None = None
    accessions: list[str] = Field(default_factory=list)
    reviewed_only: bool = True
    organism: str | None = None
    protein_count: int | None = None


class DownloadAccepted(BaseModel):
    run_id: str
    job_ids: list[str]


def _proteome_out(info: uniprot.ProteomeInfo) -> ProteomeOut:
    return ProteomeOut(
        id=info.id,
        name=info.name,
        taxon_id=info.taxon_id,
        strain=info.strain,
        protein_count=info.protein_count,
        is_reference=info.is_reference,
        busco_score=info.busco_score,
        genome_assembly=info.genome_assembly,
    )


def _protein_out(hit: uniprot.ProteinHit) -> ProteinOut:
    return ProteinOut(
        accession=hit.accession,
        entry_id=hit.entry_id,
        name=hit.name,
        organism=hit.organism,
        length=hit.length,
        reviewed=hit.reviewed,
    )


async def _with_counts(info: uniprot.ProteomeInfo) -> ProteomeOut:
    """A proteome plus both protein counts.

    Two extra requests, run concurrently. Worth it: the reviewed and
    unreviewed sets differ roughly sevenfold for human, and a user who cannot
    see that before clicking discovers it as a 147,506-entry file.
    """
    out = _proteome_out(info)
    reviewed_query = uniprot.download_query(
        proteome_id=info.id, accessions=[], reviewed_only=True
    )
    total_query = uniprot.download_query(
        proteome_id=info.id, accessions=[], reviewed_only=False
    )
    reviewed, total = await asyncio.gather(
        asyncio.to_thread(uniprot.count_results, reviewed_query),
        asyncio.to_thread(uniprot.count_results, total_query),
    )
    out.reviewed_count = reviewed
    out.total_count = total
    return out


@router.post("/resolve", response_model=ResolveResponse)
async def resolve(body: ResolveRequest) -> ResolveResponse:
    """What does this input name, and what can be downloaded for it?

    Every UniProt call runs in a worker thread: they are blocking urllib
    requests, and one on the event loop stalls every other request.
    """
    raw = (body.query or "").strip()
    if not raw:
        raise ValidationError("Enter a proteome, an accession, or a protein name.")

    kind = uniprot.classify(raw)

    if kind is uniprot.InputKind.PROTEOME:
        info = await asyncio.to_thread(uniprot.resolve_proteome, raw.upper())
        if info is None:
            return ResolveResponse(
                kind="empty", message=f"UniProt has no proteome {raw.upper()}."
            )
        return ResolveResponse(kind="proteome", proteome=await _with_counts(info))

    if kind is uniprot.InputKind.ACCESSIONS:
        accessions = uniprot.parse_accessions(raw)
        # The same string `download_query` builds, so the resolve preview and
        # the download it leads to cannot drift apart. `reviewed_only` is
        # irrelevant here -- `download_query` ignores it for picked accessions.
        query = uniprot.download_query(
            proteome_id=None, accessions=accessions, reviewed_only=True
        )
        hits = await asyncio.to_thread(uniprot.search_proteins, query)
        if not hits:
            return ResolveResponse(
                kind="empty", message="UniProt returned nothing for those accessions."
            )
        return ResolveResponse(
            kind="proteins", proteins=[_protein_out(h) for h in hits]
        )

    if kind is uniprot.InputKind.TAXON:
        try:
            taxon_id = int(raw)
        except ValueError:
            # Python caps integer parsing at 4,300 digits, and `classify`
            # routes any all-digit string here -- so a long accidental paste
            # reaches this line. Everything else malformed in this module
            # degrades to "found nothing"; a crash would be the odd one out.
            return ResolveResponse(
                kind="empty", message=f"{raw[:20]}… is not a taxon identifier."
            )
        resolution = await asyncio.to_thread(uniprot.resolve_taxon, taxon_id)
        return await _taxon_response(resolution, raw)

    # TEXT: an organism name and a protein name are indistinguishable by
    # shape, so ask. The proteome search runs first and the protein search is
    # the fallback, which degrades toward the more general answer.
    resolution = await asyncio.to_thread(uniprot.resolve_organism_name, raw)
    if resolution.proteome is not None or resolution.candidates:
        return await _taxon_response(resolution, raw)

    hits = await asyncio.to_thread(uniprot.search_proteins, raw)
    if not hits:
        return ResolveResponse(
            kind="empty", message=f"UniProt returned nothing for {raw!r}."
        )
    return ResolveResponse(kind="proteins", proteins=[_protein_out(h) for h in hits])


async def _taxon_response(
    resolution: uniprot.TaxonResolution, raw: str
) -> ResolveResponse:
    """A resolved organism, as a card.

    There is no picker branch. `resolve_taxon` and `resolve_organism_name`
    return a reference proteome or nothing, because a non-reference proteome
    cannot be downloaded at all -- its entries are in UniParc rather than
    UniProtKB's searchable index, so the download writes an empty file.
    Offering one would be worse than offering nothing.
    """
    if resolution.proteome is not None:
        return ResolveResponse(
            kind="proteome",
            proteome=await _with_counts(resolution.proteome),
            needs_picker=False,
        )

    return ResolveResponse(
        kind="empty",
        message=(
            f"UniProt has no reference proteome for {raw!r}. Only reference "
            "proteomes can be downloaded."
        ),
    )


@router.post(
    "/download",
    response_model=DownloadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def download(body: DownloadRequest, owner: OwnerDep) -> DownloadAccepted:
    """Queue a proteome or a set of proteins for download."""
    run, job_ids = await uniprot_service.launch_download(
        project_id=body.project_id,
        proteome_id=body.proteome_id,
        accessions=body.accessions,
        reviewed_only=body.reviewed_only,
        owner=owner,
        organism=body.organism,
        protein_count=body.protein_count,
    )
    return DownloadAccepted(run_id=str(run.id), job_ids=job_ids)
