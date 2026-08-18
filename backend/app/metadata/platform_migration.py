"""Move instrument models out of `metadata.platform`, once.

Before #525, `metadata.platform` was a single open field and SRA enrichment
wrote the INSTRUMENT_MODEL into it -- so every object in the database holds a
machine name ("NextSeq 550", "MinION", "Sequel IIe") in a field the schema now
declares closed over NCBI's PLATFORM tags. After #525 the two are separate
fields and enrichment writes both.

This carries the existing rows across: the stored value moves to
`instrument_model`, and `platform` is re-derived as an SRA tag.

Runs on every startup and does nothing after the first pass, the same shape as
`services/ai/migration.seed_legacy_provider`. Idempotency comes from the data
rather than a flag: an object that already has `instrument_model` is skipped,
so a second run matches nothing. That also makes the migration safe on an
object created after the split, which never needed it.

Deliberately conservative in one place. When neither `facts.sra_platform` nor
the substring table can name a platform, `platform` is *cleared* rather than
guessed at. The instrument model has already been preserved by then, so
nothing is lost, and `_qc_platform` falls back to exactly the inference it
would have done anyway. Writing a wrong tag into a field that now reads as
authoritative would be worse than writing none: `is_short_read` consults
`platform` before chemistry, so a bad guess of ILLUMINA on a nanopore file is
precisely the regression that path exists to prevent.
"""

from app.logging import get_logger
from app.models import SequencingPlatform
from app.models.object import DataObject

log = get_logger(__name__)

_SRA_TAGS = frozenset(p.value for p in SequencingPlatform)


def derive_platform(metadata_platform: str, *, sra_platform: str | None) -> str | None:
    """The SRA tag for a value stored by the pre-split code, or None.

    `facts.sra_platform` wins when present: NCBI stamped it, so it needs no
    inference. Otherwise the value goes through the same substring table
    `sam_platform` uses for read groups, and its SAM answer is translated
    back to SRA's spelling -- which is why "MinION" resolves at all, and why
    a model this repo has never heard of resolves to None instead of a
    plausible-looking default.

    The translation is `qc_stats.SAM_TO_SRA_PLATFORM`, not
    `SHORT_TO_SRA_PLATFORM`: the latter is the inverse of the long-read pair
    only, so every Illumina model -- 40 of the 55 rows this has to migrate --
    would resolve to None and lose its platform.
    """
    from app.pipelines import qc_stats
    from app.services.pipeline_service import sam_platform

    if sra_platform and sra_platform.strip().upper() in _SRA_TAGS:
        return sra_platform.strip().upper()

    # Already a tag -- an object saved by hand, or one this has run over
    # before with the instrument-model copy since removed.
    if metadata_platform.strip().upper() in _SRA_TAGS:
        return metadata_platform.strip().upper()

    sam = sam_platform(metadata_platform)
    if sam is None:
        return None
    return qc_stats.SAM_TO_SRA_PLATFORM.get(sam.value)


async def split_platform_from_instrument_model() -> int:
    """Split every pre-#525 object's platform field. Returns rows changed.

    Goes through Beanie's `DataObject` rather than a raw collection handle so
    it reads the same database the rest of the app does, and so a test can
    exercise it against a throwaway one.
    """
    stale = await DataObject.find(
        {
            "metadata.platform": {"$exists": True, "$nin": [None, ""]},
            "metadata.instrument_model": {"$exists": False},
        }
    ).to_list()

    changed = 0
    unresolved = 0
    for obj in stale:
        stored = (obj.metadata or {}).get("platform")
        if not isinstance(stored, str) or not stored.strip():
            continue

        stored = stored.strip()
        tag = derive_platform(
            stored, sra_platform=(obj.facts or {}).get("sra_platform")
        )

        obj.metadata["instrument_model"] = stored
        if tag:
            obj.metadata["platform"] = tag
        else:
            # See the module docstring: no tag beats a wrong one.
            obj.metadata.pop("platform", None)
            unresolved += 1

        await obj.save()
        changed += 1

    if changed:
        log.info(
            "platform_instrument_model_split",
            objects=changed,
            unresolved_platform=unresolved,
        )
    return changed
