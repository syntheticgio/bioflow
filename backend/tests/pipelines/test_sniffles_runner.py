from pathlib import Path

import pytest

from app.pipelines import sniffles_runner
from app.pipelines.align_runner import ReadChemistry


def test_command_carries_bam_reference_and_output():
    argv = sniffles_runner.build_sniffles_command(
        sniffles_path="sniffles",
        bam=Path("/data/sample.bam"),
        reference=Path("/data/ref.fa"),
        output=Path("/out/sample.sv.vcf.gz"),
        params=sniffles_runner.SnifflesParams(),
    )
    assert argv[0] == "sniffles"
    assert "--input" in argv and "/data/sample.bam" in argv
    assert "--reference" in argv and "/data/ref.fa" in argv
    assert "--vcf" in argv and "/out/sample.sv.vcf.gz" in argv


def test_min_support_is_omitted_when_unset():
    """Unset must reach Sniffles as "decide for me", not as a number.

    Sniffles derives support from coverage. A hardcoded default would be
    wrong in both directions -- too high on a 10x callset, too low on a
    100x one -- so the flag is absent rather than defaulted.
    """
    argv = sniffles_runner.build_sniffles_command(
        sniffles_path="sniffles",
        bam=Path("/data/s.bam"),
        reference=Path("/data/r.fa"),
        output=Path("/out/s.vcf.gz"),
        params=sniffles_runner.SnifflesParams(),
    )
    assert "--minsupport" not in argv


def test_min_support_is_passed_when_set():
    argv = sniffles_runner.build_sniffles_command(
        sniffles_path="sniffles",
        bam=Path("/data/s.bam"),
        reference=Path("/data/r.fa"),
        output=Path("/out/s.vcf.gz"),
        params=sniffles_runner.SnifflesParams(min_support=7),
    )
    assert "--minsupport" in argv
    assert argv[argv.index("--minsupport") + 1] == "7"


def test_min_sv_length_defaults_to_fifty():
    params = sniffles_runner.SnifflesParams()
    assert params.min_sv_length == 50


@pytest.mark.parametrize(
    "chemistry",
    [
        ReadChemistry.HIFI,
        ReadChemistry.ONT_SIMPLEX,
        ReadChemistry.ONT_DUPLEX,
    ],
)
def test_long_read_chemistries_are_allowed(chemistry):
    assert sniffles_runner.sv_calling_allowed_for(chemistry) is True


def test_clr_is_allowed_even_though_small_variant_calling_refuses_it():
    """The asymmetry is deliberate -- do not "fix" it into consistency.

    variant_runner.caller_for_chemistry refuses CLR because its error rate
    ruins SNV calling. SV calling accepts it: Sniffles resolves breakpoints
    from alignment structure, which tolerates that error rate, and CLR reads
    are long -- which is the property SV detection actually needs.
    """
    assert sniffles_runner.sv_calling_allowed_for(ReadChemistry.CLR) is True


@pytest.mark.parametrize(
    "chemistry", [ReadChemistry.SHORT, ReadChemistry.UNKNOWN]
)
def test_short_and_unknown_are_refused(chemistry):
    assert sniffles_runner.sv_calling_allowed_for(chemistry) is False


def test_snf_output_passed_when_set():
    argv = sniffles_runner.build_sniffles_command(
        sniffles_path="sniffles",
        bam=Path("/data/s.bam"),
        reference=Path("/data/r.fa"),
        output=Path("/out/s.vcf.gz"),
        params=sniffles_runner.SnifflesParams(),
        snf_output=Path("/out/s.snf"),
    )
    assert "--snf" in argv
    assert argv[argv.index("--snf") + 1] == "/out/s.snf"


def test_combine_command_assembly():
    argv = sniffles_runner.build_sniffles_combine_command(
        sniffles_path="sniffles",
        snf_inputs=[Path("/data/s1.snf"), Path("/data/s2.snf")],
        output=Path("/out/combined.vcf.gz"),
        threads=8,
    )
    assert argv[0] == "sniffles"
    assert "--input" in argv
    idx = argv.index("--input")
    assert argv[idx + 1] == "/data/s1.snf"
    assert argv[idx + 2] == "/data/s2.snf"
    assert "--vcf" in argv and "/out/combined.vcf.gz" in argv
    assert "--threads" in argv and "8" in argv

