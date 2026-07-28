"""Inferring read chemistry from numbers QC has already produced.

Pure and deliberately conservative: `sam_platform` answers "who made this"
(ONT/PACBIO/ILLUMINA), but HiFi and CLR are both PACBIO_SMRT in SRA and both
PACBIO in SAM, so it cannot answer "how accurate is it" -- the question the
minimap2 preset actually needs. NanoPlot already reports mean read length and
mean quality, so this needs no new tool run and no extra pass over the file.
When the evidence is ambiguous the answer is UNKNOWN rather than a guess
presented as fact; the caller falls back to the platform default.
"""

from app.pipelines.align_runner import ReadChemistry

# Below this, "long read" is not a credible read of the data regardless of
# platform -- more likely a mislabelled short-read file than genuine PacBio
# or ONT output at an unusual length.
_SHORT_READ_LENGTH_CEILING = 1000

# PacBio HiFi/CCS reads are basecalled to ~Q20+ (~99%+ accuracy) by design;
# CLR is the older chemistry at roughly Q10, well below Q15. Between the two
# thresholds the evidence does not clearly say either way.
_PACBIO_HIFI_MIN_QUALITY = 20
_PACBIO_CLR_MAX_QUALITY = 15  # exclusive: CLR is strictly below this

# ONT duplex/Q20+ chemistry is the high-accuracy mode; anything below it is
# ordinary simplex calling.
_ONT_DUPLEX_MIN_QUALITY = 20


def infer_chemistry(
    *,
    platform: str,
    mean_read_length: float | None,
    mean_quality: float | None,
) -> tuple[ReadChemistry, str]:
    """The best guess at read chemistry, and a short human reason for it.

    The reason exists so the align dialog can say *why* it picked something,
    not just what -- an inferred default that cannot explain itself invites
    the user to distrust or ignore it.
    """
    platform = (platform or "").upper()

    if mean_read_length is not None and mean_read_length < _SHORT_READ_LENGTH_CEILING:
        return (
            ReadChemistry.SHORT,
            f"{mean_read_length:.0f} bp mean length -- too short to be a "
            "genuine long read",
        )

    if mean_read_length is None or mean_quality is None:
        return ReadChemistry.UNKNOWN, "missing read length or quality data"

    length_kb = mean_read_length / 1000

    if platform == "PACBIO":
        if mean_quality >= _PACBIO_HIFI_MIN_QUALITY:
            return (
                ReadChemistry.HIFI,
                f"{length_kb:.1f} kb reads at Q{mean_quality:.0f} -- HiFi",
            )
        if mean_quality < _PACBIO_CLR_MAX_QUALITY:
            return (
                ReadChemistry.CLR,
                f"{length_kb:.1f} kb reads at Q{mean_quality:.0f} -- CLR",
            )
        return (
            ReadChemistry.UNKNOWN,
            f"{length_kb:.1f} kb reads at Q{mean_quality:.0f} -- between CLR "
            "and HiFi accuracy, not clearly either",
        )

    if platform == "ONT":
        if mean_quality >= _ONT_DUPLEX_MIN_QUALITY:
            return (
                ReadChemistry.ONT_DUPLEX,
                f"{length_kb:.1f} kb reads at Q{mean_quality:.0f} -- ONT duplex",
            )
        return (
            ReadChemistry.ONT_SIMPLEX,
            f"{length_kb:.1f} kb reads at Q{mean_quality:.0f} -- ONT simplex",
        )

    return ReadChemistry.UNKNOWN, f"unrecognized long-read platform {platform!r}"
