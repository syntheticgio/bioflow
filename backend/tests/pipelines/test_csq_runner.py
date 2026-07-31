"""The bcftools csq command.

The property worth testing is the phase flag. `csq` defaults to `-p r`, which
*requires* phased genotypes and exits 255 on the first unphased heterozygous
site -- and `bcftools call`, which produces every VCF this app annotates,
emits unphased genotypes. Measured on the real yeast VCF: the default aborts,
`-p a` annotates 4,152 of 6,641.
"""

from pathlib import Path

from app.pipelines import csq_runner


class TestBuildCsqCommand:
    def _cmd(self):
        return csq_runner.build_csq_command(
            bcftools_path="/usr/bin/bcftools",
            vcf=Path("/tmp/in.vcf.gz"),
            reference=Path("/tmp/ref.fa"),
            annotation=Path("/tmp/genes.gff3"),
            out=Path("/tmp/out.vcf.gz"),
        )

    # The regression this file exists for. Without it the job dies at runtime
    # with exit 255 on any heterozygous call.
    def test_passes_phase_a(self):
        cmd = self._cmd()
        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "a"

    def test_passes_reference_and_annotation(self):
        cmd = self._cmd()
        assert cmd[cmd.index("-f") + 1] == "/tmp/ref.fa"
        assert cmd[cmd.index("-g") + 1] == "/tmp/genes.gff3"

    def test_writes_compressed_output(self):
        cmd = self._cmd()
        assert cmd[cmd.index("-O") + 1] == "z"
        assert cmd[cmd.index("-o") + 1] == "/tmp/out.vcf.gz"

    def test_input_is_last_and_the_subcommand_is_csq(self):
        cmd = self._cmd()
        assert cmd[0] == "/usr/bin/bcftools"
        assert cmd[1] == "csq"
        assert cmd[-1] == "/tmp/in.vcf.gz"


class TestGffWarnings:
    """Real NCBI GFF3 files emit these on every run -- the T. brucei
    annotation produces all three. They are not failures."""

    def test_recognises_benign_parse_warnings(self):
        for line in (
            "Warning: Ignoring GFF feature with unknown phase .. NC_008409.1",
            "Warning: The GFF contains features with duplicate id .. NC_008409.1",
            "Warning: Ignoring transcript with unknown biotype .. NC_007276.1",
            "Note: truncated transcript rna-XM_842566.1 with incomplete CDS",
        ):
            assert csq_runner.is_benign_gff_warning(line)

    def test_does_not_swallow_a_real_error(self):
        assert not csq_runner.is_benign_gff_warning(
            "Unphased heterozygous genotype at NC_001133.9:88609"
        )
        assert not csq_runner.is_benign_gff_warning("[E::faidx] Failed to open ref.fa")

    # An "[E::" line is a failure whatever else it says. Checked ahead of the
    # substrings so the safe direction is explicit rather than a consequence of
    # which phrases happen to be listed.
    def test_an_error_line_is_never_benign_even_if_it_matches_a_marker(self):
        assert not csq_runner.is_benign_gff_warning(
            "[E::idx] Duplicate id in the index, unknown phase"
        )

    # "duplicate id" alone is generic enough to appear in a fatal message about
    # the VCF or its index; the marker is the fuller observed phrase so that a
    # swallowed failure cannot leave a job looking successful with no output.
    def test_a_bare_duplicate_id_message_is_not_treated_as_gff_noise(self):
        assert not csq_runner.is_benign_gff_warning(
            "Failed to build index: duplicate id at record 12"
        )
