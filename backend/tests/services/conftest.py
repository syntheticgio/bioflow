"""Shared fixtures for backend/tests/services/.

Thin wrappers around helpers.py's make_project/make_object factories (the
existing factory pattern in this directory -- module-level async functions,
not pytest fixtures), exposed as pytest fixtures for tests that want a
DE-results or VCF-stats-shaped object without repeating the project + object
boilerplate.
"""

from datetime import UTC, datetime

import pytest_asyncio
from app.models import Blob, BlobStorage, DataObject, ObjectRole, ObjectStatus
from beanie import PydanticObjectId

from tests.services import helpers


@pytest_asyncio.fixture
async def de_results_object_factory(beanie_models, tmp_path):
    """Build a DE_RESULTS DataObject with the given facts.

    When `gene_rows` is passed, writes a real DE results TSV (the format
    `de_runner.read_results` expects -- header `gene`, then the numeric
    columns) to a temp file and registers it as an EXTERNAL blob, so
    `launch_de_summary`'s `de_runner.read_results(...)` call resolves and
    parses a real file rather than hitting a missing-file error unrelated to
    the feature under test.
    """

    async def make(*, facts: dict | None = None, gene_rows: list[dict] | None = None):
        project = await helpers.make_project(f"de-proj-{PydanticObjectId()}")

        digest = f"{abs(hash(str(PydanticObjectId()))):064x}"[:64]
        external_path = None
        if gene_rows:
            columns = [
                "gene",
                "base_mean",
                "log2_fold_change",
                "lfc_std_error",
                "stat",
                "p_value",
                "padj",
            ]
            path = tmp_path / f"{digest}.tsv"
            lines = ["\t".join(columns)]
            for row in gene_rows:
                lines.append(
                    "\t".join(
                        "" if row.get(c) is None else str(row.get(c, ""))
                        for c in columns
                    )
                )
            path.write_text("\n".join(lines) + "\n")
            external_path = str(path)

        blob = Blob(
            id=digest,
            size=100,
            ref_count=1,
            storage=BlobStorage.EXTERNAL if external_path else BlobStorage.MANAGED,
            external_path=external_path,
            created_at=datetime.now(UTC),
        )
        await blob.insert()

        obj = DataObject(
            project_id=project.id,
            owner=project.owner,
            name="results.tsv",
            size=100,
            blob_sha256=digest,
            status=ObjectStatus.READY,
            role=ObjectRole.DE_RESULTS,
            facts=facts or {},
        )
        await obj.insert()
        return obj

    return make


@pytest_asyncio.fixture
async def vcf_stats_object_factory(beanie_models):
    """Build a DataObject carrying VCF call-set stats facts.

    launch_variant_summary does not check `obj.role`, so this factory leaves
    it unset -- matching how VCF-stats facts are actually attached in
    production (to the BAM/VCF the stats were computed from).
    """

    async def make(*, facts: dict | None = None):
        project = await helpers.make_project(f"vcf-proj-{PydanticObjectId()}")
        digest = f"{abs(hash(str(PydanticObjectId()))):064x}"[:64]
        await Blob(
            id=digest,
            size=100,
            ref_count=1,
            storage=BlobStorage.MANAGED,
            created_at=datetime.now(UTC),
        ).insert()
        obj = DataObject(
            project_id=project.id,
            owner=project.owner,
            name="calls.vcf.stats",
            size=100,
            blob_sha256=digest,
            status=ObjectStatus.READY,
            facts=facts or {},
        )
        await obj.insert()
        return obj

    return make
