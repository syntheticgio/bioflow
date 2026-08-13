"""The export_annotation_subset handler."""

import dataclasses
import uuid
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId

from app.config import settings
from app.errors import PermanentError
from app.models import ObjectRole
from app.pipelines import annotation_db, annotation_hierarchy, annotation_parse
from app.queue import results
from app.queue.annotation_handlers import export_annotation_subset
from app.services import object_service, project_service, run_service


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    """Keep _prepare_workdir out of the real tmp/ tree.

    settings.tmp_dir is a read-only property derived from bioinfo_home (see
    app/config.py), not a plain field, so it is bioinfo_home that gets
    patched here -- the same convention test_assembly_qc_handlers.py and
    test_tool_handlers.py already use.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
    return settings


class _Ctx:
    """The parts of JobContext this handler touches.

    `job_id` is here because _prepare_workdir derives the scratch directory
    from it.
    """

    def __init__(self, payload, job_id="test-export-job"):
        self.payload = payload
        self.job_id = job_id

    def check_cancel(self):
        return None

    def progress(self, **kw):
        return None


_SOURCE = """\
##gff-version 3
chr1\t.\tgene\t100\t900\t.\t+\t.\tID=g1
chr1\t.\texon\t100\t200\t.\t+\t0\tID=e1;Parent=g1
chr2\t.\tgene\t10\t20\t.\t-\t.\tID=g2
"""


def _setup(tmp_path):
    source = tmp_path / "in.gff3"
    source.write_text(_SOURCE)

    rows = []
    for i, raw in enumerate(source.read_text().splitlines(), start=1):
        if raw.startswith("#"):
            continue
        feature = annotation_parse.parse_gff_line(raw)
        if feature is not None:
            rows.append(dataclasses.replace(feature, line=i))

    db_path = tmp_path / "features.db"
    annotation_db.build_annotation_db(rows=rows, db_path=db_path)
    annotation_hierarchy.resolve_hierarchy(db_path=db_path)
    return source, db_path


def test_export_writes_the_closure_and_reports_counts(tmp_path):
    source, db_path = _setup(tmp_path)
    ctx = _Ctx({
        "object_id": "abc123",
        "annotation_path": str(source),
        "db_path": str(db_path),
        "format_kind": "gff",
        "filters": {"contig": "chr1"},
        "output_name": "subset.gff3",
    })

    result = export_annotation_subset(ctx)

    assert result["feature_count"] == 2      # the gene and its exon
    assert result["output"]["name"] == "subset.gff3"
    written = open(result["output"]["tmp_path"]).read()
    assert "ID=g1" in written and "ID=e1" in written
    assert "ID=g2" not in written


def test_export_refuses_genbank(tmp_path):
    """AE-25: GenBank features span several lines and record none."""
    source, db_path = _setup(tmp_path)
    ctx = _Ctx({
        "object_id": "abc123",
        "annotation_path": str(source),
        "db_path": str(db_path),
        "format_kind": "genbank",
        "filters": {},
        "output_name": "subset.gff3",
    })

    with pytest.raises(PermanentError, match="genbank"):
        export_annotation_subset(ctx)


def test_export_refuses_an_empty_match(tmp_path):
    """AE-16: an annotation with a header and no features is a file that
    silently disappoints, so refuse rather than write it."""
    source, db_path = _setup(tmp_path)
    ctx = _Ctx({
        "object_id": "abc123",
        "annotation_path": str(source),
        "db_path": str(db_path),
        "format_kind": "gff",
        "filters": {"contig": "chrZ"},
        "output_name": "subset.gff3",
    })

    with pytest.raises(PermanentError, match="no features"):
        export_annotation_subset(ctx)


def test_a_stale_index_fails_permanently(tmp_path):
    """AE-15: retrying cannot help -- the index must be recomputed first."""
    source, db_path = _setup(tmp_path)
    kept = [ln for ln in source.read_text().splitlines() if "g1" not in ln]
    source.write_text("\n".join(kept) + "\n")

    ctx = _Ctx({
        "object_id": "abc123",
        "annotation_path": str(source),
        "db_path": str(db_path),
        "format_kind": "gff",
        "filters": {"contig": "chr1"},
        "output_name": "subset.gff3",
    })

    with pytest.raises(PermanentError, match="out of date"):
        export_annotation_subset(ctx)


def test_export_applier_is_registered():
    """_APPLIERS is hand-maintained and silently skips unknown job types: a
    missing entry means the job succeeds and no object is ever created."""
    from app.queue.results import _APPLIERS

    assert "export_annotation_subset" in _APPLIERS


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
class TestApplierRecordsRunOutputs:
    """_apply_export_annotation_subset must attach the ingested subset to
    its run, matching every other ingest-creating applier in results.py
    (_apply_sra_download, _apply_uniprot_download, etc.) -- otherwise the
    export never shows up in the run/activity view even though the run
    itself completes normally.

    job_id currently never reaches result in production (a separate,
    already-tracked issue with how the executor passes results to
    appliers), so run_for_job resolving to None is the real-world case --
    but that just means run_id stays None and record_outputs is skipped by
    the existing guard. This test supplies a job_id explicitly to prove the
    wiring is correct and ready for when that gets fixed.
    """

    async def test_record_outputs_called_when_job_id_present(self, tmp_path, monkeypatch):
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        settings.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        settings.sentinel_path.write_text("biopipe-home-v1\n")
        owner = "annotation-export-run-owner"

        async def _skip_ingest(obj, **kwargs):
            return ""

        async def _skip_enqueue(*args, **kwargs):
            return None

        monkeypatch.setattr(object_service, "enqueue_ingest", _skip_ingest)
        monkeypatch.setattr("app.queue.queue.enqueue", _skip_enqueue)

        project = await project_service.create_project(
            name=f"{owner}-project", owner=owner
        )
        source_path = tmp_path / f"source-{uuid.uuid4().hex}.gff3"
        source_path.write_bytes(uuid.uuid4().bytes)
        source = await object_service.ingest_local_file(
            owner=owner,
            project_id=project.id,
            path=source_path,
            name="source.gff3",
            role=ObjectRole.ANNOTATION,
        )

        output_path = tmp_path / f"subset-{uuid.uuid4().hex}.gff3"
        output_path.write_bytes(uuid.uuid4().bytes)

        run_id = PydanticObjectId()
        run_for_job = AsyncMock(return_value=run_id)
        record_outputs = AsyncMock()
        monkeypatch.setattr(run_service, "run_for_job", run_for_job)
        monkeypatch.setattr(run_service, "record_outputs", record_outputs)

        job_id = str(PydanticObjectId())

        await results._apply_export_annotation_subset(
            {
                "object_id": str(source.id),
                "job_id": job_id,
                "output": {"tmp_path": str(output_path), "name": "subset.gff3"},
                "feature_count": 2,
            },
            owner=owner,
        )

        run_for_job.assert_awaited_once_with(PydanticObjectId(job_id))
        assert record_outputs.await_count == 1
        args, kwargs = record_outputs.await_args
        assert args[0] == run_id
        assert kwargs["owner"] == owner

    async def test_record_outputs_skipped_when_job_id_absent(self, tmp_path, monkeypatch):
        """The common case today: the executor never injects job_id, so
        run_for_job is never even called and no run is touched."""
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        settings.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        settings.sentinel_path.write_text("biopipe-home-v1\n")
        owner = "annotation-export-run-owner-2"

        async def _skip_ingest(obj, **kwargs):
            return ""

        async def _skip_enqueue(*args, **kwargs):
            return None

        monkeypatch.setattr(object_service, "enqueue_ingest", _skip_ingest)
        monkeypatch.setattr("app.queue.queue.enqueue", _skip_enqueue)

        project = await project_service.create_project(
            name=f"{owner}-project", owner=owner
        )
        source_path = tmp_path / f"source-{uuid.uuid4().hex}.gff3"
        source_path.write_bytes(uuid.uuid4().bytes)
        source = await object_service.ingest_local_file(
            owner=owner,
            project_id=project.id,
            path=source_path,
            name="source.gff3",
            role=ObjectRole.ANNOTATION,
        )

        output_path = tmp_path / f"subset-{uuid.uuid4().hex}.gff3"
        output_path.write_bytes(uuid.uuid4().bytes)

        run_for_job = AsyncMock()
        record_outputs = AsyncMock()
        monkeypatch.setattr(run_service, "run_for_job", run_for_job)
        monkeypatch.setattr(run_service, "record_outputs", record_outputs)

        await results._apply_export_annotation_subset(
            {
                "object_id": str(source.id),
                "output": {"tmp_path": str(output_path), "name": "subset.gff3"},
                "feature_count": 2,
            },
            owner=owner,
        )

        run_for_job.assert_not_awaited()
        record_outputs.assert_not_awaited()
