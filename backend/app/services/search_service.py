"""Object search, filtering, and facet aggregation.

Metadata keys are user-defined, so queries against them cannot rely on a
purpose-built compound index. The wildcard index on `metadata.$**` covers the
equality case, which is what "find every file from patient P-041" needs.

Free-text search is a case-insensitive regex on `name` rather than a Mongo text
index: filenames are the thing people actually search, they are short, and
partial matching (`SampleA_R1` from `sample`) matters more here than stemming.
At single-user scale that is a good trade; a text index becomes worthwhile only
if this ever holds millions of objects.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from beanie import PydanticObjectId

from app.db.client import get_db
from app.logging import get_logger
from app.models import DataObject, FormatKind, ObjectStatus

log = get_logger(__name__)

MAX_LIMIT = 500
MAX_FACET_VALUES = 40


@dataclass
class SearchQuery:
    text: str | None = None
    project_id: PydanticObjectId | None = None
    kinds: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    # {"sample_id": "P-041"} or {"mean_coverage": {"gte": 30}}
    metadata: dict[str, Any] = field(default_factory=dict)
    size_min: int | None = None
    size_max: int | None = None
    sort: str = "-created_at"
    limit: int = 100
    cursor: str | None = None


def build_filter(q: SearchQuery) -> dict:
    """Translate a SearchQuery into a MongoDB filter document."""
    f: dict[str, Any] = {}

    if q.text:
        # Escaped: a user typing "sample(1)" should search for that literal,
        # not hand us a broken -- or expensive -- regex.
        f["name"] = {"$regex": re.escape(q.text.strip()), "$options": "i"}

    if q.project_id is not None:
        f["project_id"] = q.project_id
    if q.kinds:
        f["format.kind"] = {"$in": q.kinds}
    if q.statuses:
        f["status"] = {"$in": q.statuses}
    if q.tags:
        # All listed tags must be present: filters narrow, they do not widen.
        f["tags"] = {"$all": q.tags}

    size: dict[str, Any] = {}
    if q.size_min is not None:
        size["$gte"] = q.size_min
    if q.size_max is not None:
        size["$lte"] = q.size_max
    if size:
        f["size"] = size

    for key, condition in q.metadata.items():
        field_path = f"metadata.{key}"
        if isinstance(condition, dict):
            ops = {}
            for op, value in condition.items():
                mongo_op = _RANGE_OPS.get(op)
                if mongo_op:
                    ops[mongo_op] = value
                elif op == "exists":
                    ops["$exists"] = bool(value)
                elif op == "in":
                    ops["$in"] = value
                elif op == "contains":
                    ops["$regex"] = re.escape(str(value))
                    ops["$options"] = "i"
            if ops:
                f[field_path] = ops
        else:
            f[field_path] = condition

    return f


_RANGE_OPS = {"gte": "$gte", "gt": "$gt", "lte": "$lte", "lt": "$lt", "ne": "$ne"}


async def search_objects(q: SearchQuery) -> dict:
    """Run a search. Returns results plus a cursor for the next page."""
    limit = max(1, min(q.limit, MAX_LIMIT))
    filt = build_filter(q)

    # Keyset pagination: an _id boundary rather than skip(), which degrades
    # linearly as the offset grows.
    if q.cursor:
        try:
            boundary = PydanticObjectId(q.cursor)
            direction = "$lt" if q.sort.startswith("-") else "$gt"
            filt["_id"] = {direction: boundary}
        except Exception:  # noqa: BLE001 - a bad cursor should not 500
            log.warning("invalid_cursor", cursor=q.cursor)

    sort_field = q.sort.lstrip("-")
    descending = q.sort.startswith("-")
    sort_spec = [(sort_field, -1 if descending else 1), ("_id", -1 if descending else 1)]

    docs = (
        await DataObject.find(filt)
        .sort(*[f"{'-' if d < 0 else '+'}{fld}" for fld, d in sort_spec])
        .limit(limit + 1)
        .to_list()
    )

    has_more = len(docs) > limit
    docs = docs[:limit]
    return {
        "objects": docs,
        "has_more": has_more,
        "next_cursor": str(docs[-1].id) if has_more and docs else None,
        "total": await DataObject.find(build_filter(q)).count(),
    }


async def facets(project_id: PydanticObjectId | None = None) -> dict:
    """Distinct values and counts for building the filter UI.

    Everything is aggregated server-side; pulling documents back to count them
    in Python would scale with the library, on an endpoint the UI polls.
    """
    db = get_db()
    match: dict = {"project_id": project_id} if project_id else {}

    async def group_by(field_name: str, limit: int = MAX_FACET_VALUES) -> list[dict]:
        pipeline: list[dict] = []
        if match:
            pipeline.append({"$match": match})
        pipeline += [
            {"$group": {"_id": f"${field_name}", "count": {"$sum": 1}}},
            {"$match": {"_id": {"$ne": None}}},
            {"$sort": {"count": -1}},
            {"$limit": limit},
        ]
        return [
            {"value": r["_id"], "count": r["count"]}
            async for r in db.objects.aggregate(pipeline)
        ]

    async def unwound(field_name: str, limit: int = MAX_FACET_VALUES) -> list[dict]:
        pipeline: list[dict] = []
        if match:
            pipeline.append({"$match": match})
        pipeline += [
            {"$unwind": f"${field_name}"},
            {"$group": {"_id": f"${field_name}", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": limit},
        ]
        return [
            {"value": r["_id"], "count": r["count"]}
            async for r in db.objects.aggregate(pipeline)
        ]

    # Which metadata keys exist at all, and how often -- this is what makes an
    # open-ended schema browsable instead of guesswork.
    key_pipeline: list[dict] = []
    if match:
        key_pipeline.append({"$match": match})
    key_pipeline += [
        {"$project": {"kv": {"$objectToArray": {"$ifNull": ["$metadata", {}]}}}},
        {"$unwind": "$kv"},
        {"$group": {"_id": "$kv.k", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": MAX_FACET_VALUES},
    ]
    metadata_keys = [
        {"key": r["_id"], "count": r["count"]}
        async for r in db.objects.aggregate(key_pipeline)
    ]

    return {
        "formats": await group_by("format.kind"),
        "statuses": await group_by("status"),
        "tags": await unwound("tags"),
        "metadata_keys": metadata_keys,
    }


async def metadata_values(key: str, project_id: PydanticObjectId | None = None) -> list[dict]:
    """Distinct values for one metadata key, for a value picker."""
    db = get_db()
    pipeline: list[dict] = []
    if project_id:
        pipeline.append({"$match": {"project_id": project_id}})
    pipeline += [
        {"$match": {f"metadata.{key}": {"$exists": True, "$ne": None}}},
        {"$group": {"_id": f"$metadata.{key}", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": MAX_FACET_VALUES},
    ]
    return [
        {"value": r["_id"], "count": r["count"]}
        async for r in db.objects.aggregate(pipeline)
    ]


async def bulk_update_metadata(
    object_ids: list[PydanticObjectId],
    *,
    set_values: dict | None = None,
    unset_keys: list[str] | None = None,
) -> dict:
    """Apply metadata changes across many objects in one operation.

    Set values are *merged*, never replacing the whole metadata document: a
    bulk edit that assigns `batch` must not silently erase every other field
    those files already carry.
    """
    if not object_ids:
        return {"matched": 0, "modified": 0, "warnings": []}

    from datetime import UTC, datetime

    update: dict = {"$set": {"updated_at": datetime.now(UTC)}}
    warnings: list[dict] = []

    if set_values:
        from app.metadata import schemas

        validated = schemas.coerce_and_validate(set_values)
        warnings = validated.warnings
        for key, value in validated.values.items():
            update["$set"][f"metadata.{key}"] = value

    if unset_keys:
        update["$unset"] = {f"metadata.{k}": "" for k in unset_keys}

    result = await get_db().objects.update_many({"_id": {"$in": object_ids}}, update)
    log.info(
        "bulk_metadata_updated",
        matched=result.matched_count,
        modified=result.modified_count,
    )
    return {
        "matched": result.matched_count,
        "modified": result.modified_count,
        "warnings": warnings,
    }


async def bulk_update_tags(
    object_ids: list[PydanticObjectId],
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict:
    """Add and/or remove tags across many objects."""
    if not object_ids:
        return {"matched": 0, "modified": 0}

    from datetime import UTC, datetime

    db = get_db()
    matched = modified = 0

    # $addToSet and $pull touch the same field, so Mongo cannot take both in a
    # single update document; two passes keep each one atomic.
    if add:
        r = await db.objects.update_many(
            {"_id": {"$in": object_ids}},
            {
                "$addToSet": {"tags": {"$each": [t.strip() for t in add if t.strip()]}},
                "$set": {"updated_at": datetime.now(UTC)},
            },
        )
        matched, modified = r.matched_count, r.modified_count

    if remove:
        r = await db.objects.update_many(
            {"_id": {"$in": object_ids}},
            {
                "$pull": {"tags": {"$in": [t.strip() for t in remove if t.strip()]}},
                "$set": {"updated_at": datetime.now(UTC)},
            },
        )
        matched = max(matched, r.matched_count)
        modified += r.modified_count

    return {"matched": matched, "modified": modified}


def parse_metadata_filters(raw: list[str]) -> dict:
    """Parse `key=value`, `key>=30`, `key!=x`, `key:*` query parameters.

    Query strings are the natural place for filters to live (they make a search
    shareable and survive a reload), so they need a compact syntax.
    """
    out: dict[str, Any] = {}
    for item in raw:
        if not item:
            continue
        for token, op in ((">=", "gte"), ("<=", "lte"), ("!=", "ne"), (">", "gt"),
                          ("<", "lt"), ("~", "contains"), ("=", None)):
            if token in item:
                key, _, value = item.partition(token)
                key, value = key.strip(), value.strip()
                if not key:
                    break
                if value == "*":
                    out[key] = {"exists": True}
                elif op is None:
                    out[key] = _maybe_number(value)
                else:
                    out[key] = {op: _maybe_number(value)}
                break
        else:
            out[item.strip()] = {"exists": True}
    return out


def _maybe_number(value: str):
    """Numeric-looking values become numbers so ranges compare correctly."""
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def valid_kinds() -> list[str]:
    return [k.value for k in FormatKind]


def valid_statuses() -> list[str]:
    return [s.value for s in ObjectStatus]
