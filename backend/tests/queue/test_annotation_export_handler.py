"""The export handler and the line numbers it depends on."""

from pathlib import Path

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
