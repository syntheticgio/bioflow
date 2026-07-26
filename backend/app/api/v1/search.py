"""Search, facets, metadata schemas, and bulk editing."""

from beanie import PydanticObjectId
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.v1.schemas import ObjectOut
from app.errors import ValidationError
from app.metadata import schemas as meta_schemas
from app.models import FormatKind
from app.services import search_service

router = APIRouter(tags=["search"])


class SearchResults(BaseModel):
    objects: list[ObjectOut]
    total: int
    has_more: bool
    next_cursor: str | None = None


@router.get("/search/objects", response_model=SearchResults)
async def search_objects(
    q: str | None = Query(None, description="Substring match on filename"),
    project_id: str | None = None,
    kind: list[str] = Query(default_factory=list),
    status: list[str] = Query(default_factory=list),
    tag: list[str] = Query(default_factory=list),
    meta: list[str] = Query(
        default_factory=list,
        description="Metadata filters: key=value, key>=30, key!=x, key=* (exists)",
    ),
    size_min: int | None = None,
    size_max: int | None = None,
    sort: str = "-created_at",
    limit: int = Query(100, le=search_service.MAX_LIMIT),
    cursor: str | None = None,
) -> SearchResults:
    """Find objects across all projects, or within one.

    Metadata filters use a compact `key=value` syntax so a search is fully
    described by its URL -- shareable, and it survives a reload.
    """
    query = search_service.SearchQuery(
        text=q,
        project_id=PydanticObjectId(project_id) if project_id else None,
        kinds=kind,
        statuses=status,
        tags=tag,
        metadata=search_service.parse_metadata_filters(meta),
        size_min=size_min,
        size_max=size_max,
        sort=sort,
        limit=limit,
        cursor=cursor,
    )
    result = await search_service.search_objects(query)
    return SearchResults(
        objects=[ObjectOut.of(o) for o in result["objects"]],
        total=result["total"],
        has_more=result["has_more"],
        next_cursor=result["next_cursor"],
    )


@router.get("/search/facets")
async def search_facets(project_id: str | None = None) -> dict:
    """Distinct values and counts, for building filter controls."""
    return await search_service.facets(
        PydanticObjectId(project_id) if project_id else None
    )


@router.get("/search/metadata-values/{key}")
async def metadata_values(key: str, project_id: str | None = None) -> dict:
    """Distinct values for one metadata key, for a value picker."""
    values = await search_service.metadata_values(
        key, PydanticObjectId(project_id) if project_id else None
    )
    return {"key": key, "values": values}


@router.get("/metadata/schemas")
async def list_schemas() -> dict:
    """Every format's field definitions, keyed by format kind."""
    return {
        "schemas": {
            kind.value: meta_schemas.schema_for_api(kind)
            for kind in FormatKind
            if kind is not FormatKind.UNKNOWN
        },
        "common": meta_schemas.schema_for_api(None),
    }


@router.get("/metadata/schemas/{kind}")
async def get_schema(kind: str) -> dict:
    """Suggested fields for one format.

    These are suggestions, not restrictions: arbitrary keys remain allowed and
    are stored as-is.
    """
    try:
        format_kind = FormatKind(kind)
    except ValueError as e:
        raise ValidationError(
            f"Unknown format kind: {kind!r}",
            details={"known": [k.value for k in FormatKind]},
        ) from e
    return meta_schemas.schema_for_api(format_kind)


class BulkMetadata(BaseModel):
    object_ids: list[str] = Field(min_length=1, max_length=1000)
    set: dict = Field(default_factory=dict)
    unset: list[str] = Field(default_factory=list)


@router.post("/objects/bulk-metadata")
async def bulk_metadata(body: BulkMetadata) -> dict:
    """Apply metadata to many objects at once.

    Values are merged into existing metadata rather than replacing it, so
    assigning one field cannot silently erase the others.
    """
    if not body.set and not body.unset:
        raise ValidationError("Provide at least one of 'set' or 'unset'")

    ids = [PydanticObjectId(i) for i in body.object_ids]
    return await search_service.bulk_update_metadata(
        ids, set_values=body.set or None, unset_keys=body.unset or None
    )


class BulkTags(BaseModel):
    object_ids: list[str] = Field(min_length=1, max_length=1000)
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


@router.post("/objects/bulk-tags")
async def bulk_tags(body: BulkTags) -> dict:
    if not body.add and not body.remove:
        raise ValidationError("Provide at least one of 'add' or 'remove'")

    ids = [PydanticObjectId(i) for i in body.object_ids]
    return await search_service.bulk_update_tags(
        ids, add=body.add or None, remove=body.remove or None
    )
