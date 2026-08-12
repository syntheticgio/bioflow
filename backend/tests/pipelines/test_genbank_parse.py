"""Parsing GenBank locations and qualifiers.

Kept free of I/O so every case is a plain function call: this is where the
format edge cases live, and they are the part most likely to be wrong.
"""

from app.pipelines.genbank_parse import Location, parse_location


class TestSimpleLocations:
    def test_plain_range(self):
        loc = parse_location("100..200")
        assert loc == Location(segments=[(100, 200)], strand="+", fuzzy=False)

    def test_single_position(self):
        # A feature at one base: start and end are the same.
        loc = parse_location("467")
        assert loc.segments == [(467, 467)]

    def test_between_positions(self):
        # `102^103` marks a site *between* two bases, used for insertion
        # points. Stored as the left base so it lands somewhere real on the
        # locus chart rather than being dropped.
        loc = parse_location("102^103")
        assert loc.segments == [(102, 102)]

    def test_complement_flips_strand(self):
        loc = parse_location("complement(100..200)")
        assert loc.segments == [(100, 200)]
        assert loc.strand == "-"


class TestFuzzyBounds:
    def test_fuzzy_start(self):
        # `<1` means "starts at or before 1" -- a partial feature running off
        # the contig edge. The bound is used as given and the flag records
        # that it is approximate.
        loc = parse_location("<1..200")
        assert loc.segments == [(1, 200)]
        assert loc.fuzzy is True

    def test_fuzzy_end(self):
        loc = parse_location("100..>200")
        assert loc.segments == [(100, 200)]
        assert loc.fuzzy is True


class TestJoinLocations:
    def test_join_keeps_segments_separate(self):
        # The constraint from #294: this must NOT become (100, 500).
        loc = parse_location("join(100..200,400..500)")
        assert loc.segments == [(100, 200), (400, 500)]
        assert loc.strand == "+"

    def test_join_inside_complement(self):
        # complement(join(...)) -- the whole feature is on the minus strand.
        loc = parse_location("complement(join(100..200,400..500))")
        assert loc.segments == [(100, 200), (400, 500)]
        assert loc.strand == "-"

    def test_complement_inside_join(self):
        # join(complement(...),...) -- mixed strands within one feature.
        # A single strand column cannot express that, so the feature takes
        # the strand of its first segment and every segment is preserved.
        loc = parse_location("join(complement(100..200),400..500)")
        assert loc.segments == [(100, 200), (400, 500)]
        assert loc.strand == "-"

    def test_order_behaves_like_join(self):
        # `order` means the segments are not known to be contiguous. For a
        # feature table that distinction has no representation, and treating
        # it as `join` keeps every segment rather than dropping the feature.
        loc = parse_location("order(100..200,400..500)")
        assert loc.segments == [(100, 200), (400, 500)]


class TestMalformedLocations:
    def test_empty_returns_none(self):
        assert parse_location("") is None

    def test_garbage_returns_none(self):
        # Never raises: an unrecognized grammar must not abort a whole file.
        assert parse_location("not-a-location") is None

    def test_remote_reference_returns_none(self):
        # `J00194.1:100..200` points into another record entirely. There is
        # no contig in this file to attach it to, so it is skipped.
        assert parse_location("J00194.1:100..200") is None

    def test_reversed_bounds_returns_none(self):
        assert parse_location("500..100") is None
