"""The launch path for CheckM2 bin scoring.

The behaviour this launch actually guards is the **database chaining**
(spec Q2/R2): when the database is absent the download is enqueued and the
scoring job depends on it, rather than the launch refusing. Both directions
are asserted, because "present -> no download" is the half that silently
regresses into a duplicate 9.3 GB fetch on every run.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.errors import PermanentError, ValidationError
from app.models import FormatKind, ObjectRole
from app.pipelines.tools import Tool
from app.services import pipeline_service
from tests.services.conftest import _obj

_CHECKM2 = Tool(name="checkm2", path="/usr/local/bin/checkm2", version="1.1.0")


def _assembly(*, bins=2):
    obj = _obj(
        name="community.assembly.fasta",
        kind=FormatKind.FASTA,
        role=ObjectRole.REFERENCE,
    )
    obj.facts = {"binning_bin_count": bins}
    return obj


def _bin(*, project_id, source_id, index):
    obj = _obj(
        name=f"bin.{index}.fa",
        kind=FormatKind.FASTA,
        role=ObjectRole.REFERENCE,
        project_id=project_id,
    )
    obj.facts = {"bin_index": index, "bin_source_assembly": str(source_id)}
    return obj


async def _run(*, assembly, bins, db_present=True, tool=_CHECKM2):
    # The declared 16 GB is above the test environment's admission budget, and
    # admission control is not what this suite is about -- test_admission
    # covers that. Overriding keeps each assertion below about the thing it
    # names (chaining, payload shape) rather than about the budget.
    objects = {assembly.id: assembly}

    async def _get_object(object_id, *, owner):
        return objects.get(object_id)

    enqueued: list[dict] = []

    async def _enqueue(job_type, **kwargs):
        job_id = f"job{len(enqueued) + 1}"
        entry = {"type": job_type, "job_id": job_id, **kwargs}
        enqueued.append(entry)
        return SimpleNamespace(id=job_id)

    with (
        patch("app.pipelines.tools.checkm2", return_value=tool),
        patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=_get_object),
        ),
        patch(
            "app.services.pipeline_service._bins_of_assembly",
            AsyncMock(return_value=bins),
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("a" * 64, None)),
        ),
        patch(
            "app.pipelines.checkm2_db_registry.db_present",
            return_value=db_present,
        ),
        patch("app.queue.queue.enqueue", _enqueue),
    ):
        job = await pipeline_service.launch_bin_qc(
            object_id=assembly.id, owner="local", resource_override=True
        )
    return job, enqueued


class TestTheHappyPath:
    async def test_one_job_scores_every_bin(self):
        """Q3: one job over the whole set, never one job per bin."""
        assembly = _assembly(bins=3)
        bins = [
            _bin(project_id=assembly.project_id, source_id=assembly.id, index=i)
            for i in (1, 2, 3)
        ]

        _job, enqueued = await _run(assembly=assembly, bins=bins)

        scoring = [e for e in enqueued if e["type"] == "score_bin_quality"]
        assert len(scoring) == 1
        payload = scoring[0]["payload"]
        assert payload["assembly_id"] == str(assembly.id)
        assert len(payload["bins"]) == 3
        assert {b["object_id"] for b in payload["bins"]} == {
            str(b.id) for b in bins
        }

    async def test_memory_comes_from_the_registry_not_the_model(self):
        """Q1: mem_mb is known a priori, never fitted."""
        from app.pipelines.checkm2_db_registry import CHECKM2_DBS, DEFAULT_DB

        assembly = _assembly()
        bins = [_bin(project_id=assembly.project_id, source_id=assembly.id, index=1)]

        _job, enqueued = await _run(assembly=assembly, bins=bins)

        scoring = next(e for e in enqueued if e["type"] == "score_bin_quality")
        assert scoring["resources"].mem_mb == CHECKM2_DBS[DEFAULT_DB].mem_mb


class TestDatabaseChaining:
    """Spec Q2/R2 -- the reason this launcher is not a one-liner."""

    async def test_absent_database_enqueues_the_download_and_depends_on_it(self):
        assembly = _assembly()
        bins = [_bin(project_id=assembly.project_id, source_id=assembly.id, index=1)]

        _job, enqueued = await _run(assembly=assembly, bins=bins, db_present=False)

        types = [e["type"] for e in enqueued]
        assert "download_checkm2_db" in types
        # The download is enqueued FIRST, and the scoring job depends on it.
        download = next(e for e in enqueued if e["type"] == "download_checkm2_db")
        scoring = next(e for e in enqueued if e["type"] == "score_bin_quality")
        assert scoring["depends_on"] == [download["job_id"]]

    async def test_present_database_enqueues_no_download(self):
        """The half that silently regresses into a duplicate 9.3 GB fetch."""
        assembly = _assembly()
        bins = [_bin(project_id=assembly.project_id, source_id=assembly.id, index=1)]

        _job, enqueued = await _run(assembly=assembly, bins=bins, db_present=True)

        types = [e["type"] for e in enqueued]
        assert "download_checkm2_db" not in types
        scoring = next(e for e in enqueued if e["type"] == "score_bin_quality")
        assert not scoring["depends_on"]


class TestRefusals:
    async def test_an_unbinned_assembly_is_refused(self):
        """Without bins there is nothing to score -- a missing input."""
        assembly = _assembly()

        with pytest.raises(ValidationError) as e:
            await _run(assembly=assembly, bins=[])
        assert "bin" in str(e.value).lower()

    async def test_a_missing_tool_is_refused(self):
        assembly = _assembly()
        bins = [_bin(project_id=assembly.project_id, source_id=assembly.id, index=1)]
        absent = Tool(
            name="checkm2", path=None, version=None, error="CheckM2 is not available"
        )

        # PermanentError specifically, and the message names the tool: a
        # blind `Exception` here would pass on the budget refusal too, which
        # is a different failure entirely.
        with pytest.raises(PermanentError) as e:
            await _run(assembly=assembly, bins=bins, tool=absent)
        assert "checkm2" in str(e.value)
