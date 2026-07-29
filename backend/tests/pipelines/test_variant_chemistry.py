"""Carrying the inferred read chemistry from reads onto their alignment.

QC infers chemistry once, on the FASTQ. Everything downstream that needs to
know how accurate the reads are -- the aligner preset today, the variant caller
next -- reads that one fact rather than re-inferring it. This module covers the
step that makes it reachable from a BAM.
"""

from app.models import SidecarRole
from app.pipelines.align_runner import ReadChemistry
from app.queue.results import _APPLIERS, _SIDECAR_ROLES, align_provenance, variant_provenance


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


class TestVariantProvenance:
    def test_records_the_caller(self):
        """Clair3 and bcftools disagree about marginal sites, so a VCF whose
        caller is unrecorded cannot be compared against another."""
        prov = variant_provenance(
            {"caller": "clair3", "tool_version": "2.0.2", "params": {"threads": 4}}
        )
        assert prov["variants_called_by"] == "clair3"
        assert prov["variant_caller_version"] == "2.0.2"
        assert prov["variant_params"] == {"threads": 4}

    def test_missing_params_default_to_empty(self):
        prov = variant_provenance({"caller": "bcftools"})
        assert prov["variant_params"] == {}


class TestApplierWiring:
    def test_call_variants_applier_is_registered(self):
        """A handler whose applier is unregistered runs, succeeds, and silently
        produces no object -- the failure mode this table exists to prevent."""
        assert "call_variants" in _APPLIERS

    def test_tbi_maps_to_its_sidecar_role(self):
        """The handler reports role='tbi' as a plain string; without this entry
        the .tbi would be ingested with no role at all."""
        assert _SIDECAR_ROLES["tbi"] is SidecarRole.TBI
