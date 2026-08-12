"""Reconcile Beanie's declared indexes against the live database at startup.

init_beanie calls createIndexes for every index in a model's Settings.indexes.
If an index exists with the same name but a different definition — different
keys, unique flag, partialFilterExpression, sparse, or TTL — MongoDB rejects
it with IndexKeySpecsConflict (code 86) and the API exits during startup.

This module runs before init_beanie, compares each declared index against what
the database actually has, and drops any whose definition differs. init_beanie
then recreates them cleanly. The mechanism is idempotent: if nothing changed,
nothing is dropped, and create_indexes is a no-op for identical indexes.

Orphaned indexes (in the DB but not in the model) are left alone. Dropping an
index is destructive; the safe default is to log and let a human decide.
"""

from pymongo import IndexModel
from pymongo.asynchronous.collection import AsyncCollection

from app.logging import get_logger

log = get_logger(__name__)


def _freeze(obj):
    """Recursively convert a dict/list structure into a hashable form.

    partialFilterExpression values contain nested dicts (e.g.
    {"$in": ["pending", ...]}) and frozenset(dict.items()) fails on
    unhashable nested values. This walks the structure and freezes
    dicts to frozensets and lists to tuples.
    """
    if isinstance(obj, dict):
        return frozenset((k, _freeze(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return tuple(_freeze(item) for item in obj)
    return obj


def _index_def(doc: dict) -> tuple:
    """A hashable representation of the properties that define an index.

    Two indexes with the same _index_def are compatible: createIndexes is a
    no-op. Different means the existing one must be dropped before the new
    one can be created, or MongoDB raises IndexKeySpecsConflict (code 86).

    The properties compared are exactly those MongoDB considers part of the
    index specification: key pattern, unique, partialFilterExpression, sparse,
    and expireAfterSeconds. Collation is not compared because this project
    does not use collated indexes; adding one would require extending this
    function, which is the right place for it.
    """
    # The key pattern.  IndexModel.document returns a dict
    # ({"foo": 1}); index_information() returns a list of
    # tuples ([("foo", 1)]).  Normalize both to a tuple of (field,
    # direction) pairs so the comparison is format-independent.
    raw_key = doc.get("key", [])
    if isinstance(raw_key, dict):
        key = tuple(raw_key.items())
    else:
        key = tuple(raw_key)

    flags = (
        doc.get("unique", False),
        doc.get("sparse", False),
    )

    # partialFilterExpression: a dict or absent.  Recursively freeze
    # so nested dicts and lists are hashable.  None and absent are
    # treated the same (no partial filter).
    pfe = doc.get("partialFilterExpression")
    pfe_key = _freeze(pfe) if pfe else None

    # TTL: expireAfterSeconds. None and absent are the same (no TTL).
    ttl = doc.get("expireAfterSeconds")

    return (key, flags, pfe_key, ttl)


async def reconcile_indexes(
    collection: AsyncCollection,
    declared: list[IndexModel],
) -> list[str]:
    """Drop indexes whose definition conflicts with what the model declares.

    Returns the list of index names that were dropped. Safe to call on every
    startup: if nothing changed, returns an empty list.

    Orphaned indexes (in the DB but not declared) are NOT dropped — that is a
    destructive operation left to a human.
    """
    live = await collection.index_information()

    # Map declared index names to their definition.
    declared_map: dict[str, dict] = {}
    for im in declared:
        doc = im.document
        name = doc.get("name")
        if name:
            declared_map[name] = doc

    to_drop: list[str] = []

    for name, live_doc in live.items():
        if name == "_id_":
            continue  # cannot be dropped, never conflicts

        if name not in declared_map:
            # Orphaned: in the DB but not declared. Leave it alone.
            log.info(
                "index_orphaned",
                collection=collection.name,
                index=name,
                keys=live_doc.get("key"),
            )
            continue

        live_def = _index_def(live_doc)
        declared_def = _index_def(declared_map[name])

        if live_def != declared_def:
            to_drop.append(name)
            log.info(
                "index_conflict",
                collection=collection.name,
                index=name,
                live=live_def,
                declared=declared_def,
            )

    for name in to_drop:
        await collection.drop_index(name)
        log.info("index_dropped", collection=collection.name, index=name)

    return to_drop
