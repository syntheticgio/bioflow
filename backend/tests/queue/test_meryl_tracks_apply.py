"""analyze_meryl_tracks results reach the assembly object.

#612: the handler returned ``{"object_id", "job_id", "facts"}`` exactly like
``analyze_gc_tracks``, but ``_APPLIERS`` had no ``analyze_meryl_tracks``
entry, and ``results.apply()`` silently returns for unknown job types — a
green job whose facts never left ``Job.result``. The registry test is the
one that fails if the entry is ever dropped again; the merge test pins the
applier's contract (merge facts, don't replace them).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.queue import results

OWNER = "meryl-apply-owner"
OBJECT_ID = "507f1f77bcf86cd799439011"


def test_analyze_meryl_tracks_is_registered():
    assert (
        results._APPLIERS["analyze_meryl_tracks"]
        is results._apply_analyze_meryl_tracks
    )


def _facts_payload(set_mock: AsyncMock) -> dict:
    """The facts dict passed to obj.set — keyed by a Beanie ExpressionField,
    which is a str subclass, so match on its string value."""
    payload = set_mock.await_args.args[0]
    return next(v for key, v in payload.items() if str(key) == "facts")


@pytest.mark.asyncio
async def test_apply_merges_facts_onto_object(monkeypatch):
    obj = MagicMock()
    obj.facts = {"sequence_count": 3}
    obj.set = AsyncMock()

    # A stub class, not a patched classmethod: outside init_beanie, accessing
    # DataObject.facts at class level raises AttributeError.
    fake_cls = MagicMock()
    fake_cls.get = AsyncMock(return_value=obj)
    fake_cls.facts = "facts"
    fake_cls.updated_at = "updated_at"
    monkeypatch.setattr(results, "DataObject", fake_cls)

    await results._apply_analyze_meryl_tracks(
        {
            "object_id": OBJECT_ID,
            "job_id": "irrelevant",
            "facts": {
                "kmer_spectra": {"k": 21, "total_kmers": 8940},
                "repeat_density": {"k": 21, "contigs": [{"name": "c1"}]},
            },
        },
        owner=OWNER,
    )

    obj.set.assert_awaited_once()
    merged = _facts_payload(obj.set)
    assert merged["kmer_spectra"]["total_kmers"] == 8940
    assert merged["repeat_density"]["contigs"][0]["name"] == "c1"
    # Merge, not replace: pre-existing facts survive.
    assert merged["sequence_count"] == 3


@pytest.mark.asyncio
async def test_apply_is_a_noop_without_facts(monkeypatch):
    fake_cls = MagicMock()
    fake_cls.get = AsyncMock()
    monkeypatch.setattr(results, "DataObject", fake_cls)
    get = fake_cls.get

    await results._apply_analyze_meryl_tracks(
        {"object_id": OBJECT_ID, "job_id": "irrelevant", "facts": {}},
        owner=OWNER,
    )

    get.assert_not_awaited()
