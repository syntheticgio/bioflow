"""The export handler and the line numbers it depends on."""

from pathlib import Path

import pytest

from app.errors import PermanentError
from app.pipelines.annotation_db import FeatureFilters, query_features
from app.queue.annotation_handlers import _line_rows
from app.pipelines import annotation_parse


class _Ctx:
    def check_cancel(self):
        return None


class _Acc:
    def __init__(self):
        self.rows = []

    def add(self, f):
        self.rows.append(f)

    def add_malformed(self):
        return None

    def add_attribute_keys(self, keys):
        return None


class TestLineNumbersFromFile:
    def test_line_numbers_count_comments_and_blanks(self, tmp_path):
        """The number addresses the file, not the features in it -- so a
        comment header does not shift every feature's recorded line."""
        src = tmp_path / "a.gff3"
        src.write_text(
            "##gff-version 3\n"
            "# a comment\n"
            "\n"
            "chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n"
            "chr1\tX\tgene\t200\t300\t.\t+\t.\tID=g2\n"
        )
        acc = _Acc()
        rows = list(
            _line_rows(
                annotation_parse.parse_gff_line, src, _Ctx(), acc, [], "gff"
            )
        )
        assert [r.line_no for r in rows] == [4, 5]


class TestSourceHashRecorded:
    def test_facts_carry_the_source_hash(self, tmp_path, monkeypatch):
        """Export compares this against the object's current blob_sha256.
        Without it, a rebuilt index over a replaced file exports a subset
        that silently mixes two versions."""
        from app.config import settings
        from app.queue import annotation_handlers

        src = tmp_path / "a.gff3"
        src.write_text("chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n")
        monkeypatch.setattr(
            settings.__class__, "annotation_stats_dir",
            property(lambda self: tmp_path / "stats"),
        )

        class Ctx:
            payload = {
                "object_id": "507f1f77bcf86cd799439011",
                "format_kind": "gff",
                "annotation_path": str(src),
                "annotation_sha256": "abc123",
                "contig_lengths": [],
            }

            def check_cancel(self):
                return None

            def progress(self, **kw):
                return None

        out = annotation_handlers.run_annotation_stats(Ctx())
        assert out["facts"]["annotation_source_sha256"] == "abc123"


def _payload(tmp_path, src, db, **over):
    base = {
        "object_id": "507f1f77bcf86cd799439011",
        "project_id": "507f1f77bcf86cd799439012",
        "annotation_path": str(src),
        "db_path": str(db),
        "source_sha256": "abc123",
        "recorded_sha256": "abc123",
        "source_name": "a.gff3",
        "filters": {"feature_type": "gene"},
        "out_dir": str(tmp_path / "out"),
        "format_kind": "gff",
    }
    base.update(over)
    return base


class _ExportCtx:
    def __init__(self, payload):
        self.payload = payload

    def check_cancel(self):
        return None

    def progress(self, **kw):
        return None


@pytest.fixture
def indexed(tmp_path):
    from app.pipelines.annotation_db import build_annotation_db
    from app.pipelines.annotation_hierarchy import resolve_hierarchy
    from app.pipelines import annotation_parse

    src = tmp_path / "a.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n"
        "chr1\tX\texon\t1\t50\t.\t+\t.\tID=e1;Parent=g1\n"
    )
    rows = []
    with open(src) as fh:
        for i, line in enumerate(fh, start=1):
            s = line.rstrip("\n")
            if s and not s.startswith("#"):
                rows.append(annotation_parse.parse_gff_line(s, i))
    db = tmp_path / "f.db"
    build_annotation_db(rows=rows, db_path=db)
    resolve_hierarchy(db_path=db)
    return src, db


class TestExportHandler:
    def test_writes_a_file_and_reports_counts(self, tmp_path, indexed):
        from app.queue.annotation_handlers import run_annotation_subset_export

        src, db = indexed
        out = run_annotation_subset_export(
            _ExportCtx(_payload(tmp_path, src, db))
        )
        assert out["counts"]["matched"] == 1
        # The gene matched; its exon came in through the closure.
        assert out["counts"]["exported"] == 2
        assert Path(out["output"]["tmp_path"]).exists()

    def test_rejects_a_changed_source(self, tmp_path, indexed):
        """The stale-index case per-line verification cannot catch."""
        src, db = indexed
        from app.queue.annotation_handlers import run_annotation_subset_export

        with pytest.raises(PermanentError) as e:
            run_annotation_subset_export(
                _ExportCtx(
                    _payload(tmp_path, src, db, source_sha256="different")
                )
            )
        assert "recompute" in str(e.value).lower()

    def test_proceeds_without_a_recorded_hash(self, tmp_path, indexed):
        src, db = indexed
        from app.queue.annotation_handlers import run_annotation_subset_export

        out = run_annotation_subset_export(
            _ExportCtx(_payload(tmp_path, src, db, recorded_sha256=None))
        )
        assert out["facts"]["annotation_subset_source_verified"] is False

    def test_records_verified_when_hashes_agree(self, tmp_path, indexed):
        src, db = indexed
        from app.queue.annotation_handlers import run_annotation_subset_export

        out = run_annotation_subset_export(
            _ExportCtx(_payload(tmp_path, src, db))
        )
        assert out["facts"]["annotation_subset_source_verified"] is True

    def test_rejects_genbank(self, tmp_path, indexed):
        src, db = indexed
        from app.queue.annotation_handlers import run_annotation_subset_export

        with pytest.raises(PermanentError):
            run_annotation_subset_export(
                _ExportCtx(
                    _payload(tmp_path, src, db, format_kind="genbank")
                )
            )
