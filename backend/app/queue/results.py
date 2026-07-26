"""Applying job results to the database.

Thread-mode handlers run off the event loop and so cannot use Beanie, which is
async. Rather than giving handlers a second synchronous database client -- two
connection pools with different transaction semantics -- they return plain
dicts, and the writes happen here on the loop.
"""

from datetime import UTC, datetime

from beanie import PydanticObjectId

from app.logging import get_logger
from app.models import DataObject, FormatInfo, ObjectStatus

log = get_logger(__name__)


async def apply(job_type: str, result: dict) -> None:
    handler = _APPLIERS.get(job_type)
    if handler is not None:
        await handler(result)


async def _apply_ingest_headers(result: dict) -> None:
    object_id = result.get("object_id")
    if not object_id:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        return

    fmt = result.get("format") or {}
    facts = result.get("facts") or {}

    update: dict = {
        DataObject.status: ObjectStatus.READY,
        DataObject.updated_at: datetime.now(UTC),
    }
    if fmt:
        update[DataObject.format] = FormatInfo(
            kind=fmt.get("kind", "unknown"),
            compression=fmt.get("compression", "none"),
            confidence=fmt.get("confidence", "none"),
            extension_says=fmt.get("extension_says"),
            magic_says=fmt.get("magic_says"),
            detected_at=datetime.now(UTC),
        )
    if facts:
        # Merged, not replaced: a re-ingest should not discard facts an earlier
        # pass established (or that a future parser added).
        update[DataObject.facts] = {**obj.facts, **facts}

    enrichment = result.get("enrichment") or {}
    if enrichment.get("values"):
        # enrich_from_sra already excluded anything the user had set, so this
        # merge cannot clobber a manual edit.
        update[DataObject.metadata] = {**obj.metadata, **enrichment["values"]}

    # Provenance lives in facts rather than metadata: it describes where the
    # values came from, and should not itself become searchable metadata.
    if enrichment.get("accession"):
        provenance = {
            "sra_accession": enrichment["accession"],
            "sra_source": enrichment.get("source"),
            "sra_fields_applied": sorted(enrichment.get("values", {})),
        }
        if enrichment.get("conflicts"):
            provenance["sra_conflicts"] = enrichment["conflicts"]
        if enrichment.get("error"):
            provenance["sra_error"] = enrichment["error"]
        merged_facts = update.get(DataObject.facts, obj.facts)
        update[DataObject.facts] = {**merged_facts, **provenance}
    elif enrichment.get("error"):
        merged_facts = update.get(DataObject.facts, obj.facts)
        update[DataObject.facts] = {**merged_facts, "sra_error": enrichment["error"]}

    await obj.set(update)
    log.info(
        "ingest_applied",
        object_id=object_id,
        kind=fmt.get("kind"),
        sra=enrichment.get("accession"),
        sra_fields=len(enrichment.get("values", {})),
    )


_APPLIERS = {
    "ingest_headers": _apply_ingest_headers,
}
