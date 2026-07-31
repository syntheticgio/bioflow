"""Building and parsing bcftools output for the Variant Results tab.

Kept separate from the job handler so the parts worth testing -- command
construction, stats parsing, re-binning, summary derivation -- are pure
functions over strings, with no queue or filesystem involved. Mirrors
bam_stats_runner.py's split for the same reason.
"""

from app.pipelines.bam_stats_runner import allocate_bins

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
#
# DP is bracketed rather than pinned to %INFO/DP because where a caller
# declares it varies: bcftools call declares DP in INFO, but Clair3 declares
# it only in FORMAT and has no INFO/DP at all. %INFO/DP hardcodes the first
# and fails outright -- "no such tag defined in the VCF header" -- on the
# second, breaking the feature for half the callers this app offers. Inside
# square brackets bcftools resolves %DP from whichever section declares it,
# so the same format string works for both.
#
# BCSQ sits before the sample block, not after it. `[\t%GT]` repeats once
# per sample, so a trailing consequence field cannot be told apart from an
# extra genotype by position -- a three-sample row without BCSQ has exactly
# as many fields as a two-sample row with it. Placed ahead of the repeating
# block it is always field 6, whatever the sample count. `-u` in
# build_query_command makes an undefined tag emit "." rather than failing
# the job, which is what every un-annotated VCF does here.
QUERY_FORMAT = "%CHROM\t%POS\t%REF\t%ALT\t%QUAL\t%FILTER\t%INFO/BCSQ[\t%DP][\t%GT]\n"

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

    `-u` (--allow-undef-tags) makes a VCF with no DP anywhere -- not even in
    FORMAT -- emit '.' for that field instead of erroring the whole job. That
    is on top of, not instead of, bracketing %DP: bracketing picks the right
    section when DP exists somewhere (Clair3's FORMAT vs. bcftools call's
    INFO), and -u covers the case where it exists nowhere.
    """
    return [bcftools_path, "query", "-u", "-f", QUERY_FORMAT, str(vcf)]


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


def variant_summary(stats: dict, *, filter_counts: dict[str, int]) -> dict:
    """The headline numbers, from the parsed stats and the FILTER tally.

    `pass_pct` is deliberately conditional. bcftools call does not stamp PASS
    -- every record in the reference test file carries '.' -- so a file whose
    only FILTER value is '.' has never been filtered at all. Reporting either
    0% or 100% for it would assert something untrue about the call set, so the
    rate is omitted and the UI simply does not show that statistic. A file
    that uses FILTER at all, even partially, gets a real rate.
    """
    sn = stats.get("sn", {})
    tstv = stats.get("tstv", {})

    total = sn.get("records", 0)
    no_filter = filter_counts.get(".", 0)
    uses_filter = any(k != "." for k in filter_counts)

    summary = {
        "variants": total,
        "snps": sn.get("snps", 0),
        "indels": sn.get("indels", 0),
        "multiallelic": sn.get("multiallelic_sites", 0),
        "samples": sn.get("samples", 0),
        "ts": tstv.get("ts", 0),
        "tv": tstv.get("tv", 0),
        "ti_tv": tstv.get("ti_tv", 0.0),
        "pass_count": filter_counts.get("PASS", 0),
        "no_filter_count": no_filter,
    }

    if uses_filter and total:
        summary["pass_pct"] = round(100 * summary["pass_count"] / total, 2)

    return summary


# Fixed regardless of genome size, so the stored array is the same size for a
# 135 Mb Arabidopsis genome as for a 16 Gb wheat one. Matches BIN_COUNT in
# bam_stats_runner, so the density strip and the coverage strip are directly
# comparable when both are on screen.
DENSITY_BINS = 1000


class DensityAccumulator:
    """Variant counts per bin and per contig, accumulated in one pass.

    Built as an accumulator rather than a function over a list because the
    variant stream is consumed once and never materialized -- at plant scale
    it is tens of millions of records. The handler feeds every record here
    while also writing it to the database, so the file is read once.

    Bin geometry comes from `allocate_bins`, shared with the BAM coverage
    strip, so a short contig gets its own bin in both rather than vanishing.
    """

    def __init__(self, *, contig_lengths: list[tuple[str, int]], bin_count: int = DENSITY_BINS):
        self._lengths = dict(contig_lengths)
        self._order = [name for name, _ in contig_lengths]
        self._geometry, self._boundaries, self._counts = allocate_bins(
            contig_lengths=contig_lengths, bin_count=bin_count
        )
        self._bins = [0] * bin_count if self._geometry else []
        self._per_contig: dict[str, dict] = {
            name: {"variants": 0, "snps": 0, "indels": 0} for name in self._order
        }

    def add(self, contig: str, pos: int, *, ref: str, alt: str) -> None:
        """Record one variant. Unknown contigs are ignored: a VCF can carry
        records for a contig absent from its own header, and dropping them
        from the plot is better than raising on an otherwise-usable file."""
        stats = self._per_contig.get(contig)
        if stats is None:
            return

        stats["variants"] += 1
        # Classified by allele, the same way `bcftools stats` classifies a
        # site for its SN indel/SNP totals -- a multiallelic ALT is one site
        # with several alleles, not several independent events, so every
        # allele has to agree before the site earns a bucket. All alleles a
        # single base against a single-base REF is a (possibly multiallelic)
        # SNP site; any allele whose length differs from REF makes it an
        # indel site, since one true indel allele is enough to shift the
        # contig's indel density. A same-length multi-base substitution is an
        # MNP in bcftools' vocabulary and lands in neither bucket, counted
        # only in the total -- matching this to bcftools is what keeps the
        # per-contig table's sums equal to the summary row above it instead
        # of quietly drifting apart on any file with multiallelic sites.
        alleles = alt.split(",")
        if len(ref) == 1 and all(len(a) == 1 for a in alleles):
            stats["snps"] += 1
        elif any(len(a) != len(ref) for a in alleles):
            stats["indels"] += 1

        geom = self._geometry.get(contig)
        if geom is None:
            return
        start_bin, positions_per_bin = geom
        offset = min(
            int((pos - 1) / positions_per_bin), max(self._counts[contig], 1) - 1
        )
        self._bins[start_bin + offset] += 1

    def bins(self) -> list[int]:
        return self._bins

    def boundaries(self) -> list[dict]:
        return self._boundaries

    def contigs(self) -> list[dict]:
        """Per-contig counts, ordered as the VCF header declares them.

        Header order rather than descending count: contigs have meaningful
        names a person scans for (chr1, chr2, ...), unlike BAM's per-contig
        table where the interesting ones are whichever got the most reads.
        """
        out = []
        for name in self._order:
            length = self._lengths.get(name, 0)
            stats = self._per_contig[name]
            out.append(
                {
                    "contig": name,
                    "length": length,
                    "variants": stats["variants"],
                    "snps": stats["snps"],
                    "indels": stats["indels"],
                    "per_kb": (
                        round(1000 * stats["variants"] / length, 3) if length else 0.0
                    ),
                }
            )
        return out
