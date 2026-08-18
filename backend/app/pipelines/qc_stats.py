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

# SRA `PLATFORM` tag name <-> the short vocabulary this module and
# `sam_platform` speak (ONT/PACBIO). The single source for both directions --
# previously `_QC_STATS_PLATFORM` and `_SAM_TO_SRA_PLATFORM` held each other's
# inverse independently in two different files (pipeline_handlers.py and
# pipeline_service.py), and a third file (reference_assembly.py) carried a
# third copy of just the SRA-side keys. Adding a long-read platform meant
# editing three places by hand with no error if you missed two of them; one of
# those places also had a silent `.get(platform, platform)` fall-through that
# passed an unmapped SRA tag straight into `infer_chemistry`, which returns
# UNKNOWN with a plausible-looking reason rather than surfacing the miss.
LONG_READ_PLATFORMS: dict[str, str] = {
    "OXFORD_NANOPORE": "ONT",
    "PACBIO_SMRT": "PACBIO",
}

# The inverse of LONG_READ_PLATFORMS, derived rather than hand-written so the
# two directions cannot drift relative to each other.
SHORT_TO_SRA_PLATFORM: dict[str, str] = {v: k for k, v in LONG_READ_PLATFORMS.items()}

# Full SAM `PL` -> SRA `PLATFORM` translation, for callers that need a real
# answer for every platform rather than only the long-read pair.
# `SHORT_TO_SRA_PLATFORM` above cannot serve this: it is deliberately the
# inverse of LONG_READ_PLATFORMS, so it has no entry for ILLUMINA and every
# short-read platform resolves to None there. That is correct for
# `_qc_platform`, which defaults an unmapped value to ILLUMINA anyway, and
# wrong for #525's migration, which must tell "this is Illumina" apart from
# "this is a machine nobody recognizes" -- the second clears the field rather
# than guessing.
#
# Not derived: the two vocabularies genuinely differ in spelling
# (OXFORD_NANOPORE vs ONT, ION_TORRENT vs IONTORRENT) and in membership --
# SAM has no BGISEQ, since the spec folded MGI/BGI into DNBSEQ. Both sides
# are externally owned, so this is CLAUDE.md's third registry case: keys that
# cannot be derived, pinned instead by an exhaustiveness test over the SAM
# enum (`test_every_sam_platform_translates`).
SAM_TO_SRA_PLATFORM: dict[str, str] = {
    "ILLUMINA": "ILLUMINA",
    "ONT": "OXFORD_NANOPORE",
    "PACBIO": "PACBIO_SMRT",
    "DNBSEQ": "DNBSEQ",
    "ELEMENT": "ELEMENT",
    "ULTIMA": "ULTIMA",
    "SINGULAR": "SINGULAR",
    "IONTORRENT": "ION_TORRENT",
    "LS454": "LS454",
    "SOLID": "SOLID",
    "HELICOS": "HELICOS",
    "CAPILLARY": "CAPILLARY",
}

# SRA PLATFORM tags that take the short-read QC path by default -- i.e. never
# reach LONG_READ_PLATFORMS. Inclusion rule: a tag belongs here if and only if
# its instrument family always produces short reads; SRA's PLATFORM vocabulary
# has further values (e.g. CAPILLARY) that are neither long- nor
# short-read-affirming and are deliberately left off, falling to the
# chemistry tie-break in `reference_assembly.is_short_read` instead of being
# answered directly here.
SHORT_READ_PLATFORMS: frozenset[str] = frozenset(
    {"ILLUMINA", "BGISEQ", "DNBSEQ", "ELEMENT", "ULTIMA", "SINGULAR", "ION_TORRENT"}
)

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
