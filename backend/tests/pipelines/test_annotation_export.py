"""Subset export: which lines are selected, and whether they can be trusted.

Separate from annotation_db because this is the only place the parent/child
closure lives, and from annotation_hierarchy because that resolves status
for the whole file while this walks one filtered set.
"""

from pathlib import Path

import pytest

from app.pipelines.annotation_db import (
    FeatureFilters,
    build_annotation_db,
)
from app.pipelines.annotation_export import (
    ExportMismatch,
    closure_lines,
    subset_name,
    write_subset,
)
from app.pipelines.annotation_hierarchy import resolve_hierarchy
from app.pipelines.annotation_parse import Feature


def _f(feature_id, parent, line_no, type="gene", start=1, end=100):
    return Feature(
        contig="chr1", start=start, end=end, type=type, strand="+",
        score=None, name=feature_id, feature_id=feature_id,
        parents=(parent,) if parent else (), biotype=None,
        attributes=f"ID={feature_id}", line_no=line_no,
    )


@pytest.fixture
def gene_tree(tmp_path):
    """gene g1 -> mRNA t1 -> exons e1,e2; plus an unrelated gene g2."""
    db = tmp_path / "f.db"
    build_annotation_db(
        rows=[
            _f("g1", None, 1),
            _f("t1", "g1", 2, type="mRNA"),
            _f("e1", "t1", 3, type="exon"),
            _f("e2", "t1", 4, type="exon"),
            _f("g2", None, 5),
        ],
        db_path=db,
    )
    resolve_hierarchy(db_path=db)
    return db


class TestClosure:
    def test_includes_matched_features(self, gene_tree):
        lines = closure_lines(
            db_path=gene_tree,
            filters=FeatureFilters(feature_type="exon", top_level_only=False),
        )
        assert {3, 4} <= lines

    def test_includes_ancestors(self, gene_tree):
        """A Parent= naming a feature absent from the file makes the export
        fail in downstream tools."""
        lines = closure_lines(
            db_path=gene_tree,
            filters=FeatureFilters(feature_type="exon", top_level_only=False),
        )
        assert {1, 2} <= lines

    def test_includes_descendants(self, gene_tree):
        """A gene exported without its transcripts is valid and useless."""
        lines = closure_lines(
            db_path=gene_tree,
            filters=FeatureFilters(name_query="g1", top_level_only=False),
        )
        assert lines == {1, 2, 3, 4}

    def test_excludes_unrelated_features(self, gene_tree):
        lines = closure_lines(
            db_path=gene_tree,
            filters=FeatureFilters(name_query="g1", top_level_only=False),
        )
        assert 5 not in lines

    def test_terminates_on_a_cycle(self, tmp_path):
        """A malformed file whose parents form a loop must not hang."""
        db = tmp_path / "cycle.db"
        build_annotation_db(
            rows=[_f("a", "b", 1), _f("b", "a", 2)], db_path=db
        )
        resolve_hierarchy(db_path=db)
        lines = closure_lines(
            db_path=db,
            filters=FeatureFilters(name_query="a", top_level_only=False),
        )
        assert lines == {1, 2}

    def test_features_without_a_line_are_skipped(self, tmp_path):
        """GenBank rows carry no line number and cannot be re-emitted."""
        db = tmp_path / "nl.db"
        build_annotation_db(rows=[_f("g1", None, None)], db_path=db)
        resolve_hierarchy(db_path=db)
        lines = closure_lines(
            db_path=db, filters=FeatureFilters(top_level_only=False)
        )
        assert lines == set()


class TestWriteSubset:
    def test_emits_selected_lines_verbatim(self, tmp_path):
        src = tmp_path / "a.gff3"
        src.write_text(
            "##gff-version 3\n"
            "chr1\tHAVANA\tgene\t1\t100\t.\t+\t.\tID=g1\n"
            "chr1\tHAVANA\tCDS\t1\t100\t.\t+\t2\tID=c1;Parent=g1\n"
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines={3}, verify=None)
        text = out.read_text()
        assert "chr1\tHAVANA\tCDS\t1\t100\t.\t+\t2\tID=c1;Parent=g1" in text

    def test_preserves_the_phase_column(self, tmp_path):
        """The reason for re-emitting rather than reconstructing: `phase`
        is not stored on Feature at all."""
        src = tmp_path / "a.gff3"
        src.write_text("chr1\tX\tCDS\t1\t100\t.\t+\t2\tID=c1\n")
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines={1}, verify=None)
        assert out.read_text().split("\t")[7] == "2"

    def test_copies_the_header(self, tmp_path):
        """A GFF3 without ##gff-version 3 is malformed, and headers are not
        features so the closure never selects them."""
        src = tmp_path / "a.gff3"
        src.write_text(
            "##gff-version 3\n"
            "##sequence-region chr1 1 1000\n"
            "chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n"
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines={3}, verify=None)
        lines = out.read_text().splitlines()
        assert lines[0] == "##gff-version 3"
        assert lines[1] == "##sequence-region chr1 1 1000"

    def test_header_is_not_truncated_at_the_display_cap(self, tmp_path):
        """_HEADER_SCAN_LINES bounds what is *displayed*; reusing it here
        would silently drop a long ##sequence-region header."""
        src = tmp_path / "a.gff3"
        header = "".join(f"##sequence-region c{i} 1 10\n" for i in range(80))
        src.write_text(header + "chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n")
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines={81}, verify=None)
        assert out.read_text().count("##sequence-region") == 80

    def test_emits_in_file_order(self, tmp_path):
        src = tmp_path / "a.gff3"
        src.write_text(
            "chr1\tX\tgene\t1\t10\t.\t+\t.\tID=a\n"
            "chr1\tX\tgene\t2\t20\t.\t+\t.\tID=b\n"
            "chr1\tX\tgene\t3\t30\t.\t+\t.\tID=c\n"
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines={3, 1}, verify=None)
        ids = [line.split("ID=")[1] for line in out.read_text().splitlines()]
        assert ids == ["a", "c"]

    def test_rejects_a_line_that_no_longer_matches(self, tmp_path):
        """A wrong-but-plausible annotation file is worse than no file."""
        src = tmp_path / "a.gff3"
        src.write_text("chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n")
        out = tmp_path / "out.gff3"
        with pytest.raises(ExportMismatch):
            write_subset(
                source=src, dest=out, lines={1},
                verify={1: {"contig": "chr1", "start": 999, "end": 100}},
            )

    def test_accepts_a_line_that_matches(self, tmp_path):
        src = tmp_path / "a.gff3"
        src.write_text("chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n")
        out = tmp_path / "out.gff3"
        write_subset(
            source=src, dest=out, lines={1},
            verify={1: {"contig": "chr1", "start": 1, "end": 100}},
        )
        assert "ID=g1" in out.read_text()

    def test_verifies_gtf_lines_with_the_gtf_parser(self, tmp_path):
        """write_subset must not hardcode the GFF3 parser for verification --
        a GTF export re-parsed as GFF3 would spuriously fail on every line,
        since the two formats structure their 9th column differently."""
        from app.pipelines.annotation_parse import parse_gtf_line

        src = tmp_path / "a.gtf"
        src.write_text('chr1\tX\tgene\t1\t100\t.\t+\t.\tgene_id "g1";\n')
        out = tmp_path / "out.gtf"
        write_subset(
            source=src, dest=out, lines={1},
            verify={1: {"contig": "chr1", "start": 1, "end": 100}},
            parse_line=parse_gtf_line,
        )
        assert 'gene_id "g1"' in out.read_text()

    def test_calls_check_cancel_periodically(self, tmp_path):
        """A user who cancels an in-flight export must be able to stop it
        before a multi-million-line file finishes its single sequential
        pass -- matching the checkpoint _line_rows already has."""
        src = tmp_path / "a.gff3"
        # 250_001 lines so the loop crosses the 100_000 and 200_000 marks.
        src.write_text(
            "".join(f"chr1\tX\tgene\t{i}\t{i}\t.\t+\t.\tID=g{i}\n" for i in range(1, 250_002))
        )
        out = tmp_path / "out.gff3"
        calls = []
        write_subset(
            source=src, dest=out, lines={1}, verify=None,
            check_cancel=lambda: calls.append(1),
        )
        assert len(calls) == 2

    def test_check_cancel_is_optional(self, tmp_path):
        """Every existing caller that doesn't pass check_cancel must keep
        working unchanged."""
        src = tmp_path / "a.gff3"
        src.write_text("chr1\tX\tgene\t1\t100\t.\t+\t.\tID=g1\n")
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines={1}, verify=None)
        assert "ID=g1" in out.read_text()


class TestSubsetName:
    def test_uses_a_single_filter(self):
        assert subset_name("GRCh38.gff3", {"contig": "chr21"}) == "GRCh38.chr21.gff3"

    def test_uses_two_filters(self):
        name = subset_name("GRCh38.gff3", {"contig": "chr21", "feature_type": "exon"})
        assert name == "GRCh38.chr21.exon.gff3"

    def test_falls_back_past_two(self):
        """Four active filters make an unreadable name; facts carry the
        complete filter regardless."""
        name = subset_name(
            "GRCh38.gff3",
            {"contig": "chr21", "feature_type": "exon",
             "strand": "+", "biotype": "protein_coding"},
        )
        assert name == "GRCh38.subset.gff3"

    def test_handles_a_compound_extension(self):
        assert subset_name("GRCh38.gff3.gz", {"contig": "chr21"}) == (
            "GRCh38.chr21.gff3.gz"
        )


FIXTURES = Path(__file__).parent.parent / "fixtures" / "annotation"


class TestRealNcbiFidelity:
    """Against a real NCBI GFF3, not hand-built Feature objects.

    Hand-built fixtures feed the code objects that already look the way it
    expects -- how the suggestion-rules and STAR failures passed a green
    suite while being wrong. The columns asserted here (`source`, `phase`)
    are exactly the ones Feature does not store, so a reconstruction-based
    implementation passes every other test in this file and fails these.
    """

    def _index(self, tmp_path):
        from app.pipelines import annotation_parse

        src = FIXTURES / "ncbi_slice.gff3"
        rows = []
        with open(src) as fh:
            for i, line in enumerate(fh, start=1):
                stripped = line.rstrip("\n")
                if not stripped or stripped.startswith("#"):
                    continue
                f = annotation_parse.parse_gff_line(stripped, i)
                if f is not None:
                    rows.append(f)
        db = tmp_path / "real.db"
        build_annotation_db(rows=rows, db_path=db)
        resolve_hierarchy(db_path=db)
        return src, db

    def test_exported_lines_are_byte_identical(self, tmp_path):
        src, db = self._index(tmp_path)
        lines = closure_lines(
            db_path=db,
            filters=FeatureFilters(feature_type="CDS", top_level_only=False),
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines=lines, verify=None)

        source_lines = src.read_text().splitlines()
        for emitted in out.read_text().splitlines():
            if emitted.startswith("#"):
                continue
            assert emitted in source_lines

    def test_phase_survives(self, tmp_path):
        """The column Feature has no field for."""
        src, db = self._index(tmp_path)
        lines = closure_lines(
            db_path=db,
            filters=FeatureFilters(feature_type="CDS", top_level_only=False),
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines=lines, verify=None)
        cds = [
            line.split("\t") for line in out.read_text().splitlines()
            if not line.startswith("#") and line.split("\t")[2] == "CDS"
        ]
        assert {row[7] for row in cds} == {"0", "2"}

    def test_source_column_survives(self, tmp_path):
        """Column 2, also absent from Feature."""
        src, db = self._index(tmp_path)
        lines = closure_lines(
            db_path=db,
            filters=FeatureFilters(feature_type="CDS", top_level_only=False),
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines=lines, verify=None)
        for line in out.read_text().splitlines():
            if line.startswith("#"):
                continue
            assert line.split("\t")[1] in ("RefSeq", "BestRefSeq", "Gnomon")

    def test_no_dangling_parent_references(self, tmp_path):
        """AE-13, against a real hierarchy."""
        src, db = self._index(tmp_path)
        lines = closure_lines(
            db_path=db,
            filters=FeatureFilters(feature_type="CDS", top_level_only=False),
        )
        out = tmp_path / "out.gff3"
        write_subset(source=src, dest=out, lines=lines, verify=None)

        from app.pipelines.annotation_parse import parse_gff_attributes

        present, referenced = set(), set()
        for line in out.read_text().splitlines():
            if line.startswith("#"):
                continue
            attrs = parse_gff_attributes(line.split("\t")[8])
            if attrs.get("ID"):
                present.add(attrs["ID"])
            for parent in attrs.get("Parent", "").split(","):
                if parent:
                    referenced.add(parent)
        assert referenced <= present
