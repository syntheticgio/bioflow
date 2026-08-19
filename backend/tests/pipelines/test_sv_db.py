from pathlib import Path

from app.pipelines import sv_db

DEL = (
    "chr1\t1000\tSniffles2.DEL.1\tN\t<DEL>\t60\tPASS\t"
    "SVTYPE=DEL;SVLEN=-4823;END=5823;SUPPORT=17\tGT:DR:DV\t0/1:12:17"
)
INS = (
    "chr1\t8000\tSniffles2.INS.1\tN\t<INS>\t50\tPASS\t"
    "SVTYPE=INS;SVLEN=312;END=8000;SUPPORT=9\tGT:DR:DV\t1/1:0:9"
)
BND = (
    "chr2\t5000\tSniffles2.BND.1\tN\tN[chr7:900[\t40\tPASS\t"
    "SVTYPE=BND;MATEID=Sniffles2.BND.2;SUPPORT=6\tGT:DR:DV\t0/1:8:6"
)


def test_deletion_length_is_stored_as_a_magnitude():
    """SVLEN is negative for deletions; a length is not negative.

    The sign is redundant with SVTYPE and would make every length filter and
    the histogram wrong -- a -4823 bp deletion sorts below a 50 bp insertion
    and lands in no positive bin.
    """
    rec = sv_db.parse_sv_record(DEL)
    assert rec.svtype == "DEL"
    assert rec.svlen == 4823
    assert rec.end == 5823


def test_deletion_span_is_not_a_point_event():
    """The failure this whole design exists to prevent.

    Run through the small-variant path, this record is a 1 bp event at
    POS with `<DEL>` as its ALT string. END is what makes it a span.
    """
    rec = sv_db.parse_sv_record(DEL)
    assert rec.end - rec.pos == 4823


def test_insertion_length_comes_from_svlen_not_from_end():
    """An insertion's END equals its POS -- the inserted bases are not in
    the reference. Deriving length from END would report every insertion as
    zero-length."""
    rec = sv_db.parse_sv_record(INS)
    assert rec.svtype == "INS"
    assert rec.svlen == 312
    assert rec.end == rec.pos


def test_breakend_carries_its_mate_and_has_no_length():
    """A translocation joins two loci; it has no span on either."""
    rec = sv_db.parse_sv_record(BND)
    assert rec.svtype == "BND"
    assert rec.mate == "Sniffles2.BND.2"
    assert rec.svlen is None


def test_support_is_parsed():
    assert sv_db.parse_sv_record(DEL).support == 17


def test_malformed_line_is_skipped_not_raised():
    assert sv_db.parse_sv_record("not\ta\tvcf\tline") is None


def test_build_and_query_round_trip(tmp_path: Path):
    db = tmp_path / "sv.sqlite"
    inserted = sv_db.build_sv_db(rows=iter([DEL, INS, BND]), db_path=db)
    assert inserted == 3
    assert sv_db.count_svs(db, sv_db.SvFilters()) == 3


def test_svtype_filter_selects_one_type(tmp_path: Path):
    db = tmp_path / "sv.sqlite"
    sv_db.build_sv_db(rows=iter([DEL, INS, BND]), db_path=db)
    rows = sv_db.query_svs(db, sv_db.SvFilters(svtype="DEL"), limit=10, offset=0)
    assert [r["svtype"] for r in rows] == ["DEL"]


def test_length_filter_uses_magnitude(tmp_path: Path):
    """A 4823 bp deletion is longer than a 312 bp insertion."""
    db = tmp_path / "sv.sqlite"
    sv_db.build_sv_db(rows=iter([DEL, INS, BND]), db_path=db)
    rows = sv_db.query_svs(
        db, sv_db.SvFilters(min_length=1000), limit=10, offset=0
    )
    assert [r["svtype"] for r in rows] == ["DEL"]


def test_type_counts(tmp_path: Path):
    db = tmp_path / "sv.sqlite"
    sv_db.build_sv_db(rows=iter([DEL, INS, BND]), db_path=db)
    assert sv_db.type_counts(db) == {"DEL": 1, "INS": 1, "BND": 1}


def test_length_histogram_bins_logarithmically(tmp_path: Path):
    """SV sizes span five orders of magnitude; linear bins would put
    nearly every call in the first bar."""
    db = tmp_path / "sv.sqlite"
    sv_db.build_sv_db(rows=iter([DEL, INS, BND]), db_path=db)
    hist = sv_db.length_histogram(db)
    by_label = {b["label"]: b["count"] for b in hist}
    # 312 bp -> the 100 bp bin; 4823 bp -> the 1 kb bin.
    assert by_label["100 bp"] == 1
    assert by_label["1 kb"] == 1
    # Every bin is present even when empty, so the chart has a stable axis.
    assert len(hist) == len(sv_db.LENGTH_BINS)


def test_breakends_are_absent_from_the_length_histogram(tmp_path: Path):
    """A BND has no length -- counting it as zero would invent a bar."""
    db = tmp_path / "sv.sqlite"
    sv_db.build_sv_db(rows=iter([BND]), db_path=db)
    assert sum(b["count"] for b in sv_db.length_histogram(db)) == 0
