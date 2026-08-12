"""Object search, filtering, and facet aggregation.

Metadata keys are user-defined, so queries against them cannot rely on a
purpose-built compound index. The wildcard index on `metadata.$**` covers the
equality case, which is what "find every file from patient P-041" needs.

Free-text search is a case-insensitive regex on `name` rather than a Mongo text
index: filenames are the thing people actually search, they are short, and
partial matching (`SampleA_R1` from `sample`) matters more here than stemming.
At single-user scale that is a good trade; a text index becomes worthwhile only
if this ever holds millions of objects.

Every public function here takes a required keyword-only `owner`. Search is the
widest read surface in the application -- it is the one place that queries
across projects by default -- so an unscoped filter here discloses more than any
single-row leak could. `build_filter` is the choke point all three read paths go
through, and it seeds its filter with the owner clause before looking at
anything the user asked for.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from beanie import PydanticObjectId

from app.db.client import get_db
from app.errors import NotFoundError
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


def build_filter(q: SearchQuery, *, owner: str) -> dict:
    """Translate a SearchQuery into a MongoDB filter document.

    `owner` is a separate required keyword argument rather than a field on
    `SearchQuery`, and the distinction matters. A `SearchQuery` is assembled
    straight from user-supplied query parameters in the route -- text, kinds,
    tags, metadata are all things the caller chose. Filing the owner in that
    same bag would make the partition boundary look like one more user-tunable
    filter, one `SearchQuery(**params)` away from being set by the client.
    Keeping it out of the dataclass means the only thing that can supply it is
    the caller, and the only caller is a route holding an `OwnerDep`.

    It also has no default, on purpose. Every search, facet and metadata-value
    query in this module funnels through here, so a default -- even `"local"`
    -- would make an unscoped filter constructible by omission, which is
    exactly the leak this closes. Without one, forgetting it is a TypeError at
    the call site rather than a silent cross-profile read.
    """
    f: dict[str, Any] = {"owner": owner}

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


async def search_objects(q: SearchQuery, *, owner: str) -> dict:
    """Run a search. Returns results plus a cursor for the next page."""
    limit = max(1, min(q.limit, MAX_LIMIT))
    filt = build_filter(q, owner=owner)

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
        # Rebuilt rather than reusing `filt`: the cursor clause was added to
        # that one in place, and the total is a count of the whole result set,
        # not of the page after the boundary.
        "total": await DataObject.find(build_filter(q, owner=owner)).count(),
    }


async def facets(project_id: PydanticObjectId | None = None, *, owner: str) -> dict:
    """Distinct values and counts for building the filter UI.

    Everything is aggregated server-side; pulling documents back to count them
    in Python would scale with the library, on an endpoint the UI polls.

    Facets leak by counting. Even though no document is returned, a tag list
    naming another profile's cohorts, or a metadata key list naming their
    fields, discloses the shape of a library the caller cannot open -- so the
    owner match leads every pipeline below, unconditionally. It used to be
    `if match`, applied only when a project was named; the match is now never
    empty, which is the point.
    """
    db = get_db()
    match: dict = {"owner": owner}
    if project_id:
        match["project_id"] = project_id

    async def group_by(field_name: str, limit: int = MAX_FACET_VALUES) -> list[dict]:
        pipeline: list[dict] = [{"$match": match}]
        pipeline += [
            {"$group": {"_id": f"${field_name}", "count": {"$sum": 1}}},
            {"$match": {"_id": {"$ne": None}}},
            {"$sort": {"count": -1}},
            {"$limit": limit},
        ]
        return [
            {"value": r["_id"], "count": r["count"]}
            async for r in await db.objects.aggregate(pipeline)
        ]

    async def unwound(field_name: str, limit: int = MAX_FACET_VALUES) -> list[dict]:
        pipeline: list[dict] = [{"$match": match}]
        pipeline += [
            {"$unwind": f"${field_name}"},
            {"$group": {"_id": f"${field_name}", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": limit},
        ]
        return [
            {"value": r["_id"], "count": r["count"]}
            async for r in await db.objects.aggregate(pipeline)
        ]

    # Which metadata keys exist at all, and how often -- this is what makes an
    # open-ended schema browsable instead of guesswork.
    key_pipeline: list[dict] = [{"$match": match}]
    key_pipeline += [
        {"$project": {"kv": {"$objectToArray": {"$ifNull": ["$metadata", {}]}}}},
        {"$unwind": "$kv"},
        {"$group": {"_id": "$kv.k", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": MAX_FACET_VALUES},
    ]
    metadata_keys = [
        {"key": r["_id"], "count": r["count"]}
        async for r in await db.objects.aggregate(key_pipeline)
    ]

    return {
        "formats": await group_by("format.kind"),
        "statuses": await group_by("status"),
        "tags": await unwound("tags"),
        "metadata_keys": metadata_keys,
    }


async def metadata_values(
    key: str, project_id: PydanticObjectId | None = None, *, owner: str
) -> list[dict]:
    """Distinct values for one metadata key, for a value picker.

    The most directly disclosing of the three read paths: the values *are* the
    data. An unscoped `sample_id` picker hands over every patient identifier in
    the database.
    """
    db = get_db()
    match: dict = {"owner": owner}
    if project_id:
        match["project_id"] = project_id
    pipeline: list[dict] = [{"$match": match}]
    pipeline += [
        {"$match": {f"metadata.{key}": {"$exists": True, "$ne": None}}},
        {"$group": {"_id": f"$metadata.{key}", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": MAX_FACET_VALUES},
    ]
    return [
        {"value": r["_id"], "count": r["count"]}
        async for r in await db.objects.aggregate(pipeline)
    ]


async def _assert_all_owned(object_ids: list[PydanticObjectId], *, owner: str) -> None:
    """Refuse the whole batch unless every id belongs to `owner`.

    The alternative -- narrow the update to `{"_id": {"$in": ids}, "owner":
    owner}` and let the counts come back short -- is safe, and it was the
    tempting one, because the filter alone already makes another profile's row
    unreachable. It is rejected here for what the caller is then told.

    These two functions return `{"matched", "modified"}` and nothing else.
    Silently dropping the ids that were not the caller's would answer a
    50-object edit with `matched: 30` and no way to distinguish "20 of those
    ids belong to someone else" from "20 of them no longer exist" -- the same
    number for a typo, a stale tab, and a genuine cross-profile reach. The
    caller sees a partial success it did not ask for and cannot diagnose.
    Widening the return shape to report which ids were skipped would leak the
    one fact the partition exists to hide: that those ids are real and belong
    to somebody.

    So the batch is all-or-nothing, and the refusal is a NotFoundError naming
    only a count. That is the same answer `object_service.get_object` gives for
    a wrong-owner id -- unreachable and absent are one response, so no error
    tells a caller that an id they cannot touch exists.
    """
    if not object_ids:
        return

    # A count, not a fetch: the check needs to know how many of these ids are
    # the caller's, and pulling the documents to compare owners in Python would
    # read rows this profile has no business holding, even transiently.
    owned = await get_db().objects.count_documents(
        {"_id": {"$in": list(set(object_ids))}, "owner": owner}
    )
    missing = len(set(object_ids)) - owned
    if missing:
        raise NotFoundError(
            f"{missing} of {len(set(object_ids))} objects were not found",
            details={"requested": len(set(object_ids)), "found": owned},
        )


async def bulk_update_metadata(
    object_ids: list[PydanticObjectId],
    *,
    owner: str,
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

    await _assert_all_owned(object_ids, owner=owner)

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

    # The owner clause is repeated on the update even though `_assert_all_owned`
    # just passed. The check and the write are two round trips, so the filter is
    # what actually guarantees the invariant at the moment of writing; the check
    # exists to turn a partial write into a clean refusal, not to authorise this
    # one. If the two ever disagree, the filter is the half that must hold.
    result = await get_db().objects.update_many(
        {"_id": {"$in": object_ids}, "owner": owner}, update
    )
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
    owner: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict:
    """Add and/or remove tags across many objects."""
    if not object_ids:
        return {"matched": 0, "modified": 0}

    await _assert_all_owned(object_ids, owner=owner)

    from datetime import UTC, datetime

    db = get_db()
    matched = modified = 0
    # Both passes below filter on owner as well as id, for the reason spelled
    # out in bulk_update_metadata: the pre-check and the write are separate
    # round trips, and this clause is the one that holds at write time.
    scope = {"_id": {"$in": object_ids}, "owner": owner}

    # $addToSet and $pull touch the same field, so Mongo cannot take both in a
    # single update document; two passes keep each one atomic.
    if add:
        r = await db.objects.update_many(
            scope,
            {
                "$addToSet": {"tags": {"$each": [t.strip() for t in add if t.strip()]}},
                "$set": {"updated_at": datetime.now(UTC)},
            },
        )
        matched, modified = r.matched_count, r.modified_count

    if remove:
        r = await db.objects.update_many(
            scope,
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


async def count_by_kind(project_id: PydanticObjectId, *, owner: str) -> dict[str, int]:
    """Count objects grouped by format kind for a project.

    Returns a dict like {"fastq": 12, "bam": 3, "reference": 7}. Used by the
    agent's project context injection to give the agent a picture of the project
    without a discovery tool call.
    """
    db = get_db()
    pipeline: list[dict] = [
        {"$match": {"owner": owner, "project_id": project_id}},
        {"$group": {"_id": "$format.kind", "count": {"$sum": 1}}},
    ]
    result: dict[str, int] = {}
    async for row in db.objects.aggregate(pipeline):
        kind = row["_id"]
        if kind:
            result[kind] = row["count"]
    return result
