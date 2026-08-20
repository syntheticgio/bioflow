"""API surface for Kraken2 classification: launch and database listing.

Uses the shared `client`/`two_profiles` fixtures from `conftest.py` -- the
same real-app-and-Mongo shape every `OwnerDep` route test in this package
uses. `test_classify_reads_rejects_unknown_db` doesn't need a real FASTQ
object: `launch_classify_reads` validates `db_key` against the registry
before it ever looks up the object (see `pipeline_service.py`), so a
syntactically valid but nonexistent ObjectId is enough to reach that check.
"""

import fakeredis.aioredis
import pytest
from beanie import PydanticObjectId

from app.queue import queue

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
async def fake_redis(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(queue, "get_redis", lambda: client)
    yield client
    await client.aclose()


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


async def test_classify_reads_accepts_fastq(client, two_profiles):
    from unittest.mock import patch

    from app.models import (
        Blob,
        BlobState,
        BlobStorage,
        DataObject,
        FormatInfo,
        FormatKind,
        ObjectStatus,
    )
    from app.pipelines.tools import Tool
    from app.services import project_service
    from app.storage.paths import blob_rel_path

    owner = two_profiles["a"].owner_id()
    project = await project_service.create_project(name="kraken-fq-project", owner=owner)
    sha = "a" * 64
    await Blob(
        id=sha,
        size=100,
        state=BlobState.PRESENT,
        storage=BlobStorage.MANAGED,
        rel_path=blob_rel_path(sha),
        ref_count=1,
    ).insert()
    obj = DataObject(
        project_id=project.id,
        name="sample.fastq.gz",
        owner=owner,
        format=FormatInfo(kind=FormatKind.FASTQ),
        status=ObjectStatus.READY,
        blob_sha256=sha,
    )
    await obj.insert()

    fake_tool = Tool(name="kraken2", path="/usr/bin/kraken2", version="2.1.3", error=None)
    with patch("app.pipelines.tools.kraken2", return_value=fake_tool):
        resp = await client.post(
            "/api/v1/pipelines/classify-reads",
            json={"object_id": str(obj.id), "db_key": "standard-8", "resource_override": True},
            headers=two_profiles["a_headers"],
        )
    assert resp.status_code == 201
    job = resp.json()
    assert job["payload"]["format"] == "fastq"
    assert job["payload"]["reads_name"] == "sample.fastq.gz"


async def test_classify_reads_accepts_fasta_contigs(client, two_profiles):
    from unittest.mock import patch

    from app.models import (
        Blob,
        BlobState,
        BlobStorage,
        DataObject,
        FormatInfo,
        FormatKind,
        ObjectStatus,
    )
    from app.pipelines.tools import Tool
    from app.services import project_service
    from app.storage.paths import blob_rel_path

    owner = two_profiles["a"].owner_id()
    project = await project_service.create_project(name="kraken-fa-project", owner=owner)
    sha = "b" * 64
    await Blob(
        id=sha,
        size=100,
        state=BlobState.PRESENT,
        storage=BlobStorage.MANAGED,
        rel_path=blob_rel_path(sha),
        ref_count=1,
    ).insert()
    obj = DataObject(
        project_id=project.id,
        name="bin.001.fasta",
        owner=owner,
        format=FormatInfo(kind=FormatKind.FASTA),
        status=ObjectStatus.READY,
        blob_sha256=sha,
    )
    await obj.insert()

    fake_tool = Tool(name="kraken2", path="/usr/bin/kraken2", version="2.1.3", error=None)
    with patch("app.pipelines.tools.kraken2", return_value=fake_tool):
        resp = await client.post(
            "/api/v1/pipelines/classify-reads",
            json={"object_id": str(obj.id), "db_key": "standard-8", "resource_override": True},
            headers=two_profiles["a_headers"],
        )
    assert resp.status_code == 201
    job = resp.json()
    assert job["payload"]["format"] == "fasta"
    assert "mate_sha256" not in job["payload"]


async def test_classify_reads_refuses_non_sequence_format(client, two_profiles):
    from unittest.mock import patch

    from app.models import DataObject, FormatInfo, FormatKind, ObjectStatus
    from app.pipelines.tools import Tool
    from app.services import project_service

    owner = two_profiles["a"].owner_id()
    project = await project_service.create_project(name="kraken-bam-project", owner=owner)
    obj = DataObject(
        project_id=project.id,
        name="alignments.bam",
        owner=owner,
        format=FormatInfo(kind=FormatKind.BAM),
        status=ObjectStatus.READY,
        sha256="111" * 20 + "2222",
    )
    await obj.insert()

    fake_tool = Tool(name="kraken2", path="/usr/bin/kraken2", version="2.1.3", error=None)
    with patch("app.pipelines.tools.kraken2", return_value=fake_tool):
        resp = await client.post(
            "/api/v1/pipelines/classify-reads",
            json={"object_id": str(obj.id), "db_key": "standard-8", "resource_override": True},
            headers=two_profiles["a_headers"],
        )
    assert resp.status_code == 422
    assert "not FASTQ reads or FASTA contigs" in resp.json()["message"]


async def test_classify_reads_refuses_protein_fasta(client, two_profiles):
    from unittest.mock import patch

    from app.models import DataObject, FormatInfo, FormatKind, ObjectRole, ObjectStatus
    from app.pipelines.tools import Tool
    from app.services import project_service

    owner = two_profiles["a"].owner_id()
    project = await project_service.create_project(name="kraken-prot-project", owner=owner)
    obj = DataObject(
        project_id=project.id,
        name="proteins.faa",
        owner=owner,
        format=FormatInfo(kind=FormatKind.FASTA),
        role=ObjectRole.PROTEIN,
        status=ObjectStatus.READY,
        sha256="333" * 20 + "4444",
    )
    await obj.insert()

    fake_tool = Tool(name="kraken2", path="/usr/bin/kraken2", version="2.1.3", error=None)
    with patch("app.pipelines.tools.kraken2", return_value=fake_tool):
        resp = await client.post(
            "/api/v1/pipelines/classify-reads",
            json={"object_id": str(obj.id), "db_key": "standard-8", "resource_override": True},
            headers=two_profiles["a_headers"],
        )
    assert resp.status_code == 422
    assert "not a nucleotide sequence" in resp.json()["message"]

