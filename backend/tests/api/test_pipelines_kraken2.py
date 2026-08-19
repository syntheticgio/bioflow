"""API surface for Kraken2 classification: launch and database listing.

Uses the shared `client`/`two_profiles` fixtures from `conftest.py` -- the
same real-app-and-Mongo shape every `OwnerDep` route test in this package
uses. `test_classify_reads_rejects_unknown_db` doesn't need a real FASTQ
object: `launch_classify_reads` validates `db_key` against the registry
before it ever looks up the object (see `pipeline_service.py`), so a
syntactically valid but nonexistent ObjectId is enough to reach that check.
"""

import pytest
from beanie import PydanticObjectId

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def test_kraken_dbs_lists_registry_with_presence(client, tmp_path, monkeypatch):
    """`kraken_dbs_dir` is `BIOINFO_HOME`-relative shared storage, not test-
    scoped -- it's the same directory the main and every worktree preview
    stack write real, multi-GB downloads into (deliberately shared, see
    CLAUDE.md). A real download landing there between test runs makes
    `present` genuinely True, which is not what this test means to assert;
    it means to assert the registry's *shape*. Point `bioinfo_home` at an
    empty tmp_path, the same isolation `test_kraken_db_registry.py`'s own
    `db_present` test uses, so this test's meaning doesn't depend on
    whether anyone has downloaded a database on this machine."""
    from app.config import settings

    monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")

    resp = await client.get("/api/v1/pipelines/kraken-dbs")
    assert resp.status_code == 200
    rows = resp.json()
    assert {r["key"] for r in rows} == {"standard-8", "pluspf-8", "viral"}
    for r in rows:
        assert set(r) >= {"key", "label", "description", "download_bytes", "present"}
        assert r["present"] is False  # isolated tmp_path, never downloaded into


async def test_classify_reads_rejects_unknown_db(client, two_profiles):
    resp = await client.post(
        "/api/v1/pipelines/classify-reads",
        json={"object_id": str(PydanticObjectId()), "db_key": "nonsense"},
        headers=two_profiles["a_headers"],
    )
    assert resp.status_code == 422
