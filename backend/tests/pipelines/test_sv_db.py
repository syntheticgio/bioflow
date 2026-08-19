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


def test_sub_minimum_length_still_lands_in_the_smallest_bin(tmp_path: Path):
    """A call shorter than the first bin's named floor (50bp) must still be
    counted, not silently dropped by falling through every comparison.

    Sniffles2's --minsvlen defaults to 50, but it is a configurable runner
    parameter -- a run configured lower, or any shorter call, must still show
    up in the histogram total rather than vanishing with no error.
    """
    db = tmp_path / "sv.sqlite"
    tiny = (
        "chr1\t2000\tSniffles2.DEL.2\tN\t<DEL>\t60\tPASS\t"
        "SVTYPE=DEL;SVLEN=-10;END=2010;SUPPORT=5\tGT:DR:DV\t0/1:9:5"
    )
    inserted = sv_db.build_sv_db(rows=iter([DEL, INS, BND, tiny]), db_path=db)
    assert inserted == 4

    hist = sv_db.length_histogram(db)
    by_label = {b["label"]: b["count"] for b in hist}
    assert by_label["50 bp"] == 1
    # DEL, INS, and the sub-minimum tiny record are all non-null lengths;
    # BND is the only one excluded. Nothing else should be dropped.
    assert sum(b["count"] for b in hist) == 3


def test_build_sv_db_flushes_interior_batches(tmp_path: Path):
    """The streaming build flushes every _INSERT_BATCH (10,000) rows before
    the final flush at loop end -- exercise that interior flush branch
    rather than only ever hitting the tail-batch path with small fixtures."""
    db = tmp_path / "sv.sqlite"
    row_count = 25_001

    def make_rows():
        for i in range(row_count):
            pos = 1000 + i
            yield (
                f"chr1\t{pos}\tSniffles2.INS.{i}\tN\t<INS>\t50\tPASS\t"
                f"SVTYPE=INS;SVLEN=100;END={pos};SUPPORT=5\tGT:DR:DV\t0/1:5:5"
            )

    inserted = sv_db.build_sv_db(rows=make_rows(), db_path=db)
    assert inserted == row_count
    assert sv_db.count_svs(db, sv_db.SvFilters()) == row_count
