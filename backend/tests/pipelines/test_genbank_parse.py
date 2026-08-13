"""Parsing GenBank locations and qualifiers.

Kept free of I/O so every case is a plain function call: this is where the
format edge cases live, and they are the part most likely to be wrong.
"""

from app.pipelines.genbank_parse import Location, iter_features, parse_location, parse_qualifiers


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


class TestQualifiers:
    def test_simple_key_value(self):
        lines = ['/gene="thrA"', '/locus_tag="b0002"']
        assert parse_qualifiers(lines) == {"gene": "thrA", "locus_tag": "b0002"}

    def test_valueless_qualifier(self):
        # `/pseudo` has no value. Stored as an empty string so the key is
        # still present -- its presence is the information.
        assert parse_qualifiers(["/pseudo"]) == {"pseudo": ""}

    def test_unquoted_value(self):
        # Numeric qualifiers are conventionally unquoted.
        assert parse_qualifiers(["/codon_start=1"]) == {"codon_start": "1"}

    def test_wrapped_value_joins_with_space(self):
        # A /note wrapping across lines is one value. GenBank wraps on word
        # boundaries, so the parts join with a space.
        lines = [
            '/note="bifunctional: aspartokinase I (N-terminal);',
            'homoserine dehydrogenase I (C-terminal)"',
        ]
        assert parse_qualifiers(lines) == {
            "note": "bifunctional: aspartokinase I (N-terminal); "
                    "homoserine dehydrogenase I (C-terminal)"
        }

    def test_wrapped_translation_joins_without_space(self):
        # /translation is a protein sequence -- joining its parts with a
        # space would corrupt it. This is the one qualifier that wraps
        # mid-token rather than on word boundaries.
        lines = ['/translation="MRVLKFGGTSVAN', 'AERFLRVADILESNAR"']
        assert parse_qualifiers(lines) == {
            "translation": "MRVLKFGGTSVANAERFLRVADILESNAR"
        }

    def test_repeated_key_keeps_first(self):
        # /db_xref repeats legitimately. The dict keeps the first; the raw
        # block is preserved separately by the caller, so nothing is lost.
        lines = ['/db_xref="GeneID:1"', '/db_xref="ASAP:2"']
        assert parse_qualifiers(lines) == {"db_xref": "GeneID:1"}

    def test_ignores_malformed_line(self):
        # A line that is not a qualifier is skipped, not raised -- the same
        # posture parse_gff_attributes documents.
        assert parse_qualifiers(["junk", '/gene="thrA"']) == {"gene": "thrA"}


FEATURE_LINES = [
    "     gene            337..2799",
    '                     /gene="thrA"',
    '                     /locus_tag="b0002"',
    "     CDS             join(complement(337..600),700..2799)",
    '                     /gene="thrA"',
    '                     /product="aspartokinase"',
]


class TestIterFeatures:
    def test_simple_feature_is_one_row(self):
        rows = list(iter_features(FEATURE_LINES[:3], accession="NC_1"))
        assert len(rows) == 1
        row = rows[0]
        assert row.contig == "NC_1"
        assert (row.start, row.end) == (337, 2799)
        assert row.type == "gene"
        assert row.name == "thrA"
        assert row.parents == ()
        assert row.feature_id == "gb:NC_1:0"

    def test_join_emits_parent_then_segments(self):
        rows = list(iter_features(FEATURE_LINES[3:], accession="NC_1"))
        assert len(rows) == 3

        parent, seg1, seg2 = rows
        # The parent spans the outer bounds, honestly -- the children below
        # state the real extent, the same way a GFF3 gene spans its introns.
        assert (parent.start, parent.end) == (337, 2799)
        assert parent.type == "CDS"
        assert parent.parents == ()
        assert parent.feature_id == "gb:NC_1:0"

        assert (seg1.start, seg1.end) == (337, 600)
        assert (seg2.start, seg2.end) == (700, 2799)
        assert seg1.type == "CDS_segment"
        assert seg1.parents == ("gb:NC_1:0",)
        assert seg2.parents == ("gb:NC_1:0",)
        assert seg1.feature_id == "gb:NC_1:0:seg1"
        assert seg2.feature_id == "gb:NC_1:0:seg2"

    def test_strand_from_complement(self):
        rows = list(iter_features(FEATURE_LINES[3:], accession="NC_1"))
        assert all(r.strand == "-" for r in rows)

    def test_ids_are_unique_across_features(self):
        rows = list(iter_features(FEATURE_LINES, accession="NC_1"))
        ids = [r.feature_id for r in rows]
        assert len(ids) == len(set(ids))

    def test_name_falls_back_through_qualifiers(self):
        # /gene wins; then /locus_tag; then /product.
        lines = ["     CDS             1..9", '                     /locus_tag="b1"']
        assert list(iter_features(lines, accession="X"))[0].name == "b1"

        lines = ["     CDS             1..9", '                     /product="widget"']
        assert list(iter_features(lines, accession="X"))[0].name == "widget"

    def test_attributes_preserve_every_qualifier(self):
        # The issue's constraint: a qualifier nothing promotes to a column
        # must still survive. /product is not a column, so it has to be here.
        row = list(iter_features(FEATURE_LINES[3:], accession="NC_1"))[0]
        assert "product=aspartokinase" in row.attributes
        assert "gene=thrA" in row.attributes

    def test_score_is_always_none(self):
        # GenBank has no score column. None, not 0.0 -- the reasoning
        # annotation_parse._score documents.
        row = list(iter_features(FEATURE_LINES[:3], accession="NC_1"))[0]
        assert row.score is None

    def test_malformed_location_is_skipped(self):
        lines = ["     CDS             not-a-location", '                     /gene="x"']
        assert list(iter_features(lines, accession="X")) == []

    def test_short_continuation_line_is_not_mistaken_for_a_new_feature(self):
        # A wrapped qualifier's tail line can be shorter than column 22 --
        # e.g. trimmed by a hand-edited file or a tool that rstrips trailing
        # whitespace. It must still be read as a continuation of the open
        # qualifier, not misclassified as a new feature key: the old
        # boundary check only required reaching column 6, and a short line
        # like "      short" has content there but never reaches column 22,
        # so line[21:22] was "" -- and "".isspace() is False, which made the
        # key-detection branch fire by mistake.
        lines = [
            "     CDS             1..9",
            '                     /note="wraps',
            "      short",
        ]
        rows = list(iter_features(lines, accession="X"))
        assert len(rows) == 1
        assert "note=wraps%20short" in rows[0].attributes

    def test_blank_line_inside_qualifier_block_does_not_break_accumulation(self):
        # A blank line between two features is ordinary. A blank line in the
        # middle of one feature's qualifier block -- a stray newline from a
        # hand-edited file -- must be skipped without ending the feature or
        # losing the qualifiers on either side of it.
        lines = [
            "     CDS             1..9",
            '                     /gene="thrA"',
            "",
            '                     /product="aspartokinase"',
        ]
        rows = list(iter_features(lines, accession="X"))
        assert len(rows) == 1
        assert "gene=thrA" in rows[0].attributes
        assert "product=aspartokinase" in rows[0].attributes
