"""Which structural variant caller covers which read chemistry.

Its own module rather than a function inside either runner: a Delly run
importing `sniffles_runner` to discover that it should use Delly is the
tangle that puts the next caller's branch in the wrong file. Both runners
and every consumer depend on this; it depends on neither runner.
"""

from enum import StrEnum

from app.pipelines.align_runner import ReadChemistry


class SvCaller(StrEnum):
    SNIFFLES2 = "sniffles2"
    DELLY = "delly"


# Chemistries whose reads are long enough for breakpoint resolution.
_LONG_READ = frozenset(
    {
        ReadChemistry.HIFI,
        ReadChemistry.CLR,
        ReadChemistry.ONT_SIMPLEX,
        ReadChemistry.ONT_DUPLEX,
    }
)


def caller_for_chemistry(chemistry: ReadChemistry) -> SvCaller | None:
    """Which SV caller covers this chemistry. None means none does.

    CLR maps to a caller here while `variant_runner.caller_for_chemistry`
    refuses it outright. That asymmetry is deliberate and must not be
    harmonised away: small-variant calling reads per-base accuracy, which
    CLR does not have, while SV calling resolves breakpoints from alignment
    structure -- split reads and within-read gaps -- which tolerates a high
    per-base error rate. CLR reads are long, and length is the property SV
    detection needs.

    Delly ships a `delly lr` long-read mode that this function deliberately
    never selects. Sniffles2 is the long-read standard here and produces the
    .snf sidecar the merge card depends on; swapping it would invalidate
    #619's testing for no capability gain.

    UNKNOWN returns None because it means QC has not run. An unrecognised
    BAM that turns out to be Illumina would produce junk quietly under a
    long-read caller, and vice versa.
    """
    if chemistry in _LONG_READ:
        return SvCaller.SNIFFLES2
    if chemistry is ReadChemistry.SHORT:
        return SvCaller.DELLY
    return None
