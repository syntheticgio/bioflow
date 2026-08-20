"""What the binning applier does with N bins from one job.

`_apply_binning` is the only applier that ingests an unbounded number of
objects, so the guarantees worth testing are the ones that only matter at N:
that one bad bin cannot cost the others, and that a run over the cap ingests
nothing at all rather than a truncated, arbitrary subset.

These run against a real database because that is where the guarantees live --
"the other bins survived" is a statement about what got written, and a pure
mapping test cannot make it.
"""

import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.errors import PermanentError
from app.models import DataObject, ObjectRole
from app.queue import results
from app.services import object_service, project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    """Stub the enqueues ingest reaches, same as test_facts_race."""

    async def _skip_ingest(obj, **kwargs):
        return ""

    async def _skip_enqueue(*args, **kwargs):
        return None

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip_ingest)
    monkeypatch.setattr("app.queue.queue.enqueue", _skip_enqueue)


def _fasta(contigs: int = 2, bases: int = 120) -> Path:
    """A unique FASTA per call so ingest never dedupes onto another's blob."""
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    path = settings.tmp_dir / f"bin-{uuid.uuid4().hex}.fa"
    body = f"; {uuid.uuid4().hex}\n"
    per = max(4, bases // max(1, contigs))
    for c in range(contigs):
        body += f">ctg{c}\n" + ("ACGT" * (per // 4)) + "\n"
    path.write_text(body)
    return path


async def _assembly(owner: str) -> DataObject:
    project = await project_service.create_project(name=f"{owner}-proj", owner=owner)
    return await object_service.ingest_local_file(
        owner=owner,
        project_id=project.id,
        path=_fasta(),
        name="community.assembly.fasta",
        role=ObjectRole.REFERENCE,
        metadata={"organism": "mixed community"},
    )


def _bin_entry(index: int) -> dict:
    return {
        "tmp_path": str(_fasta()),
        "name": f"community.bin.{index:03d}.fasta",
        "index": index,
        "contig_count": 2,
        "total_bases": 120,
        "mean_depth": 8.0 + index,
    }


def _result(contigs: DataObject, bins: list[dict], **extra) -> dict:
    return {
        "contigs_id": str(contigs.id),
        "object_id": str(contigs.id),
        "project_id": str(contigs.project_id),
        "tool_version": "2.18",
        "params": {"min_contig": 2500, "threads": 4, "seed": 1},
        "bins": bins,
        "binning_facts": {
            "binning_bin_count": len(bins),
            "binning_binned_bases": 120 * len(bins),
            "binning_unbinned_bases": 0,
        },
        **extra,
    }


async def _bins_of(contigs: DataObject) -> list[DataObject]:
    found = await DataObject.find(
        DataObject.project_id == contigs.project_id
    ).to_list()
    return [o for o in found if (o.facts or {}).get("bin_source_assembly")]


class TestEachBinBecomesAUsableGenome:
    async def test_every_bin_is_ingested_as_a_reference(self):
        """`ObjectRole.REFERENCE`, not a new `ObjectRole.MAG` (design B2/M3).

        A MAG *is* a draft genome, and everything a user does with one next --
        align to it, annotate it, score its completeness -- already keys off
        REFERENCE. This assertion is what makes "usable by the existing cards
        without special-casing" true.
        """
        owner = "bins-role"
        contigs = await _assembly(owner)
        await results._apply_binning(
            _result(contigs, [_bin_entry(i) for i in (1, 2, 3)]), owner=owner
        )
        bins = await _bins_of(contigs)
        assert len(bins) == 3
        assert {b.role for b in bins} == {ObjectRole.REFERENCE}

    async def test_a_bin_traces_back_to_its_community(self):
        owner = "bins-facts"
        contigs = await _assembly(owner)
        await results._apply_binning(
            _result(contigs, [_bin_entry(1), _bin_entry(2)]), owner=owner
        )
        bins = sorted(await _bins_of(contigs), key=lambda b: b.facts["bin_index"])
        first = bins[0]
        assert first.facts["bin_source_assembly"] == str(contigs.id)
        assert first.facts["bin_index"] == 1
        assert first.facts["bin_total_bins"] == 2
        assert first.facts["bin_contig_count"] == 2
        assert first.facts["binned_by"] == "metabat2"
        # The community's sample metadata, carried forward: a MAG came out of
        # this sample and stays findable by it.
        assert first.metadata.get("organism") == "mixed community"

    async def test_a_bin_descends_from_both_the_assembly_and_the_alignment(self):
        """A MAG is a claim about this assembly *at this coverage*, so losing
        the BAM from the lineage would make the binning unreproducible from
        the object graph."""
        owner = "bins-lineage"
        contigs = await _assembly(owner)
        bam = await object_service.ingest_local_file(
            owner=owner,
            project_id=contigs.project_id,
            path=_fasta(),
            name="reads.bam",
            role=ObjectRole.ALIGNMENT,
        )
        await results._apply_binning(
            _result(contigs, [_bin_entry(1)], bam_object_id=str(bam.id)), owner=owner
        )
        [only] = await _bins_of(contigs)
        assert set(only.derived_from) == {contigs.id, bam.id}


class TestOneBadBinDoesNotCostTheOthers:
    async def test_the_survivors_land_and_the_count_reflects_reality(
        self, monkeypatch
    ):
        """R3. A failure on one bin must not lose the rest -- and the count
        reported must be the count that actually landed, not the count
        MetaBAT2 produced. Reporting the latter would tell the user they have
        three MAGs when the project holds two.
        """
        owner = "bins-partial"
        contigs = await _assembly(owner)
        entries = [_bin_entry(i) for i in (1, 2, 3)]
        doomed = entries[1]["name"]

        real_ingest = object_service.ingest_local_file

        async def flaky(*args, **kwargs):
            if kwargs.get("name") == doomed:
                raise RuntimeError("blob store said no")
            return await real_ingest(*args, **kwargs)

        monkeypatch.setattr(object_service, "ingest_local_file", flaky)

        await results._apply_binning(_result(contigs, entries), owner=owner)

        bins = await _bins_of(contigs)
        assert sorted(b.facts["bin_index"] for b in bins) == [1, 3]

        refreshed = await DataObject.get(contigs.id)
        assert refreshed.facts["binning_bin_count"] == 2


class TestTheCapRefusesRatherThanTruncates:
    async def test_nothing_is_ingested_when_the_run_is_over_the_cap(
        self, monkeypatch
    ):
        """R5, and the assertion that matters is "nothing was ingested".

        Asserting only that the job failed would pass an implementation that
        ingested 200 objects and *then* raised -- leaving the user with a
        truncated, arbitrary subset of their MAGs and an error suggesting they
        got none.
        """
        owner = "bins-cap"
        contigs = await _assembly(owner)
        monkeypatch.setattr(settings, "metagenome_bin_cap", 2)

        with pytest.raises(PermanentError) as exc:
            await results._apply_binning(
                _result(contigs, [_bin_entry(i) for i in (1, 2, 3)]), owner=owner
            )

        message = str(exc.value)
        assert "3" in message and "2" in message
        assert await _bins_of(contigs) == []

    async def test_a_run_at_the_cap_is_ingested(self, monkeypatch):
        owner = "bins-at-cap"
        contigs = await _assembly(owner)
        monkeypatch.setattr(settings, "metagenome_bin_cap", 2)
        await results._apply_binning(
            _result(contigs, [_bin_entry(1), _bin_entry(2)]), owner=owner
        )
        assert len(await _bins_of(contigs)) == 2


class TestTheUnbinnedFractionIsAResult:
    async def test_the_unbinned_contigs_become_their_own_object(self):
        """B3. The unbinned fraction is frequently the most interesting part
        of a metagenome -- novel organisms with no close relative -- so it is
        an object that can be re-assembled, re-binned or classified, not a
        number on a fact page."""
        owner = "bins-unbinned"
        contigs = await _assembly(owner)
        await results._apply_binning(
            _result(
                contigs,
                [_bin_entry(1)],
                unbinned={
                    "tmp_path": str(_fasta()),
                    "name": "community.unbinned.fasta",
                    "contig_count": 5,
                    "total_bases": 4000,
                },
            ),
            owner=owner,
        )
        objects = await _bins_of(contigs)
        unbinned = [o for o in objects if (o.facts or {}).get("bin_unbinned")]
        assert len(unbinned) == 1
        assert unbinned[0].role is ObjectRole.REFERENCE
        # Not a bin, so no bin_index -- a reader keys off `bin_unbinned`
        # rather than inferring meaning from a null.
        assert "bin_index" not in unbinned[0].facts

    async def test_the_unbinned_object_is_not_counted_as_a_bin(self):
        """It rides in the same produced list, so a naive count would report
        one MAG too many."""
        owner = "bins-unbinned-count"
        contigs = await _assembly(owner)
        await results._apply_binning(
            _result(
                contigs,
                [_bin_entry(1), _bin_entry(2)],
                unbinned={
                    "tmp_path": str(_fasta()),
                    "name": "community.unbinned.fasta",
                    "contig_count": 5,
                    "total_bases": 4000,
                },
            ),
            owner=owner,
        )
        refreshed = await DataObject.get(contigs.id)
        assert refreshed.facts["binning_bin_count"] == 2
        assert refreshed.facts["binning_unbinned_object"]

    async def test_the_split_lands_on_the_source_assembly(self):
        """So "how much of this community resolved" is answerable without
        opening N objects and adding up their sizes."""
        owner = "bins-split"
        contigs = await _assembly(owner)
        await results._apply_binning(
            _result(contigs, [_bin_entry(1)]), owner=owner
        )
        refreshed = await DataObject.get(contigs.id)
        assert refreshed.facts["binning_binned_bases"] == 120
        assert refreshed.facts["binning_unbinned_bases"] == 0


class TestGuards:
    async def test_a_result_with_no_bins_does_nothing(self):
        owner = "bins-none"
        contigs = await _assembly(owner)
        await results._apply_binning(_result(contigs, []), owner=owner)
        assert await _bins_of(contigs) == []

    async def test_a_missing_parent_is_survived(self):
        """The applier logs and returns rather than raising: the parent being
        gone means the user deleted the assembly, not that the job is broken."""
        await results._apply_binning(
            {
                "contigs_id": "507f1f77bcf86cd799439011",
                "bins": [_bin_entry(1)],
            },
            owner="bins-orphan",
        )
