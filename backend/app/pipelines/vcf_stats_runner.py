"""Building and parsing bcftools output for the Variant Results tab.

Kept separate from the job handler so the parts worth testing -- command
construction, stats parsing, re-binning, summary derivation -- are pure
functions over strings, with no queue or filesystem involved. Mirrors
bam_stats_runner.py's split for the same reason.
"""

# The columns of the variant table, in the order build_query_command emits
# them. The format string below and this tuple are one definition split in
# two: changing either alone shifts every value one column left or right.
VARIANT_COLUMNS = (
    "chrom",
    "pos",
    "ref",
    "alt",
    "qual",
    "filter",
    "dp",
    "gt",
)

# Real tab and newline escapes. A literal backslash-t here makes bcftools emit
# one unsplittable column, and every variant lands in the database as a single
# field -- verified against bcftools 1.21, this yields exactly 8 columns.
QUERY_FORMAT = "%CHROM\t%POS\t%REF\t%ALT\t%QUAL\t%FILTER\t%INFO/DP[\t%GT]\n"

# `number of X:` keys in the SN section, mapped to the names used in facts.
_SN_KEYS = {
    "number of samples:": "samples",
    "number of records:": "records",
    "number of no-ALTs:": "no_alts",
    "number of SNPs:": "snps",
    "number of MNPs:": "mnps",
    "number of indels:": "indels",
    "number of others:": "others",
    "number of multiallelic sites:": "multiallelic_sites",
    "number of multiallelic SNP sites:": "multiallelic_snp_sites",
}


def build_stats_command(*, bcftools_path: str, vcf) -> list[str]:
    """Whole-callset summary: counts, Ti/Tv, substitution types, and the
    QUAL/DP/indel-length distributions. One pass over the file."""
    return [bcftools_path, "stats", str(vcf)]


def build_query_command(*, bcftools_path: str, vcf) -> list[str]:
    """The per-variant table as TSV, one line per record.

    Streamed rather than collected: at plant scale this is tens of millions of
    lines, and materializing them would exhaust the container.
    """
    return [bcftools_path, "query", "-f", QUERY_FORMAT, str(vcf)]


def parse_stats(text: str) -> dict:
    """The sections of `bcftools stats` output, as typed rows.

    Section-marker driven and tolerant of absences: an empty VCF emits the
    headers with no data rows, which is a normal outcome of a strict caller
    rather than an error. Unrecognised sections are ignored, so a future
    bcftools release adding one does not break parsing.
    """
    sn: dict[str, int] = {}
    tstv: dict[str, float] = {}
    st: list[dict] = []
    qual: list[dict] = []
    dp: list[dict] = []
    idd: list[dict] = []

    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        section = parts[0]

        if section == "SN" and len(parts) >= 4:
            key = _SN_KEYS.get(parts[2])
            if key is not None:
                sn[key] = int(parts[3])
        elif section == "TSTV" and len(parts) >= 5:
            tstv = {
                "ts": int(parts[2]),
                "tv": int(parts[3]),
                "ti_tv": float(parts[4]),
            }
        elif section == "ST" and len(parts) >= 4:
            st.append({"type": parts[2], "count": int(parts[3])})
        elif section == "QUAL" and len(parts) >= 4:
            # Column 3 is the quality value; column 4 the number of SNPs at
            # it. bcftools emits '.' for a file without QUAL scores.
            if parts[2] == ".":
                continue
            qual.append({"qual": float(parts[2]), "count": int(parts[3])})
        elif section == "DP" and len(parts) >= 6:
            # Column 6 is number of *sites*, not column 4's number of
            # genotypes -- the latter is 0 for a file bcftools did not
            # genotype, which would draw an empty chart for a file that
            # plainly has depth.
            dp.append({"depth": int(parts[2]), "count": int(parts[5])})
        elif section == "IDD" and len(parts) >= 4:
            idd.append({"length": int(parts[2]), "count": int(parts[3])})

    return {"sn": sn, "tstv": tstv, "st": st, "qual": qual, "dp": dp, "idd": idd}


# How many buckets the stored QUAL and DP histograms hold. bcftools emits one
# row per distinct value -- 805 and 211 respectively on a 6,641-variant test
# file, and far more at plant scale -- which is a list to store, not a shape
# to read. This is the BIN_COUNT of this module.
HISTOGRAM_BUCKETS = 40


def rebin_distribution(
    rows: list[dict], *, value_key: str, bucket_count: int = HISTOGRAM_BUCKETS
) -> list[dict]:
    """Collapse a one-row-per-distinct-value distribution into a histogram.

    Buckets span the observed range in equal widths, and every observation
    lands in exactly one -- the total count is preserved, so the histogram
    describes the same data at lower resolution rather than a sample of it.

    Returns `[{"value", "count"}]` where `value` is the bucket's lower bound,
    so a caller can label an axis without knowing the bucket width. A
    distribution with a single distinct value returns one bucket rather than
    dividing by a zero-width range.
    """
    if not rows:
        return []

    values = [float(r[value_key]) for r in rows]
    lo, hi = min(values), max(values)

    if hi == lo or len(rows) <= bucket_count:
        return [
            {"value": float(r[value_key]), "count": int(r["count"])} for r in rows
        ]

    width = (hi - lo) / bucket_count
    sums = [0] * bucket_count
    for r in rows:
        # The maximum lands one past the last bucket without this clamp.
        idx = min(int((float(r[value_key]) - lo) / width), bucket_count - 1)
        sums[idx] += int(r["count"])

    return [
        {"value": round(lo + i * width, 4), "count": c}
        for i, c in enumerate(sums)
        if c > 0
    ]
