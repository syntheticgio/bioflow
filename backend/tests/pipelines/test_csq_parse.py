"""BCSQ parsing.

Most fixtures here are real strings from `bcftools csq -p a` run against
DRR1066343.bcftools.vcf.gz with the GCF_000146045.2 GFF3. The format is not
one fixed shape -- records carry 7, 5, 4 or 1 fields -- and the parser exists
because a `split("|")[5]` would be wrong on three of those four.

The severity-ranking and malformed-input tests (`GENE1`/`rna-1` style, the
"??>??" case, and the unknown-consequence-type cases) are hand-built instead:
the measured run had no frameshift-beside-synonymous record, no malformed aa
field, and no consequence type outside its own vocabulary, so those shapes
are constructed to exercise behaviour the real data never happened to hit.
"""

from app.pipelines import csq_parse


class TestSingleConsequence:
    def test_parses_a_full_missense_record(self):
        c = csq_parse.parse_bcsq(
            "missense|CYS3|rna-NM_001178157.1|protein_coding|+|160K>160M|131277A>T"
        )
        assert c is not None
        assert c.consequence == "missense"
        assert c.gene == "CYS3"
        assert c.transcript == "rna-NM_001178157.1"
        assert c.aa_pos == 160
        assert c.aa_change == "160K>160M"

    # Synonymous records carry position and residue with no ">" -- there is no
    # change to describe.
    def test_parses_a_synonymous_record_with_no_arrow(self):
        c = csq_parse.parse_bcsq(
            "synonymous|SSA1|rna-NM_001178151.1|protein_coding|-|99P|141135A>T"
        )
        assert c.consequence == "synonymous"
        assert c.aa_pos == 99
        assert c.aa_change == "99P"

    # 5 fields: no amino-acid column at all.
    def test_parses_a_five_field_start_lost(self):
        c = csq_parse.parse_bcsq(
            "start_lost|SNU23|rna-NM_001180157.1|protein_coding|-"
        )
        assert c.consequence == "start_lost"
        assert c.gene == "SNU23"
        assert c.aa_pos is None
        assert c.aa_change is None

    # 4 fields, and the transcript column is empty.
    def test_parses_a_four_field_intron(self):
        c = csq_parse.parse_bcsq("intron|RPL19B||protein_coding")
        assert c.consequence == "intron"
        assert c.gene == "RPL19B"
        assert c.transcript is None
        assert c.aa_pos is None

    def test_parses_a_structural_variant_overlap_record(self):
        c = csq_parse.parse_bcsq(
            "structural_variant_overlap|GENE1,GENE2||protein_coding"
        )
        assert c is not None
        assert c.consequence == "structural_variant_overlap"
        assert c.gene == "GENE1,GENE2"
        assert c.transcript is None

    # A "*" prefix marks a compound/haplotype-modified prediction.
    def test_strips_the_compound_marker(self):
        c = csq_parse.parse_bcsq(
            "*missense|ADH1|rna-NM_001183340.1|protein_coding|+|48T>48A|15000A>G"
        )
        assert c.consequence == "missense"
        assert c.compound is True


class TestPointersAndLists:
    # "@position" is a pointer to another record sharing a haplotype, not a
    # consequence. Alone, it yields nothing.
    def test_a_bare_pointer_yields_nothing(self):
        assert csq_parse.parse_bcsq("@286153") is None

    # THE case that motivated this parser: a pointer can sit inside a comma
    # list beside a real consequence. Rejecting the whole record on seeing "@"
    # would discard a real annotation.
    def test_a_pointer_beside_a_real_consequence_keeps_the_real_one(self):
        c = csq_parse.parse_bcsq(
            "missense|CHS3|rna-NM_001178371.1|protein_coding|-|1163F>1163L|284437G>T,@286153"
        )
        assert c is not None
        assert c.gene == "CHS3"
        assert c.consequence == "missense"

    # The same variant against two overlapping transcripts.
    def test_counts_additional_consequences(self):
        c = csq_parse.parse_bcsq(
            "synonymous|YBL100W-A|rna-NM_001180049.1|protein_coding|+|26V|30012C>T,"
            "synonymous|YBL100W-B|rna-NM_001180050.2|protein_coding|+|26V|30012C>T"
        )
        assert c.gene == "YBL100W-A"
        assert c.additional == 1

    # Severity ranking, not file order: a frameshift beside a synonymous call
    # is the one worth showing in a single column.
    def test_keeps_the_most_severe_consequence(self):
        c = csq_parse.parse_bcsq(
            "synonymous|GENE1|rna-1|protein_coding|+|10A|100A>G,"
            "frameshift|GENE2|rna-2|protein_coding|+|20B|100A>G"
        )
        assert c.consequence == "frameshift"
        assert c.gene == "GENE2"

    # An unrecognised consequence type must not be ranked below everything
    # known -- that is how a new bcftools vocabulary entry would silently lose
    # to a synonymous call on an overlapping transcript.
    def test_an_unknown_consequence_outranks_the_benign_tail(self):
        c = csq_parse.parse_bcsq(
            "synonymous|GENE1|rna-1|protein_coding|+|10A|100A>G,"
            "brand_new_type|GENE2|rna-2|protein_coding|+|20B|100A>G"
        )
        assert c.consequence == "brand_new_type"
        assert c.gene == "GENE2"

    # But a genuinely severe known type still beats an unknown one.
    def test_a_known_severe_type_still_beats_an_unknown(self):
        c = csq_parse.parse_bcsq(
            "brand_new_type|GENE1|rna-1|protein_coding|+|10A|100A>G,"
            "frameshift|GENE2|rna-2|protein_coding|+|20B|100A>G"
        )
        assert c.consequence == "frameshift"


class TestAbsentAndMalformed:
    # bcftools query emits "." for a missing tag. Every un-annotated VCF is
    # this case, so it must be ordinary rather than an error.
    def test_a_dot_yields_nothing(self):
        assert csq_parse.parse_bcsq(".") is None

    def test_empty_yields_nothing(self):
        assert csq_parse.parse_bcsq("") is None
        assert csq_parse.parse_bcsq("   ") is None

    # Truncated to fewer fields than even the 4-field form.
    def test_too_few_fields_yields_nothing(self):
        assert csq_parse.parse_bcsq("missense|CYS3") is None

    # A non-numeric amino-acid position must not raise -- the consequence and
    # gene are still worth keeping.
    def test_unparseable_aa_position_keeps_the_rest(self):
        c = csq_parse.parse_bcsq(
            "missense|CYS3|rna-1|protein_coding|+|??>??|131277A>T"
        )
        assert c is not None
        assert c.gene == "CYS3"
        assert c.aa_pos is None
