"""Which BAMs the RNA-seq QC charts apply to.

Inference, not a stored fact -- see the module docstring. The precedence
between signals is the part worth testing: the authoritative field is
unpopulated on real data, so the fallbacks carry the feature.
"""

from app.services.transcript_qc_gating import Applicability, applicability


def _obj(metadata=None, facts=None):
    return {"metadata": metadata or {}, "facts": facts or {}}


class TestApplicability:
    def test_explicit_rna_molecule_type_applies(self):
        got = applicability(_obj(metadata={"molecule_type": "RNA"}))
        assert got.gene_body is True
        assert got.feature_distribution is True
        assert got.reason == "molecule_type"

    def test_explicit_dna_molecule_type_beats_a_splice_aware_aligner(self):
        """An explicit answer outranks every inference below it. STAR is
        routinely used for DNA in some workflows, so the aligner must not
        override a stated molecule type."""
        got = applicability(
            _obj(metadata={"molecule_type": "DNA"}, facts={"aligned_by": "star"})
        )
        assert got.gene_body is False
        assert got.feature_distribution is False

    def test_rnaseq_assay_applies_when_molecule_type_is_missing(self):
        """This is the branch that carries the feature in practice:
        molecule_type is populated on 0 of 9 BAMs in the real database, while
        assay is populated on all 9."""
        got = applicability(_obj(metadata={"assay": "RNA-seq"}))
        assert got.gene_body is True
        assert got.feature_distribution is True
        assert got.reason == "assay"

    def test_chipseq_gets_feature_distribution_only(self):
        """ChIP-seq is DNA, so a gene body curve is meaningless for it -- but
        where its reads fall relative to genes is exactly the question."""
        got = applicability(_obj(metadata={"assay": "ChIP-seq"}))
        assert got.gene_body is False
        assert got.feature_distribution is True

    def test_splice_aware_aligner_applies_as_a_last_resort(self):
        for aligner in ("star", "hisat2"):
            got = applicability(_obj(facts={"aligned_by": aligner}))
            assert got.gene_body is True, aligner
            assert got.reason == "aligner"

    def test_a_dna_aligner_does_not_apply(self):
        got = applicability(_obj(facts={"aligned_by": "bwa-mem2"}))
        assert got.gene_body is False
        assert got.feature_distribution is False

    def test_nothing_known_does_not_apply(self):
        got = applicability(_obj())
        assert got.gene_body is False
        assert got.feature_distribution is False
        assert got.reason is None

    def test_wgs_assay_does_not_apply(self):
        got = applicability(_obj(metadata={"assay": "WGS"}))
        assert got.gene_body is False
        assert got.feature_distribution is False
