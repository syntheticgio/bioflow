"""Object endpoints."""

from dataclasses import asdict
from pathlib import Path

from beanie import PydanticObjectId
from fastapi import APIRouter, Query, status
from fastapi.responses import FileResponse

from app.api.deps import LinkableOwnerDep, OwnerDep
from app.api.v1.schemas import (
    BlobOut,
    ComputationRecord,
    ExpectedGc,
    MoleculeTypeInferenceOut,
    ObjectComputationsOut,
    ObjectDetail,
    ObjectOut,
    ObjectUpdate,
    PairRequest,
    ProvenanceGapOut,
    ProvenanceNarrativeOut,
    ProvenanceProseOut,
    ProvenanceStepOut,
)
from app.errors import NotFoundError, ValidationError
from app.logging import get_logger
from app.metadata import infer_molecule
from app.models import BlobStorage, FormatKind, JobClass, JobRunTiming
from app.models.ai import TaskSlot
from app.services import (
    ai,
    expected_gc,
    object_service,
    pipeline_service,
    provenance_lineage,
    provenance_prompt,
    provenance_report,
    provenance_walker,
    timing_service,
)
from app.services.ai import Completion
from app.storage.paths import blob_path

router = APIRouter(prefix="/objects", tags=["objects"])
log = get_logger(__name__)


@router.get("/{object_id}", response_model=ObjectDetail)
async def get_object(object_id: PydanticObjectId, owner: OwnerDep) -> ObjectDetail:
    obj, blob = await object_service.object_with_blob(object_id, owner=owner)
    references = await expected_gc.references_for_project(
        project_id=obj.project_id, owner=owner
    )
    resolved = expected_gc.resolve(
        references=references,
        organism=obj.metadata.get("organism") if obj.metadata else None,
    )
    return ObjectDetail(
        **ObjectOut.of(obj).model_dump(),
        blob=BlobOut.of(blob) if blob else None,
        summary_fingerprint=pipeline_service.summary_fingerprint(obj),
        expected_gc=ExpectedGc(**asdict(resolved)) if resolved else None,
    )


@router.patch("/{object_id}", response_model=ObjectOut)
async def update_object(
    object_id: PydanticObjectId, body: ObjectUpdate, owner: OwnerDep
) -> ObjectOut:
    obj = await object_service.update_object(
        object_id, body.model_dump(exclude_unset=True), owner=owner
    )
    return ObjectOut.of(obj)


@router.get("/{object_id}/computations", response_model=ObjectComputationsOut)
async def object_computations(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    limit: int = Query(100, le=500),
) -> ObjectComputationsOut:
    """How this file was made, and every run that has used it since.

    `JobRunTiming` has no owner field, so `get_object` is what authorizes
    this request -- it must run before any timing row is read, not
    concurrently with it, or a wrong-owner request would read another
    profile's records before the check that says it may not.
    """
    obj = await object_service.get_object(object_id, owner=owner)

    rows = await timing_service.records_for_object(str(object_id), limit=limit + 1)
    has_more = len(rows) > limit
    records = [ComputationRecord.of(r) for r in rows[:limit]]

    produced_by = None
    if obj.produced_by_job:
        row = await JobRunTiming.find_one(
            JobRunTiming.job_id == str(obj.produced_by_job)
        )
        if row is not None:
            produced_by = ComputationRecord.of(row)

    return ObjectComputationsOut(
        produced_by=produced_by,
        produced_by_job=str(obj.produced_by_job) if obj.produced_by_job else None,
        records=records,
        has_more=has_more,
    )


@router.get(
    "/{object_id}/provenance-narrative",
    response_model=ProvenanceNarrativeOut,
)
async def provenance_narrative(
    object_id: PydanticObjectId,
    owner: OwnerDep,
) -> ProvenanceNarrativeOut:
    """A methods report for one object, assembled from recorded facts only.

    No model involvement: this must work on an install with no AI provider
    configured, because it is the artifact users cite.
    """
    chain = await provenance_walker.walk(object_id, owner=owner)

    def _out(entry) -> ProvenanceStepOut:
        node = entry.node
        step = entry.step
        used_by = chain.nodes.get(node.used_by) if node.used_by else None
        return ProvenanceStepOut(
            object_id=str(node.object_id),
            name=provenance_lineage.format_names(entry.names),
            names=list(entry.names),
            kind=node.kind,
            verb=step.verb if step else None,
            tool=step.tool if step else None,
            tool_version=step.tool_version if step else None,
            job_type=step.job_type if step else None,
            ran_at=step.ran_at if step else None,
            outcome=step.outcome if step else None,
            params=dict(step.params) if step else {},
            gaps=[
                provenance_report.inline_gap_text(g)
                for g in entry.gaps
                if provenance_report.is_step_level(g)
            ],
            used_by=used_by.name if used_by is not None else None,
        )

    lineage = [_out(e) for e in provenance_lineage.lineage_for(chain)]

    # The rail names every gap once, including the chain-level ones a step row
    # has nowhere to show. `job_type` comes from the node the gap is attached
    # to, so a download's missing parameters read as "Download parameters"
    # rather than the generic phrase.
    job_types = {
        node.object_id: node.produced_by.job_type
        for node in chain.nodes.values()
        if node.produced_by is not None
    }

    return ProvenanceNarrativeOut(
        markdown=provenance_report.render_markdown(chain),
        gap_count=chain.gap_count,
        lineage=lineage,
        steps=[s for s in lineage if s.kind == "spine"],
        materials=[s for s in lineage if s.kind == "supporting"],
        gaps=[
            ProvenanceGapOut(
                label=provenance_report.gap_label(g, job_types.get(g.object_id)),
                object_id=str(g.object_id) if g.object_id else None,
            )
            for g in chain.gaps
        ],
        has_branches=bool(chain.branches),  # each branch: (dest_id, parent_id, ...)
        redirected_from_name=(
            chain.redirected_from[1] if chain.redirected_from else None
        ),
    )


@router.post(
    "/{object_id}/provenance-narrative/prose",
    response_model=ProvenanceProseOut,
)
async def provenance_narrative_prose(
    object_id: PydanticObjectId,
    owner: OwnerDep,
) -> ProvenanceProseOut:
    """The same facts, rendered as prose by a model.

    Every failure mode returns 200 with `prose=None` and a reason: no
    provider configured, the call failed, or the output was rejected for
    introducing an unsupported fact. None of those is an error the caller
    should retry, and the structured report is unaffected either way.
    """
    chain = await provenance_walker.walk(object_id, owner=owner)

    provider = await ai.resolve(TaskSlot.PROVENANCE_NARRATIVE)
    if provider is None:
        return ProvenanceProseOut(
            prose=None,
            unavailable_reason="No AI provider is configured for methods narratives.",
        )

    system, user = provenance_prompt.build_prompt(chain)
    result = await ai.complete(provider, system=system, user=user)

    # `complete()` never raises and never returns None -- it returns
    # Completion | ToolCall | Failure. Checking for None here would treat
    # every failure as a success.
    if not isinstance(result, Completion):
        return ProvenanceProseOut(
            prose=None,
            unavailable_reason="The model call did not succeed.",
        )

    rejection = provenance_prompt.verify_containment(result.text, chain)
    if rejection is not None:
        log.warning(
            "provenance_prose_rejected",
            object_id=str(object_id),
            reason=rejection,
        )
        return ProvenanceProseOut(
            prose=None,
            unavailable_reason=(
                f"The generated paragraph was rejected because it introduced a "
                f"fact this record does not support ({rejection}). "
                f"The structured report above is unaffected."
            ),
        )

    return ProvenanceProseOut(prose=result.text, unavailable_reason=None)


@router.post("/{object_id}/pair", response_model=ObjectOut)
async def pair_object(
    object_id: PydanticObjectId, body: PairRequest, owner: OwnerDep
) -> ObjectOut:
    """Mark two reads files as paired-end mates.

    Its own endpoint rather than a field on PATCH because it writes both
    documents and validates across them -- a merge endpoint could leave one
    side pointing at a file that does not point back.
    """
    obj = await object_service.set_pair(
        object_id, body.mate_object_id, body.read_number, owner=owner
    )
    return ObjectOut.of(obj)


@router.delete("/{object_id}/pair", response_model=ObjectOut)
async def unpair_object(object_id: PydanticObjectId, owner: OwnerDep) -> ObjectOut:
    """Undo a pairing from either side. Idempotent."""
    obj = await object_service.clear_pair(object_id, owner=owner)
    return ObjectOut.of(obj)


@router.get("/{object_id}/download")
async def download_object(
    object_id: PydanticObjectId, owner: LinkableOwnerDep
) -> FileResponse:
    """Serve the object's raw bytes as an attachment.

    The file is returned exactly as stored -- still gzipped if it was ingested
    gzipped -- because the point of this route is to get the original back out,
    not a re-encoded copy. `name` is what the user called the file, so it is
    what the download is named; the digest is an implementation detail of where
    we put it.

    Takes `LinkableOwnerDep`: the frontend's download links are plain
    `<a href download>` elements, not `fetch` calls, so `X-BioFlow-Profile`
    is never attached -- see `get_current_owner_linkable`.

    Managed and external blobs both resolve here. Neither path is
    caller-controlled: a managed path is built from the validated digest, and
    an external path was checked against the register allowlist at ingest, so
    there is no user-supplied path segment to contain.
    """
    obj, blob = await object_service.object_with_blob(object_id, owner=owner)
    if blob is None or not obj.blob_sha256:
        raise NotFoundError("Object has no stored content to download yet")

    if blob.storage is BlobStorage.EXTERNAL:
        if not blob.external_path:
            raise NotFoundError("External blob has no recorded path")
        target = Path(blob.external_path)
    else:
        target = blob_path(obj.blob_sha256)

    # An external drive can be unmounted, and GC can win a race against a stale
    # page, so presence is checked rather than assumed. Reporting this as a 404
    # beats letting FileResponse raise once the response has already begun.
    if not target.is_file():
        raise NotFoundError(f"Stored content is not available: {obj.name}")

    return FileResponse(
        target,
        # Deliberately opaque: these are FASTQ/BAM/VCF payloads to hand to the
        # user's own tools, and guessing a renderable type for a file whose
        # bytes came from outside is how a download becomes a page.
        media_type="application/octet-stream",
        filename=obj.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/{object_id}/reingest", status_code=status.HTTP_202_ACCEPTED)
async def reingest_object(object_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """Re-run format detection and header parsing.

    Useful after a parser improves, or when a file was ingested while its
    format was not yet recognized. Runs at user-interactive priority because
    someone clicked a button and is waiting.
    """
    obj, blob = await object_service.object_with_blob(object_id, owner=owner)
    if blob is None or not obj.blob_sha256:
        raise ValidationError("Object has no stored content to re-ingest yet")

    job_id = await object_service.enqueue_ingest(
        obj,
        # The request's owner rather than `obj.owner`: the fetch above is now
        # scoped, so they are equal, and naming the request's owner keeps the
        # job's partition traceable to the caller rather than to a field that
        # would silently carry a stale value if the fetch ever widened again.
        owner=owner,
        digest=obj.blob_sha256 if blob.storage is BlobStorage.MANAGED else None,
        path=Path(blob.external_path) if blob.external_path else None,
        job_class=JobClass.USER_INTERACTIVE,
    )
    return {"object_id": str(object_id), "job_id": job_id}


@router.post("/{object_id}/infer-molecule-type", response_model=MoleculeTypeInferenceOut)
async def infer_molecule_type_endpoint(
    object_id: PydanticObjectId, owner: OwnerDep
) -> MoleculeTypeInferenceOut:
    """Sample a FASTQ's own bases to suggest DNA or RNA.

    User-triggered only -- never runs automatically. Returns a suggestion for
    the caller to apply to an in-progress metadata edit; does not write
    anything itself. Runs synchronously: sampling ~2000 reads from the start
    of the file is bounded regardless of the file's total size, unlike
    reingest's full pipeline dispatch.
    """
    obj, blob = await object_service.object_with_blob(object_id, owner=owner)
    if obj.format.kind is not FormatKind.FASTQ:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not FASTQ reads to sample",
            details={"object_id": str(object_id), "kind": obj.format.kind.value},
        )
    if blob is None or not obj.blob_sha256:
        raise NotFoundError("Object has no stored content to sample yet")

    if blob.storage is BlobStorage.EXTERNAL:
        if not blob.external_path:
            raise NotFoundError("External blob has no recorded path")
        target = Path(blob.external_path)
    else:
        target = blob_path(obj.blob_sha256)

    if not target.is_file():
        raise NotFoundError(f"Stored content is not available: {obj.name}")

    result = infer_molecule.infer_molecule_type(target)
    return MoleculeTypeInferenceOut(**result)


@router.delete("/{object_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_object(object_id: PydanticObjectId, owner: OwnerDep) -> None:
    """Detach the object. The blob's bytes are unlinked later by GC, once its
    refcount has been zero past the grace window."""
    await object_service.delete_object(object_id, owner=owner)
