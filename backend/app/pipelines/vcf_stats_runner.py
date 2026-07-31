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
