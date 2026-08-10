"""What goes into the summary prompt, and -- mostly -- what stays out.

The failure mode this feature has is not a crash, it is a confident paragraph
about the wrong things: array lengths, sampling parameters, an invented species.
Every test here is about the selection policy that prevents that, so they assert
on the *absence* of noise at least as often as the presence of signal.
"""

from app.services.summary_prompt import build_user_prompt


def _reads_facts() -> dict:
    return {
        "qc_tool": "fastp",
        "qc_before_filtering": {
            "total_reads": 4_200_000,
            "total_bases": 630_000_000,
            "q20_rate": 0.972,
            "q30_rate": 0.931,
            "gc_content": 0.412,
            "read1_mean_length": 150,
            "read2_mean_length": 150,
        },
        "qc_duplication_rate": 0.184,
    }


class TestOrganismHandling:
    def test_a_known_organism_is_stated_for_the_model_to_interpret_against(self):
        prompt = build_user_prompt(
            name="reads.fastq.gz",
            format_kind="fastq",
            role=None,
            organism="Homo sapiens",
            facts=_reads_facts(),
            metadata={},
        )
        assert "Organism: Homo sapiens" in prompt

    def test_an_unknown_organism_forbids_guessing_rather_than_going_unmentioned(self):
        """Silence would invite the model to infer a species from the filename,
        which is exactly the fabrication this feature must not produce."""
        prompt = build_user_prompt(
            name="human_liver_sample.fastq.gz",
            format_kind="fastq",
            role=None,
            organism=None,
            facts=_reads_facts(),
            metadata={},
        )
        assert "not recorded" in prompt
        assert "do not guess" in prompt.lower()


class TestNoiseExclusion:
    def test_per_position_curves_and_histograms_are_left_out(self):
        facts = {
            **_reads_facts(),
            "quality_per_position": list(range(150)),
            "base_composition": [{"A": 1}] * 150,
            "mapq_histogram": [1] * 60,
        }
        prompt = build_user_prompt(
            name="reads.fastq.gz",
            format_kind="fastq",
            role=None,
            organism="Homo sapiens",
            facts=facts,
            metadata={},
        )
        assert "quality_per_position" not in prompt
        assert "base_composition" not in prompt
        assert "mapq_histogram" not in prompt

    def test_sampling_bookkeeping_is_left_out(self):
        facts = {**_reads_facts(), "stats_sampled_reads": 1000, "stats_sampling": "head", "has_index": True}
        prompt = build_user_prompt(
            name="reads.fastq.gz",
            format_kind="fastq",
            role=None,
            organism="Homo sapiens",
            facts=facts,
            metadata={},
        )
        assert "sampled_reads" not in prompt
        assert "stats_sampling" not in prompt
        assert "has_index" not in prompt


class TestNumberPresentation:
    def test_rates_are_rendered_as_percentages_not_fractions(self):
        """fastp stores 0.931; nobody reads a Q30 rate that way."""
        prompt = build_user_prompt(
            name="reads.fastq.gz",
            format_kind="fastq",
            role=None,
            organism="Homo sapiens",
            facts=_reads_facts(),
            metadata={},
        )
        assert "93.1%" in prompt
        assert "0.931" not in prompt

    def test_adapter_presence_is_reported_without_printing_the_oligo(self):
        facts = {
            **_reads_facts(),
            "qc_adapters": {"read1_sequence": "AGATCGGAAGAGC", "read2_sequence": None},
        }
        prompt = build_user_prompt(
            name="reads.fastq.gz",
            format_kind="fastq",
            role=None,
            organism="Homo sapiens",
            facts=facts,
            metadata={},
        )
        assert "Adapter contamination detected" in prompt
        assert "AGATCGGAAGAGC" not in prompt


class TestSampledVersusMeasured:
    def test_ingest_sampled_stats_are_labelled_as_a_sample(self):
        """Presenting the sampler's numbers as whole-file truth would be wrong,
        and the model cannot tell the two blocks apart without being told."""
        prompt = build_user_prompt(
            name="reads.fastq.gz",
            format_kind="fastq",
            role=None,
            organism="Homo sapiens",
            facts={"gc_content_percent": 41.2, "mean_quality": 34.1, "read_length": 150},
            metadata={"platform": "ILLUMINA"},
        )
        assert "not the" in prompt and "whole file" in prompt


class TestSuppression:
    def test_a_file_with_nothing_measured_produces_no_prompt(self):
        """A summary of a file we know nothing about is filler by construction."""
        assert (
            build_user_prompt(
                name="mystery.dat",
                format_kind="unknown",
                role=None,
                organism=None,
                facts={},
                metadata={},
            )
            is None
        )


class TestPerFormatSelection:
    def test_alignment_files_get_mapping_statistics(self):
        prompt = build_user_prompt(
            name="sample.bam",
            format_kind="bam",
            role="alignment",
            organism="Homo sapiens",
            facts={"total_reads": 4_000_000, "mapped_pct": 97.4, "duplicate_pct": 12.1},
            metadata={},
        )
        assert "Mapping rate" in prompt
        assert "97.40%" in prompt or "97.4" in prompt

    def test_variant_files_get_variant_statistics(self):
        prompt = build_user_prompt(
            name="calls.vcf.gz",
            format_kind="vcf",
            role="variants",
            organism="Homo sapiens",
            facts={"sample_count": 3, "variant_types_sampled": ["snp", "indel"]},
            metadata={},
        )
        assert "Variant types present" in prompt
        assert "snp" in prompt

    def test_trimmed_output_reports_that_it_was_trimmed(self):
        """A summary of trimmed reads that does not say so is misleading."""
        prompt = build_user_prompt(
            name="reads.trimmed.fastq.gz",
            format_kind="fastq",
            role="trimmed_reads",
            organism="Homo sapiens",
            facts={**_reads_facts(), "trimmed_by": "fastp", "trim_reads_after": 4_010_000},
            metadata={},
        )
        assert "Trimming provenance" in prompt
        assert "Trimmed with" in prompt
