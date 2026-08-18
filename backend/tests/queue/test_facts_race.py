"""Regression tests for #606: ingest_headers and index_bam race on facts.

After an alignment both jobs finish at nearly the same moment on the same
BAM. Each applier used to load the whole ``facts`` dict, merge its own keys
in memory, and write the merged dict back -- so whichever loaded first wrote
a merge computed from a stale snapshot, erasing every key the other had just
added. A BAM would end up with flagstat numbers and no ``sort_order``, which
made the Results computation refuse a coordinate-sorted file.

These tests reproduce the race deterministically rather than by timing:
``DataObject.get`` is wrapped so the competing applier's write lands *after*
the applier under test has taken its snapshot but before it writes. With the
whole-dict merge that snapshot wins and the competing keys vanish; with
per-key writes both key sets survive.
"""

import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.models import DataObject, ObjectRole
from app.queue import results
from app.services import object_service, project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

HEADER_FACTS = {"sort_order": "coordinate", "sam_version": "1.6", "reference_count": 1}
FLAGSTAT_FACTS = {"mapped_pct": 99.2, "properly_paired_pct": 97.0}


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    """Stub the enqueues these appliers reach, same as test_results_owner."""

    async def _skip_ingest(obj, **kwargs):
        return ""

    async def _skip_enqueue(*args, **kwargs):
        return None

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip_ingest)
    monkeypatch.setattr("app.queue.queue.enqueue", _skip_enqueue)


def _scratch_file() -> Path:
    """Unique bytes per call so ingest never dedupes onto another test's blob."""
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    path = settings.tmp_dir / f"facts-race-{uuid.uuid4().hex}.tmp"
    path.write_bytes(uuid.uuid4().bytes)
    return path


async def _bam(owner: str) -> DataObject:
    project = await project_service.create_project(name=f"{owner}-proj", owner=owner)
    return await object_service.ingest_local_file(
        owner=owner,
        project_id=project.id,
        path=_scratch_file(),
        name="reads.bam",
        role=ObjectRole.ALIGNMENT,
    )


def _write_in_the_window(monkeypatch, target: DataObject, competing: dict):
    """Make the applier's own ``DataObject.get`` a stale read.

    The wrapper fetches the object, then applies ``competing`` facts straight
    to the database -- simulating the other applier finishing inside the
    read-modify-write window -- and hands back the pre-write snapshot.
    """
    real_get = DataObject.get
    fired = False

    async def stale_get(object_id, *args, **kwargs):
        nonlocal fired
        obj = await real_get(object_id, *args, **kwargs)
        if not fired and obj is not None and obj.id == target.id:
            fired = True
            await DataObject.find_one(DataObject.id == target.id).update(
                {"$set": {f"facts.{k}": v for k, v in competing.items()}}
            )
        return obj

    monkeypatch.setattr(DataObject, "get", stale_get)


class TestConcurrentAppliersKeepEachOthersFacts:
    async def test_index_bam_keeps_header_facts_landing_in_its_window(
        self, monkeypatch
    ):
        """The direction observed in #606: index_bam won, sort_order vanished."""
        owner = "facts-race-a"
        bam = await _bam(owner)
        output = _scratch_file()
        _write_in_the_window(monkeypatch, bam, HEADER_FACTS)

        await results._apply_index_bam(
            {
                "bam_object_id": str(bam.id),
                "output": {"tmp_path": str(output), "name": "reads.bam.bai"},
                "facts": dict(FLAGSTAT_FACTS),
            },
            owner=owner,
        )

        refreshed = await DataObject.find_one(DataObject.id == bam.id)
        assert refreshed is not None
        for key, value in {**HEADER_FACTS, **FLAGSTAT_FACTS}.items():
            assert refreshed.facts.get(key) == value, key
        assert refreshed.facts.get("has_index") is True

    async def test_ingest_headers_keeps_flagstat_facts_landing_in_its_window(
        self, monkeypatch
    ):
        """The other order of the same race: the flagstat numbers must survive,
        and the True ``has_index`` the index job stamped must not be clobbered
        back to the parse's sibling-file False."""
        owner = "facts-race-b"
        bam = await _bam(owner)
        _write_in_the_window(
            monkeypatch, bam, {**FLAGSTAT_FACTS, "has_index": True}
        )

        await results._apply_ingest_headers(
            {
                "object_id": str(bam.id),
                "facts": {**HEADER_FACTS, "has_index": False},
            },
            owner=owner,
        )

        refreshed = await DataObject.find_one(DataObject.id == bam.id)
        assert refreshed is not None
        for key, value in {**HEADER_FACTS, **FLAGSTAT_FACTS}.items():
            assert refreshed.facts.get(key) == value, key
        assert refreshed.facts.get("has_index") is True

    async def test_ingest_headers_still_records_a_missing_index(self):
        """Unraced, the parse's ``has_index: False`` is real information --
        the facts table shows "Indexed: No" from it -- and must still land."""
        owner = "facts-race-c"
        bam = await _bam(owner)

        await results._apply_ingest_headers(
            {
                "object_id": str(bam.id),
                "facts": {**HEADER_FACTS, "has_index": False},
            },
            owner=owner,
        )

        refreshed = await DataObject.find_one(DataObject.id == bam.id)
        assert refreshed is not None
        assert refreshed.facts.get("has_index") is False
        assert refreshed.facts.get("sort_order") == "coordinate"
