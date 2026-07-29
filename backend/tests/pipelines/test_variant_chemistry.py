"""Carrying the inferred read chemistry from reads onto their alignment.

QC infers chemistry once, on the FASTQ. Everything downstream that needs to
know how accurate the reads are -- the aligner preset today, the variant caller
next -- reads that one fact rather than re-inferring it. This module covers the
step that makes it reachable from a BAM.
"""

from app.pipelines.align_runner import ReadChemistry
from app.queue.results import align_provenance


class TestAlignProvenance:
    def test_carries_chemistry_from_reads(self):
        prov = align_provenance(
            result={"aligner": "minimap2", "tool_version": "2.28"},
            reads_facts={"qc_read_chemistry": "ont_simplex"},
        )
        assert prov["qc_read_chemistry"] == "ont_simplex"

    def test_omits_chemistry_when_reads_have_none(self):
        """Absent stays absent: a missing fact must not become the string
        "None", which would parse back as an unrecognized chemistry."""
        prov = align_provenance(
            result={"aligner": "minimap2"},
            reads_facts={},
        )
        assert "qc_read_chemistry" not in prov

    def test_tolerates_missing_facts_entirely(self):
        prov = align_provenance(result={"aligner": "bwa-mem2"}, reads_facts=None)
        assert "qc_read_chemistry" not in prov

    def test_preserves_alignment_provenance(self):
        """Chemistry rides alongside the alignment facts, it does not replace
        them."""
        prov = align_provenance(
            result={
                "aligner": "minimap2",
                "tool_version": "2.28",
                "samtools_version": "1.21",
                "params": {"preset": "map-ont"},
                "read_group": {"ID": "rg1"},
            },
            reads_facts={"qc_read_chemistry": "hifi"},
        )
        assert prov["aligned_by"] == "minimap2"
        assert prov["aligner_version"] == "2.28"
        assert prov["samtools_version"] == "1.21"
        assert prov["align_params"] == {"preset": "map-ont"}
        assert prov["read_group"] == {"ID": "rg1"}
        assert prov["qc_read_chemistry"] == "hifi"

    def test_defaults_empty_params_and_read_group(self):
        prov = align_provenance(result={}, reads_facts={})
        assert prov["align_params"] == {}
        assert prov["read_group"] == {}

    def test_every_chemistry_round_trips(self):
        """Whatever QC wrote must survive the copy unchanged -- this fact is
        parsed back into the enum downstream."""
        for chemistry in ReadChemistry:
            prov = align_provenance(
                result={},
                reads_facts={"qc_read_chemistry": chemistry.value},
            )
            assert ReadChemistry(prov["qc_read_chemistry"]) is chemistry
