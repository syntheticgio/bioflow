"""Taking a finished UniProt download into the project.

The assertion that matters is the role. A protein FASTA and a reference
genome are both `FormatKind.FASTA`, and only `ObjectRole.PROTEIN` keeps this
file out of the aligner's reference picker -- where selecting it would
produce silently wrong alignments rather than an error.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId

from app.models import ObjectRole
from app.queue import results

PROJECT_ID = "507f1f77bcf86cd799439011"
JOB_ID = "507f1f77bcf86cd799439012"


@pytest.fixture
def staged_file(tmp_path: Path) -> Path:
    path = tmp_path / "UP000002311_reviewed.fasta"
    path.write_text(">sp|P0DTC2|SPIKE_SARS2 Spike\nMFVFLV\n")
    return path


def _result(staged_file: Path) -> dict:
    return {
        "staged": [{"path": str(staged_file), "name": staged_file.name}],
        "protein_count": 6067,
        "release": "2026_02",
        "query": "proteome:UP000002311 AND reviewed:true",
        "proteome_id": "UP000002311",
        "accessions": [],
        "reviewed_only": True,
        "organism": "Saccharomyces cerevisiae",
        "project_id": PROJECT_ID,
        "job_id": JOB_ID,
    }


@pytest.mark.asyncio
class TestApply:
    async def test_it_ingests_as_a_protein(self, staged_file, monkeypatch):
        ingest = AsyncMock()
        monkeypatch.setattr(
            "app.services.object_service.ingest_local_file", ingest
        )
        monkeypatch.setattr(
            "app.services.run_service.run_for_job", AsyncMock(return_value=None)
        )

        await results._apply_uniprot_download(_result(staged_file))

        assert ingest.await_count == 1
        assert ingest.await_args.kwargs["role"] is ObjectRole.PROTEIN

    async def test_provenance_lands_in_facts(self, staged_file, monkeypatch):
        """The query, the release, and whether unreviewed entries were
        included -- what would otherwise be unrecoverable once the file is
        just a FASTA in a project."""
        ingest = AsyncMock()
        monkeypatch.setattr(
            "app.services.object_service.ingest_local_file", ingest
        )
        monkeypatch.setattr(
            "app.services.run_service.run_for_job", AsyncMock(return_value=None)
        )

        await results._apply_uniprot_download(_result(staged_file))

        facts = ingest.await_args.kwargs["facts"]
        assert facts["uniprot_query"] == "proteome:UP000002311 AND reviewed:true"
        assert facts["uniprot_release"] == "2026_02"
        assert facts["uniprot_proteome"] == "UP000002311"
        assert facts["uniprot_reviewed_only"] is True
        assert facts["uniprot_protein_count"] == 6067

    async def test_a_result_with_nothing_staged_is_a_no_op(self, monkeypatch):
        ingest = AsyncMock()
        monkeypatch.setattr(
            "app.services.object_service.ingest_local_file", ingest
        )

        await results._apply_uniprot_download(
            {"staged": [], "project_id": PROJECT_ID}
        )

        assert ingest.await_count == 0

    async def test_an_ingest_failure_does_not_raise(self, staged_file, monkeypatch):
        """The transfer already succeeded; losing the job over a write-back
        failure would discard the expensive part."""
        monkeypatch.setattr(
            "app.services.object_service.ingest_local_file",
            AsyncMock(side_effect=RuntimeError("disk gone")),
        )

        await results._apply_uniprot_download(_result(staged_file))


class TestRegistration:
    def test_the_applier_is_wired_to_the_handler(self):
        """A handler with no applier downloads a file and silently drops it."""
        assert results._APPLIERS["download_uniprot"] is results._apply_uniprot_download
