"""What the CheckM2 applier writes onto the bins it scored.

Runs against a real database, for `test_binning_apply.py`'s reason: the
guarantees worth testing here are statements about what got *written* --
that one bin failing cannot cost the others their scores, and that a
per-key merge did not clobber facts another job put on the same bin.
A pure mapping test cannot make either claim.
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


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    async def _skip_ingest(obj, **kwargs):
        return ""

    async def _skip_enqueue(*args, **kwargs):
        return None

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip_ingest)
    monkeypatch.setattr("app.queue.queue.enqueue", _skip_enqueue)


def _fasta() -> Path:
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    path = settings.tmp_dir / f"bin-{uuid.uuid4().hex}.fa"
    path.write_text(f"; {uuid.uuid4().hex}\n>ctg0\n" + "ACGT" * 30 + "\n")
    return path


async def _assembly_with_bins(owner: str, count: int = 3):
    project = await project_service.create_project(name=f"{owner}-proj", owner=owner)
    assembly = await object_service.ingest_local_file(
        owner=owner,
        project_id=project.id,
        path=_fasta(),
        name="community.assembly.fasta",
        role=ObjectRole.REFERENCE,
    )
    bins = []
    for i in range(1, count + 1):
        bins.append(
            await object_service.ingest_local_file(
                owner=owner,
                project_id=project.id,
                path=_fasta(),
                name=f"community.bin.{i:03d}.fasta",
                role=ObjectRole.REFERENCE,
                facts={
                    "bin_index": i,
                    "bin_source_assembly": str(assembly.id),
                    "bin_contig_count": 1,
                },
            )
        )
    return assembly, bins


def _result(assembly, scored: list[dict]) -> dict:
    return {
        "assembly_id": str(assembly.id),
        "db_key": "uniref100",
        "tool_version": "1.1.0",
        "scored": scored,
    }


class TestScoresLandOnTheBins:
    async def test_each_bin_gets_its_own_scores(self):
        """R3: completeness and contamination, per bin."""
        assembly, bins = await _assembly_with_bins("checkm2-happy")
        scored = [
            {
                "object_id": str(b.id),
                "facts": {
                    "checkm2_completeness": 90.0 + i,
                    "checkm2_contamination": float(i),
                    "checkm2_quality_tier": "high",
                },
            }
            for i, b in enumerate(bins)
        ]

        await results._apply_checkm2_scores(
            _result(assembly, scored), owner="checkm2-happy"
        )

        for i, b in enumerate(bins):
            fresh = await DataObject.get(b.id)
            assert fresh.facts["checkm2_completeness"] == 90.0 + i
            assert fresh.facts["checkm2_contamination"] == float(i)

    async def test_the_merge_preserves_the_bins_existing_facts(self):
        """#606: per-key `facts.<key>` paths, never a whole-dict merge.

        The bin identity facts #728 wrote must survive being scored -- a
        whole-dict merge computed from a stale snapshot would erase them.
        """
        assembly, bins = await _assembly_with_bins("checkm2-merge", count=1)
        target = bins[0]

        await results._apply_checkm2_scores(
            _result(
                assembly,
                [{"object_id": str(target.id), "facts": {"checkm2_completeness": 77.0}}],
            ),
            owner="checkm2-merge",
        )

        fresh = await DataObject.get(target.id)
        assert fresh.facts["checkm2_completeness"] == 77.0
        # Written by #728's binning applier, untouched here.
        assert fresh.facts["bin_index"] == 1
        assert fresh.facts["bin_source_assembly"] == str(assembly.id)

    async def test_contamination_over_100_survives_to_the_fact(self):
        """R6: not clamped anywhere along the path."""
        assembly, bins = await _assembly_with_bins("checkm2-contam", count=1)

        await results._apply_checkm2_scores(
            _result(
                assembly,
                [
                    {
                        "object_id": str(bins[0].id),
                        "facts": {
                            "checkm2_completeness": 88.0,
                            "checkm2_contamination": 137.4,
                        },
                    }
                ],
            ),
            owner="checkm2-contam",
        )

        fresh = await DataObject.get(bins[0].id)
        assert fresh.facts["checkm2_contamination"] == 137.4


class TestPartialFailure:
    async def test_one_unresolvable_bin_does_not_cost_the_others(self):
        """The per-bin loop's whole purpose, `_apply_binning`'s posture."""
        from beanie import PydanticObjectId

        assembly, bins = await _assembly_with_bins("checkm2-partial", count=3)
        scored = [
            {"object_id": str(bins[0].id), "facts": {"checkm2_completeness": 91.0}},
            # An id no object has -- logged and skipped, not fatal.
            {
                "object_id": str(PydanticObjectId()),
                "facts": {"checkm2_completeness": 50.0},
            },
            {"object_id": str(bins[2].id), "facts": {"checkm2_completeness": 93.0}},
        ]

        await results._apply_checkm2_scores(
            _result(assembly, scored), owner="checkm2-partial"
        )

        assert (await DataObject.get(bins[0].id)).facts["checkm2_completeness"] == 91.0
        assert (await DataObject.get(bins[2].id)).facts["checkm2_completeness"] == 93.0

    async def test_no_bin_is_deleted_or_hidden_by_a_low_score(self):
        """R5: store, do not act.

        A 40%-complete bin is a legitimate result for a low-abundance
        organism; discarding it would destroy the finding that the organism
        is present at all.
        """
        assembly, bins = await _assembly_with_bins("checkm2-lowscore", count=2)
        scored = [
            {
                "object_id": str(b.id),
                "facts": {
                    "checkm2_completeness": 4.0,
                    "checkm2_contamination": 200.0,
                    "checkm2_quality_tier": "low",
                },
            }
            for b in bins
        ]

        await results._apply_checkm2_scores(
            _result(assembly, scored), owner="checkm2-lowscore"
        )

        for b in bins:
            fresh = await DataObject.get(b.id)
            assert fresh is not None
            assert fresh.status == b.status


class TestTheAssemblysOwnFacts:
    async def test_the_assembly_records_how_many_bins_were_scored(self):
        assembly, bins = await _assembly_with_bins("checkm2-count", count=3)
        scored = [
            {"object_id": str(b.id), "facts": {"checkm2_completeness": 80.0}}
            for b in bins
        ]

        await results._apply_checkm2_scores(
            _result(assembly, scored), owner="checkm2-count"
        )

        fresh = await DataObject.get(assembly.id)
        assert fresh.facts["checkm2_scored_bins"] == 3
        assert fresh.facts["checkm2_db_key"] == "uniref100"

    async def test_an_empty_result_writes_nothing(self):
        assembly, _bins = await _assembly_with_bins("checkm2-empty", count=1)

        await results._apply_checkm2_scores(
            _result(assembly, []), owner="checkm2-empty"
        )

        fresh = await DataObject.get(assembly.id)
        assert "checkm2_scored_bins" not in (fresh.facts or {})
