# Variant Results Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Results tab for VCF/BCF files showing call-set summary statistics and a filterable, paginated browser of individual variants.

**Architecture:** Follows the BAM Results tab exactly: a read-only compute job runs `bcftools stats` and `bcftools query`, returns bounded summary numbers as `facts` merged onto the object, and writes the per-variant detail to disk. The one deliberate departure is that the per-variant detail goes into an indexed SQLite database rather than a flat TSV — a plant resequencing VCF holds millions of rows, where the BAM tab's read-whole-file-and-slice approach costs 440 MB of RSS per request (see the spec's finding 5).

**Tech Stack:** Python 3.12 / FastAPI / Beanie(MongoDB) / SQLite (stdlib `sqlite3`) / pytest — React 18 / TypeScript / TanStack Query / Vite

**Spec:** `docs/superpowers/specs/2026-07-30-variant-results-tab-design.md`

---

## Orientation for someone new to this repo

Read this before Task 1; it will save you an hour.

**Running anything.** The app runs as one Docker Compose stack, always from the
main repo root — never from a worktree, because the bind mounts are relative
paths and running from a worktree silently repoints the shared stack:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

**Tests run inside the container**, not a host venv (the host venv hits Mongo
replica-set errors):

```bash
docker compose exec api python -m pytest tests/ -q
```

**The worker does not hot-reload.** `api` runs `uvicorn --reload` and `web`
runs `vite dev`, so their changes land on the next request. `worker` runs
`python -m app.worker_main` directly and keeps executing whatever it loaded at
process start. After changing any handler, run:

```bash
docker compose restart worker
```

Skipping this makes a job appear to run with your fix while silently executing
the old code — which reads as "the fix didn't work".

**How a pipeline job flows**, which is the shape every backend task here fills
in:

1. `api/v1/pipelines.py` — HTTP route, validates the request body
2. `services/pipeline_service.py` — checks preconditions, resolves blobs, enqueues
3. `queue/*_handlers.py` — runs in a worker **thread**, so it cannot touch the
   database; it returns a plain dict
4. `queue/results.py` — runs on the event loop, merges that dict into MongoDB

**The reference implementation for everything here** is the BAM Results
feature. When a step is ambiguous, read these and mirror them:
`pipelines/bam_stats_runner.py`, `queue/align_handlers.py:432` (`run_bam_stats`),
`services/pipeline_service.py:1267` (`launch_bam_stats`),
`queue/results.py:1020` (`_apply_run_bam_stats`), `components/BamResults.tsx`,
`components/ContigTable.tsx`.

**Test data already in the database.** Three VCFs exist. Use them:

| File | Variants | Notes |
| --- | --- | --- |
| `DRR1066343.bcftools.vcf.gz` | 6,641 | 17 contigs, *S. cerevisiae*; FILTER is all `.` |
| `DRR1078403.clair3.vcf.gz` | 1 | one `RefCall` record |
| `DRR1078403.bcftools.vcf.gz` | 0 | empty — a real, normal case |

The two near-empty files are not broken; a strict caller producing nothing is
an ordinary outcome and the code must render it rather than fail.

---

## File Structure

**Create:**

| Path | Responsibility |
| --- | --- |
| `backend/app/pipelines/vcf_stats_runner.py` | Command construction, `bcftools stats` parsing, re-binning, summary derivation. Pure functions over strings. |
| `backend/app/pipelines/variant_db.py` | Building and querying the SQLite variant database. Isolated because it is the only stateful part and the only place SQL lives. |
| `backend/tests/pipelines/test_vcf_stats_runner.py` | Tests for the above parsing/derivation. |
| `backend/tests/pipelines/test_variant_db.py` | Tests for build, indexes, filtering, and the memory bound. |
| `backend/tests/pipelines/test_vcf_stats_launch.py` | Precondition and tool-gate tests for the launcher. |
| `backend/tests/api/test_vcf_stats_report.py` | Pagination route: filters, counting, path containment. |
| `frontend/src/components/VariantResults.tsx` | The tab: compute prompt, summary sections, provenance. |
| `frontend/src/components/VariantCharts.tsx` | Density strip and QUAL/DP histograms as inline SVG. |
| `frontend/src/components/VariantTable.tsx` | Paginated, filtered variant table. |

**Modify:**

| Path | Change |
| --- | --- |
| `backend/app/config.py` | Add `vcf_stats_dir` property. |
| `backend/app/pipelines/bam_stats_runner.py` | Extract `allocate_bins` out of `bin_depth`. |
| `backend/app/queue/variant_handlers.py` | Add the `run_vcf_stats` handler. |
| `backend/app/services/pipeline_service.py` | Add `launch_vcf_stats` + `_check_vcf_stats_callable`. |
| `backend/app/queue/results.py` | Add `_apply_run_vcf_stats`, register in `_APPLIERS`. |
| `backend/app/api/v1/pipelines.py` | Add the launch and paginated-report routes. |
| `frontend/src/api/types.ts` | Add the fact and page types. |
| `frontend/src/api/client.ts` | Add `launchVcfStats`, `vcfStatsVariants`, `vcfStatsDownloadUrl`. |
| `frontend/src/components/DetailPanel.tsx` | Widen the Results tab condition; dispatch on format kind. |

---

## Task 1: Add the `vcf_stats_dir` setting

**Files:**
- Modify: `backend/app/config.py`
- Test: `backend/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_config.py`:

```python
def test_vcf_stats_dir_sits_beside_bam_stats(tmp_path, monkeypatch):
    """Derived, regenerable report data -- outside objects/, like bam_stats."""
    from app.config import Settings

    s = Settings(bioinfo_home=str(tmp_path))
    assert s.vcf_stats_dir == tmp_path / "vcf_stats"
    assert "objects" not in s.vcf_stats_dir.parts
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/test_config.py::test_vcf_stats_dir_sits_beside_bam_stats -v
```

Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'vcf_stats_dir'`

- [ ] **Step 3: Write the implementation**

In `backend/app/config.py`, directly after the `bam_stats_dir` property:

```python
    @property
    def vcf_stats_dir(self) -> Path:
        """Generated Variant Results artifacts (the variants TSV and the
        SQLite database the table queries), keyed by object id.

        Outside objects/ deliberately, same rationale as bam_stats_dir: both
        are derivative and regenerable from the VCF itself, so content-
        addressing them would buy deduplication of something never shared and
        cost a blob record per run.
        """
        return self.bioinfo_home / "vcf_stats"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/test_config.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_config.py
git commit -m "feat: add the vcf_stats_dir setting"
```

---

## Task 2: Extract `allocate_bins` from `bin_depth`

The density strip needs *counts* per bin; `bin_depth` computes a *mean*. The
reusable part is the bin allocation — proportional bins with a one-bin floor so
short contigs never vanish. Extract it without changing BAM behaviour.

**Files:**
- Modify: `backend/app/pipelines/bam_stats_runner.py:143-215`
- Test: `backend/tests/pipelines/test_bam_stats_runner.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_bam_stats_runner.py`:

```python
from app.pipelines.bam_stats_runner import allocate_bins


class TestAllocateBins:
    def test_bins_sum_to_exactly_bin_count(self):
        """Rounding must never leave the total short or over."""
        geometry, boundaries, counts = allocate_bins(
            contig_lengths=[("chr1", 1000), ("chr2", 3000), ("chr3", 17)],
            bin_count=100,
        )
        assert sum(counts.values()) == 100

    def test_short_contig_still_gets_a_bin(self):
        """A 17bp contig beside a 3Mb one must not vanish from the plot."""
        _, _, counts = allocate_bins(
            contig_lengths=[("big", 3_000_000), ("tiny", 17)], bin_count=10
        )
        assert counts["tiny"] >= 1

    def test_boundaries_mark_each_contig_start(self):
        _, boundaries, _ = allocate_bins(
            contig_lengths=[("chr1", 100), ("chr2", 100)], bin_count=10
        )
        assert boundaries[0] == {"contig": "chr1", "bin_start": 0}
        assert boundaries[1]["contig"] == "chr2"
        assert boundaries[1]["bin_start"] > 0

    def test_empty_input_returns_empty(self):
        geometry, boundaries, counts = allocate_bins(
            contig_lengths=[], bin_count=100
        )
        assert geometry == {} and boundaries == [] and counts == {}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py::TestAllocateBins -v
```

Expected: FAIL — `ImportError: cannot import name 'allocate_bins'`

- [ ] **Step 3: Write the implementation**

In `backend/app/pipelines/bam_stats_runner.py`, add this function immediately
*before* `bin_depth`:

```python
def allocate_bins(
    *,
    contig_lengths: list[tuple[str, int]],
    bin_count: int,
) -> tuple[dict[str, tuple[int, float]], list[dict], dict[str, int]]:
    """Lay contigs end to end across a fixed number of bins.

    Bins are allocated proportionally to each contig's length, with one floor:
    every contig gets at least one bin regardless of its share, so a short
    scaffold is never averaged away into a neighbour's bin or omitted
    entirely. Rounding discrepancies are absorbed by the last contig, so the
    allocation always sums to exactly `bin_count`.

    Extracted from `bin_depth` so variant density (which counts per bin) and
    read depth (which averages per bin) share one definition of where a
    contig sits on the axis. Returns `(geometry, boundaries, counts)`:
    `geometry` maps contig -> (start_bin, positions_per_bin), `boundaries`
    marks which bin index starts each contig for drawing separators, and
    `counts` maps contig -> how many bins it was given.
    """
    total_length = sum(length for _, length in contig_lengths)
    if total_length <= 0 or bin_count <= 0:
        return {}, [], {}

    n = len(contig_lengths)
    floor_bins = min(bin_count, n)
    remaining_bins = bin_count - floor_bins

    contig_bin_counts: dict[str, int] = {}
    for name, length in contig_lengths:
        share = round(remaining_bins * length / total_length) if total_length else 0
        contig_bin_counts[name] = 1 + share

    allocated = sum(contig_bin_counts.values())
    if allocated != bin_count and contig_lengths:
        last_name = contig_lengths[-1][0]
        contig_bin_counts[last_name] += bin_count - allocated

    geometry: dict[str, tuple[int, float]] = {}
    boundaries = []
    offset = 0
    for name, length in contig_lengths:
        bins_for_contig = contig_bin_counts[name]
        positions_per_bin = max(length / bins_for_contig, 1)
        geometry[name] = (offset, positions_per_bin)
        boundaries.append({"contig": name, "bin_start": offset})
        offset += bins_for_contig

    return geometry, boundaries, contig_bin_counts
```

Then replace the body of `bin_depth` between its docstring and the
`bin_sum = [0.0] * bin_count` line with a call to it. The whole block from
`total_length = sum(...)` down to the end of the `for name, length in
contig_lengths:` loop becomes:

```python
    geometry, boundaries, contig_bin_counts = allocate_bins(
        contig_lengths=contig_lengths, bin_count=bin_count
    )
    if not geometry:
        return [], []
```

The rest of `bin_depth` (from `bin_sum = [0.0] * bin_count` onward) is
unchanged.

- [ ] **Step 4: Run the whole BAM suite to verify nothing regressed**

```bash
docker compose exec api python -m pytest tests/pipelines/test_bam_stats_runner.py -v
```

Expected: PASS — all pre-existing `bin_depth` tests included. Those tests are
the real check here: they pin the behaviour the extraction must preserve.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/bam_stats_runner.py backend/tests/pipelines/test_bam_stats_runner.py
git commit -m "refactor: extract allocate_bins so density and depth share bin geometry"
```

---

## Task 3: Parse `bcftools stats` output

**Files:**
- Create: `backend/app/pipelines/vcf_stats_runner.py`
- Test: `backend/tests/pipelines/test_vcf_stats_runner.py`

The fixture text below is **real output** captured from
`DRR1066343.bcftools.vcf.gz` in the running container. Do not invent values.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_vcf_stats_runner.py`:

```python
"""Command construction and output parsing for VCF results statistics.

Pure functions over strings and paths, mirroring bam_stats_runner.py.

The STATS fixture is real `bcftools stats` output captured from
DRR1066343.bcftools.vcf.gz (6,641 variants, 17 contigs, S. cerevisiae).
"""

from pathlib import Path

from app.pipelines.vcf_stats_runner import (
    build_query_command,
    build_stats_command,
    parse_stats,
)

STATS = """# This file was produced by bcftools stats
# SN\t[2]id\t[3]key\t[4]value
SN\t0\tnumber of samples:\t1
SN\t0\tnumber of records:\t6641
SN\t0\tnumber of no-ALTs:\t0
SN\t0\tnumber of SNPs:\t6157
SN\t0\tnumber of MNPs:\t0
SN\t0\tnumber of indels:\t484
SN\t0\tnumber of others:\t0
SN\t0\tnumber of multiallelic sites:\t22
SN\t0\tnumber of multiallelic SNP sites:\t0
# TSTV\t[2]id\t[3]ts\t[4]tv\t[5]ts/tv
TSTV\t0\t4358\t1799\t2.42\t4358\t1799\t2.42
# ST\t[2]id\t[3]type\t[4]count
ST\t0\tA>C\t221
ST\t0\tA>G\t1109
ST\t0\tC>T\t1097
# QUAL\t[2]id\t[3]Quality\t[4]number of SNPs
QUAL\t0\t3.0\t2\t1\t1\t2
QUAL\t0\t3.1\t2\t1\t1\t4
QUAL\t0\t50.5\t19\t11\t8\t1
# DP\t[2]id\t[3]bin\t[4]number of genotypes
DP\t0\t1\t0\t0.000000\t1099\t16.548713
DP\t0\t2\t0\t0.000000\t753\t11.338654
# IDD\t[2]id\t[3]length
IDD\t0\t-2\t14\t0\t0.00
IDD\t0\t1\t31\t0\t0.00
"""

EMPTY_STATS = """# This file was produced by bcftools stats
# SN\t[2]id\t[3]key\t[4]value
SN\t0\tnumber of samples:\t1
SN\t0\tnumber of records:\t0
SN\t0\tnumber of SNPs:\t0
SN\t0\tnumber of indels:\t0
# TSTV\t[2]id\t[3]ts\t[4]tv\t[5]ts/tv
TSTV\t0\t0\t0\t0.00\t0\t0\t0.00
"""


class TestCommandConstruction:
    def test_stats_command(self):
        cmd = build_stats_command(
            bcftools_path="/usr/bin/bcftools", vcf=Path("/work/a.vcf.gz")
        )
        assert cmd == ["/usr/bin/bcftools", "stats", "/work/a.vcf.gz"]

    def test_query_command_emits_the_table_columns(self):
        """The format string is the schema: its field order must match
        VARIANT_COLUMNS or the database is populated with shifted values."""
        cmd = build_query_command(
            bcftools_path="/usr/bin/bcftools", vcf=Path("/work/a.vcf.gz")
        )
        assert cmd[:3] == ["/usr/bin/bcftools", "query", "-f"]
        # Real tabs and a real newline, not the two-character sequences. Write
        # this assertion with actual escapes -- "\t" not "\\t" -- because a
        # literal backslash-t makes bcftools emit one unsplittable column and
        # every row lands in the database as a single field.
        assert cmd[3] == "%CHROM\t%POS\t%REF\t%ALT\t%QUAL\t%FILTER\t%INFO/DP[\t%GT]\n"
        assert cmd[4] == "/work/a.vcf.gz"

    def test_query_format_separator_is_a_real_tab(self):
        """Guards the escaping directly: verified against bcftools 1.21, this
        format yields exactly 8 tab-separated columns."""
        from app.pipelines.vcf_stats_runner import QUERY_FORMAT

        assert "\t" in QUERY_FORMAT
        assert "\\t" not in QUERY_FORMAT
        assert QUERY_FORMAT.endswith("\n")


class TestParseStats:
    def test_sn_section_becomes_typed_counts(self):
        out = parse_stats(STATS)
        assert out["sn"]["records"] == 6641
        assert out["sn"]["snps"] == 6157
        assert out["sn"]["indels"] == 484
        assert out["sn"]["samples"] == 1
        assert out["sn"]["multiallelic_sites"] == 22

    def test_tstv_section(self):
        out = parse_stats(STATS)
        assert out["tstv"] == {"ts": 4358, "tv": 1799, "ti_tv": 2.42}

    def test_substitutions_preserve_order_and_counts(self):
        out = parse_stats(STATS)
        assert out["st"] == [
            {"type": "A>C", "count": 221},
            {"type": "A>G", "count": 1109},
            {"type": "C>T", "count": 1097},
        ]

    def test_qual_rows_are_value_and_count(self):
        """Column 4 is the SNP count at that QUAL; the trailing columns are
        transitions/transversions/indels, which the histogram does not use."""
        out = parse_stats(STATS)
        assert out["qual"] == [
            {"qual": 3.0, "count": 2},
            {"qual": 3.1, "count": 2},
            {"qual": 50.5, "count": 19},
        ]

    def test_dp_rows_use_the_number_of_sites_column(self):
        """Column 6 is number of sites. Column 4 is number of genotypes, which
        is 0 for a file bcftools did not genotype -- using it would draw an
        empty depth chart for a file that plainly has depth."""
        out = parse_stats(STATS)
        assert out["dp"] == [
            {"depth": 1, "count": 1099},
            {"depth": 2, "count": 753},
        ]

    def test_idd_rows(self):
        out = parse_stats(STATS)
        assert out["idd"] == [
            {"length": -2, "count": 14},
            {"length": 1, "count": 31},
        ]

    def test_comment_lines_are_skipped(self):
        out = parse_stats(STATS)
        assert all(not k.startswith("#") for k in out)

    def test_empty_vcf_parses_to_zeroes_rather_than_raising(self):
        """Two of the three VCFs in the live database hold 0 and 1 records.
        bcftools exits 0 and emits headers with no rows; that is a normal
        outcome of a strict caller, not a failure."""
        out = parse_stats(EMPTY_STATS)
        assert out["sn"]["records"] == 0
        assert out["st"] == []
        assert out["qual"] == []
        assert out["dp"] == []

    def test_unknown_sections_are_ignored(self):
        """bcftools adds sections between releases; an unrecognised one must
        not break parsing."""
        out = parse_stats(STATS + "HWE\t0\t1\t2\t3\n")
        assert out["sn"]["records"] == 6641
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_vcf_stats_runner.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.vcf_stats_runner'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/vcf_stats_runner.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_vcf_stats_runner.py -v
```

Expected: PASS — 13 tests

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/vcf_stats_runner.py backend/tests/pipelines/test_vcf_stats_runner.py
git commit -m "feat: parse bcftools stats output for the Variant Results tab"
```

---

## Task 4: Re-bin the QUAL and DP distributions

The real file yields 805 QUAL rows and 211 DP rows — one per distinct value,
not a histogram. Storing them raw would bloat `facts` and render as noise.

**Files:**
- Modify: `backend/app/pipelines/vcf_stats_runner.py`
- Test: `backend/tests/pipelines/test_vcf_stats_runner.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_vcf_stats_runner.py`:

```python
from app.pipelines.vcf_stats_runner import rebin_distribution


class TestRebinDistribution:
    def test_collapses_to_at_most_bucket_count(self):
        """805 QUAL rows on the real test file; facts stores a histogram."""
        rows = [{"qual": float(i), "count": 1} for i in range(805)]
        out = rebin_distribution(rows, value_key="qual", bucket_count=20)
        assert len(out) <= 20

    def test_preserves_the_total_count(self):
        """Re-binning redistributes; it must not lose or invent observations."""
        rows = [{"qual": float(i), "count": i} for i in range(100)]
        out = rebin_distribution(rows, value_key="qual", bucket_count=10)
        assert sum(b["count"] for b in out) == sum(r["count"] for r in rows)

    def test_buckets_span_the_observed_range(self):
        rows = [{"qual": 10.0, "count": 1}, {"qual": 90.0, "count": 1}]
        out = rebin_distribution(rows, value_key="qual", bucket_count=8)
        assert out[0]["value"] == 10.0
        assert out[-1]["value"] <= 90.0
        assert sum(b["count"] for b in out) == 2

    def test_single_distinct_value_yields_one_bucket(self):
        """A file where every record has the same QUAL must not divide by a
        zero-width range."""
        rows = [{"qual": 50.0, "count": 7}]
        out = rebin_distribution(rows, value_key="qual", bucket_count=20)
        assert out == [{"value": 50.0, "count": 7}]

    def test_empty_input_returns_empty(self):
        assert rebin_distribution([], value_key="qual", bucket_count=20) == []

    def test_fewer_rows_than_buckets_passes_through(self):
        rows = [{"depth": 1, "count": 5}, {"depth": 2, "count": 3}]
        out = rebin_distribution(rows, value_key="depth", bucket_count=20)
        assert sum(b["count"] for b in out) == 8
        assert len(out) == 2
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_vcf_stats_runner.py::TestRebinDistribution -v
```

Expected: FAIL — `ImportError: cannot import name 'rebin_distribution'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/pipelines/vcf_stats_runner.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_vcf_stats_runner.py -v
```

Expected: PASS — 19 tests

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/vcf_stats_runner.py backend/tests/pipelines/test_vcf_stats_runner.py
git commit -m "feat: re-bin the QUAL and DP distributions into fixed histograms"
```

---

## Task 5: Derive the summary, with a conditional PASS rate

Finding 3 in the spec: FILTER is `.` on every record of the real test file —
bcftools call does not stamp PASS. Reporting "100% PASS" or "0% PASS" for such
a file both misstate it, so the rate is omitted entirely when the file does not
use FILTER.

**Files:**
- Modify: `backend/app/pipelines/vcf_stats_runner.py`
- Test: `backend/tests/pipelines/test_vcf_stats_runner.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_vcf_stats_runner.py`:

```python
from app.pipelines.vcf_stats_runner import variant_summary


class TestVariantSummary:
    def test_headline_counts_come_from_the_sn_section(self):
        out = variant_summary(parse_stats(STATS), filter_counts={".": 6641})
        assert out["variants"] == 6641
        assert out["snps"] == 6157
        assert out["indels"] == 484
        assert out["ti_tv"] == 2.42

    def test_pass_rate_is_absent_when_the_file_does_not_use_filter(self):
        """Every record in the real bcftools test file has FILTER='.'. A
        '100% PASS' headline on a file where nothing was ever filtered is
        misleading, so the rate is omitted rather than guessed."""
        out = variant_summary(parse_stats(STATS), filter_counts={".": 6641})
        assert "pass_pct" not in out
        assert out["no_filter_count"] == 6641

    def test_pass_rate_is_reported_when_the_file_does_use_filter(self):
        """The rate is PASS over the record count from the SN section, not
        over the sum of filter_counts -- those agree for a well-formed file,
        and SN is the authority on how many records there are."""
        out = variant_summary(
            parse_stats(STATS),
            filter_counts={"PASS": 6000, "LowQual": 600, "RefCall": 41},
        )
        assert out["pass_count"] == 6000
        # 6000 / 6641 records
        assert out["pass_pct"] == 90.35
        assert out["no_filter_count"] == 0

    def test_mixed_filter_and_dot_still_reports_a_rate(self):
        """A partially-filtered file uses FILTER, so the rate is meaningful."""
        out = variant_summary(
            parse_stats(STATS), filter_counts={"PASS": 6000, ".": 641}
        )
        assert out["pass_pct"] == 90.35
        assert out["no_filter_count"] == 641

    def test_empty_vcf_summarises_to_zero_without_dividing(self):
        out = variant_summary(parse_stats(EMPTY_STATS), filter_counts={})
        assert out["variants"] == 0
        assert "pass_pct" not in out
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_vcf_stats_runner.py::TestVariantSummary -v
```

Expected: FAIL — `ImportError: cannot import name 'variant_summary'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/pipelines/vcf_stats_runner.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_vcf_stats_runner.py -v
```

Expected: PASS — 24 tests

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/vcf_stats_runner.py backend/tests/pipelines/test_vcf_stats_runner.py
git commit -m "feat: derive the variant summary with a conditional PASS rate"
```

---

## Task 6: Build the SQLite variant database

This is the departure from the BAM tab. The build must **stream** — at 32M
variants (wheat) a list of rows would exhaust the container.

**Files:**
- Create: `backend/app/pipelines/variant_db.py`
- Test: `backend/tests/pipelines/test_variant_db.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_variant_db.py`:

```python
"""The SQLite database backing the variant table.

Separate from vcf_stats_runner because this is the only stateful part of the
feature and the only place SQL lives.
"""

import sqlite3

import pytest

from app.pipelines.variant_db import (
    VariantFilters,
    build_variant_db,
    count_variants,
    query_variants,
)


def _rows():
    """Three contigs, mixed FILTER values, SNPs and one indel."""
    return iter(
        [
            "chr1\t100\tA\tG\t50.0\tPASS\t30\t0/1",
            "chr1\t200\tC\tT\t10.0\tLowQual\t8\t0/1",
            "chr1\t300\tG\tGTT\t80.0\tPASS\t44\t1/1",
            "chr2\t150\tT\tC\t95.0\tPASS\t50\t1/1",
            "chr2\t250\tA\tT\t20.0\t.\t12\t0/1",
            "chr3\t50\tCTT\tC\t60.0\tPASS\t33\t0/1",
        ]
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "variants.db"
    build_variant_db(rows=_rows(), db_path=path)
    return path


class TestBuild:
    def test_all_rows_land(self, db):
        assert count_variants(db_path=db, filters=VariantFilters()) == 6

    def test_both_indexes_exist(self, db):
        """A missing index turns a 0.3ms query into a full scan with no other
        symptom -- nothing else in the suite would catch it."""
        con = sqlite3.connect(db)
        names = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "ix_variants_locus" in names
        assert "ix_variants_filter" in names

    def test_the_locus_index_is_actually_used(self, db):
        """Asserting the index exists is not the same as it being chosen."""
        con = sqlite3.connect(db)
        plan = " ".join(
            str(r)
            for r in con.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM variants "
                "WHERE chrom=? AND pos BETWEEN ? AND ?",
                ("chr1", 1, 500),
            )
        )
        assert "ix_variants_locus" in plan

    def test_numeric_columns_are_typed_not_text(self, db):
        """Stored as TEXT, `qual >= 30` compares lexically: '9.0' > '80.0'."""
        con = sqlite3.connect(db)
        row = con.execute(
            "SELECT pos, qual, dp FROM variants WHERE chrom='chr1' AND pos=100"
        ).fetchone()
        assert row == (100, 50.0, 30)

    def test_missing_dp_and_qual_become_null_not_zero(self, tmp_path):
        """bcftools emits '.' for an absent value. Storing 0 would place the
        record at the bottom of a depth chart rather than out of it."""
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(["chr1\t10\tA\tG\t.\tPASS\t.\t0/1"]), db_path=path
        )
        con = sqlite3.connect(path)
        assert con.execute("SELECT qual, dp FROM variants").fetchone() == (None, None)

    def test_every_sample_genotype_is_kept(self, tmp_path):
        """`[\\t%GT]` emits one column per sample. Storing only parts[7] drops
        samples 2..n and leaves the picker showing sample 1's genotype for
        every selection -- wrong data, presented confidently. Invisible on
        the single-sample files this pipeline produces, which is exactly why
        it needs pinning."""
        path = tmp_path / "multi.db"
        build_variant_db(
            rows=iter(["chr1\t100\tA\tG\t50.0\tPASS\t30\t0/1\t1/1\t0/0"]),
            db_path=path,
        )
        row = query_variants(
            db_path=path, filters=VariantFilters(), offset=0, limit=1
        )[0]
        assert row["gt"].split("\t") == ["0/1", "1/1", "0/0"]

    def test_empty_input_produces_a_valid_empty_database(self, tmp_path):
        """Two of the three VCFs in the live database are empty or near-empty."""
        path = tmp_path / "empty.db"
        build_variant_db(rows=iter([]), db_path=path)
        assert count_variants(db_path=path, filters=VariantFilters()) == 0

    def test_malformed_line_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "v.db"
        build_variant_db(
            rows=iter(["chr1\t10\tA\tG\t5.0\tPASS\t3\t0/1", "garbage"]),
            db_path=path,
        )
        assert count_variants(db_path=path, filters=VariantFilters()) == 1


class TestQuery:
    def test_pagination_slices_in_locus_order(self, db):
        page = query_variants(db_path=db, filters=VariantFilters(), offset=0, limit=2)
        assert [r["chrom"] for r in page] == ["chr1", "chr1"]
        assert [r["pos"] for r in page] == [100, 200]

        page2 = query_variants(db_path=db, filters=VariantFilters(), offset=2, limit=2)
        assert [r["pos"] for r in page2] == [300, 150]

    def test_rows_are_dicts_keyed_by_column(self, db):
        row = query_variants(
            db_path=db, filters=VariantFilters(), offset=0, limit=1
        )[0]
        assert row == {
            "chrom": "chr1",
            "pos": 100,
            "ref": "A",
            "alt": "G",
            "qual": 50.0,
            "filter": "PASS",
            "dp": 30,
            "gt": "0/1",
        }

    def test_filter_by_contig(self, db):
        f = VariantFilters(contig="chr2")
        assert count_variants(db_path=db, filters=f) == 2

    def test_filter_by_position_range(self, db):
        f = VariantFilters(contig="chr1", pos_min=150, pos_max=350)
        rows = query_variants(db_path=db, filters=f, offset=0, limit=10)
        assert [r["pos"] for r in rows] == [200, 300]

    def test_filter_by_filter_value(self, db):
        f = VariantFilters(filter_value="PASS")
        assert count_variants(db_path=db, filters=f) == 4

    def test_filter_by_min_qual(self, db):
        f = VariantFilters(min_qual=60.0)
        rows = query_variants(db_path=db, filters=f, offset=0, limit=10)
        assert sorted(r["qual"] for r in rows) == [60.0, 80.0, 95.0]

    def test_filter_by_type_snp_excludes_indels(self, db):
        """A SNP is a single-base REF and ALT; length differences are indels."""
        f = VariantFilters(variant_type="snp")
        rows = query_variants(db_path=db, filters=f, offset=0, limit=10)
        assert all(len(r["ref"]) == 1 and len(r["alt"]) == 1 for r in rows)
        assert len(rows) == 4

    def test_filter_by_type_indel(self, db):
        f = VariantFilters(variant_type="indel")
        rows = query_variants(db_path=db, filters=f, offset=0, limit=10)
        assert {r["pos"] for r in rows} == {300, 50}

    def test_filters_combine(self, db):
        f = VariantFilters(contig="chr1", filter_value="PASS", min_qual=60.0)
        rows = query_variants(db_path=db, filters=f, offset=0, limit=10)
        assert [r["pos"] for r in rows] == [300]

    def test_filter_matching_nothing_returns_empty_not_error(self, db):
        f = VariantFilters(contig="chrX")
        assert query_variants(db_path=db, filters=f, offset=0, limit=10) == []
        assert count_variants(db_path=db, filters=f) == 0

    def test_filter_values_are_parameterized(self, db):
        """A contig name is user input reaching a WHERE clause."""
        f = VariantFilters(contig="chr1'; DROP TABLE variants; --")
        assert count_variants(db_path=db, filters=f) == 0
        assert count_variants(db_path=db, filters=VariantFilters()) == 6
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_variant_db.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.variant_db'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/pipelines/variant_db.py`:

```python
"""The SQLite database backing the variant table.

Why a database rather than the flat TSV the BAM Results tab paginates: this
tool targets plant genomes, where a resequencing VCF holds millions of calls.
Benchmarked on a synthetic 5M-variant file in the api container, reading the
whole TSV and slicing it in Python costs ~440 MB of RSS per in-flight request
and ~0.9s per page, which projects to ~2.8 GB for wheat. The same data in
SQLite with indexes on (chrom, pos) and filter answers a filtered page in
0.2-0.4ms using 14 MB.

The build is deliberately streaming: at 32M variants a list of rows would
exhaust the container before a single row was written.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

# Batched inserts. Large enough that per-statement overhead disappears, small
# enough that the pending batch is never a meaningful fraction of memory.
_INSERT_BATCH = 10_000


@dataclass(frozen=True)
class VariantFilters:
    """What the table is currently showing.

    One object rather than loose arguments so `query_variants` and
    `count_variants` cannot drift apart about what is being filtered -- the
    page and its total have to agree or pagination silently misreports.
    """

    contig: str | None = None
    pos_min: int | None = None
    pos_max: int | None = None
    filter_value: str | None = None
    variant_type: str | None = None  # "snp" | "indel"
    min_qual: float | None = None


def _num(value: str) -> float | None:
    """A bcftools numeric field, which is '.' when absent.

    None rather than 0: an absent depth is not a depth of zero, and storing it
    as one would place the record at the bottom of a depth chart rather than
    out of it.
    """
    if value == "." or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def build_variant_db(*, rows, db_path: Path) -> int:
    """Stream parsed `bcftools query` lines into an indexed SQLite database.

    `rows` is consumed once, in the order bcftools emits it (locus order), and
    is never materialized -- see the module docstring.

    Indexes are built *after* the bulk insert: creating them first makes every
    insert maintain a B-tree and turns a 7-second load into minutes. Journaling
    and synchronous writes are off because this file is a derived artifact
    rebuilt from the VCF on demand, so durability buys nothing.

    Returns the number of rows inserted.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute(
            """
            CREATE TABLE variants (
              chrom  TEXT,
              pos    INTEGER,
              ref    TEXT,
              alt    TEXT,
              qual   REAL,
              filter TEXT,
              dp     INTEGER,
              gt     TEXT
            )
            """
        )

        inserted = 0
        skipped = 0
        batch: list[tuple] = []
        for line in rows:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 8:
                skipped += 1
                continue
            try:
                pos = int(parts[1])
            except ValueError:
                skipped += 1
                continue
            qual = _num(parts[4])
            dp = _num(parts[6])
            # Every column from 7 on is one sample's genotype -- the query
            # format's `[\t%GT]` repeats per sample. Rejoined rather than
            # taking parts[7] alone, which would silently drop samples 2..n
            # and leave the table showing sample 1's genotype whichever
            # sample the picker selects. The frontend splits this back apart
            # by index.
            batch.append(
                (
                    parts[0],
                    pos,
                    parts[2],
                    parts[3],
                    qual,
                    parts[5],
                    int(dp) if dp is not None else None,
                    "\t".join(parts[7:]),
                )
            )
            if len(batch) >= _INSERT_BATCH:
                con.executemany(
                    "INSERT INTO variants VALUES (?,?,?,?,?,?,?,?)", batch
                )
                inserted += len(batch)
                batch = []

        if batch:
            con.executemany("INSERT INTO variants VALUES (?,?,?,?,?,?,?,?)", batch)
            inserted += len(batch)

        con.execute("CREATE INDEX ix_variants_locus ON variants(chrom, pos)")
        con.execute("CREATE INDEX ix_variants_filter ON variants(filter)")
        con.commit()
    finally:
        con.close()

    if skipped:
        log.warning("variant_db_skipped_lines", count=skipped, db=str(db_path))
    return inserted


def _where(filters: VariantFilters) -> tuple[str, list]:
    """The WHERE clause and its bound parameters.

    Every value is bound, never interpolated: these come from query string
    arguments and reach a SQL statement directly.
    """
    clauses: list[str] = []
    args: list = []

    if filters.contig:
        clauses.append("chrom = ?")
        args.append(filters.contig)
    if filters.pos_min is not None:
        clauses.append("pos >= ?")
        args.append(filters.pos_min)
    if filters.pos_max is not None:
        clauses.append("pos <= ?")
        args.append(filters.pos_max)
    if filters.filter_value:
        clauses.append("filter = ?")
        args.append(filters.filter_value)
    if filters.min_qual is not None:
        clauses.append("qual >= ?")
        args.append(filters.min_qual)
    if filters.variant_type == "snp":
        clauses.append("length(ref) = 1 AND length(alt) = 1")
    elif filters.variant_type == "indel":
        clauses.append("length(ref) <> length(alt)")

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), args


def _connect(db_path: Path) -> sqlite3.Connection:
    """Read-only. Nothing but the compute job ever writes, and SQLite handles
    concurrent readers without coordination."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def query_variants(
    *, db_path: Path, filters: VariantFilters, offset: int, limit: int
) -> list[dict]:
    """One page of the table, in locus order.

    Ordered by rowid rather than an ORDER BY on (chrom, pos): bcftools query
    emits records in locus order already, so insertion order *is* locus order,
    and sorting explicitly would cost a scan on every page.
    """
    where, args = _where(filters)
    con = _connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            f"SELECT chrom,pos,ref,alt,qual,filter,dp,gt FROM variants{where} "
            f"LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def count_variants(*, db_path: Path, filters: VariantFilters) -> int:
    """How many rows match. See the route: this is not recomputed on every
    page turn, because a combined predicate costs ~400ms at 5M rows."""
    where, args = _where(filters)
    con = _connect(db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM variants{where}", args).fetchone()[0]
    finally:
        con.close()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_variant_db.py -v
```

Expected: PASS — 20 tests

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/variant_db.py backend/tests/pipelines/test_variant_db.py
git commit -m "feat: add the indexed SQLite variant database"
```

---

## Task 7: Pin the streaming build against a memory regression

"Collect rows into a list, then insert" is the natural refactor that passes
every test in Task 6 while reintroducing exactly the problem this design
exists to avoid. This test is the one that catches it.

**Files:**
- Test: `backend/tests/pipelines/test_variant_db.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_variant_db.py`:

```python
class TestStreamingBuild:
    def test_build_does_not_materialize_the_input(self, tmp_path):
        """The build must consume its input lazily.

        A generator that raises if fully drained before the first insert would
        be elaborate; instead assert the cheap, direct property -- that the
        function accepts an iterator and never calls list() on it -- by
        counting how many rows exist in memory at peak.
        """
        peak = 0

        def rows():
            nonlocal peak
            for i in range(50_000):
                peak = max(peak, i)
                yield f"chr1\t{i}\tA\tG\t50.0\tPASS\t30\t0/1"

        path = tmp_path / "v.db"
        n = build_variant_db(rows=rows(), db_path=path)
        assert n == 50_000
        assert count_variants(db_path=path, filters=VariantFilters()) == 50_000

    def test_peak_rss_stays_bounded_while_building(self, tmp_path):
        """The regression that matters. 200k rows through a streaming build
        should add tens of MB, not hundreds -- a list-then-insert refactor
        blows past this while every correctness test still passes."""

        def rss_mb() -> float:
            with open("/proc/self/status") as fh:
                return int(fh.read().split("VmRSS:")[1].split()[0]) / 1024

        before = rss_mb()
        path = tmp_path / "big.db"
        build_variant_db(
            rows=(
                f"chr{i % 10}\t{i}\tA\tG\t{i % 200}.0\tPASS\t{i % 90}\t0/1"
                for i in range(200_000)
            ),
            db_path=path,
        )
        growth = rss_mb() - before
        assert growth < 150, f"build grew RSS by {growth:.0f} MB"
```

- [ ] **Step 2: Run the test**

```bash
docker compose exec api python -m pytest tests/pipelines/test_variant_db.py::TestStreamingBuild -v
```

Expected: PASS — Task 6's implementation already streams. This test exists to
fail *later*, if someone refactors the build into a list.

- [ ] **Step 3: Verify the test actually detects the regression**

A guard that cannot fail is worse than none. Temporarily change
`build_variant_db` to materialize its input — add `rows = list(rows)` as the
first line of the function body — and re-run:

```bash
docker compose exec api python -m pytest tests/pipelines/test_variant_db.py::TestStreamingBuild::test_peak_rss_stays_bounded_while_building -v
```

Expected: FAIL with a growth figure over 150 MB. **Then revert that line.**

- [ ] **Step 4: Confirm the revert**

```bash
docker compose exec api python -m pytest tests/pipelines/test_variant_db.py -v
```

Expected: PASS — 22 tests

- [ ] **Step 5: Commit**

```bash
git add backend/tests/pipelines/test_variant_db.py
git commit -m "test: pin the streaming variant-db build against a memory regression"
```

---

## Task 8: Per-contig counts and density bins

**Files:**
- Modify: `backend/app/pipelines/vcf_stats_runner.py`
- Test: `backend/tests/pipelines/test_vcf_stats_runner.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_vcf_stats_runner.py`:

```python
from app.pipelines.vcf_stats_runner import DensityAccumulator


class TestDensityAccumulator:
    def test_counts_variants_per_contig(self):
        acc = DensityAccumulator(
            contig_lengths=[("chr1", 1000), ("chr2", 1000)], bin_count=10
        )
        for pos in (10, 20, 30):
            acc.add("chr1", pos, ref="A", alt="G")
        acc.add("chr2", 10, ref="A", alt="AT")

        contigs = acc.contigs()
        by_name = {c["contig"]: c for c in contigs}
        assert by_name["chr1"]["variants"] == 3
        assert by_name["chr2"]["variants"] == 1

    def test_splits_snps_from_indels(self):
        acc = DensityAccumulator(contig_lengths=[("chr1", 1000)], bin_count=10)
        acc.add("chr1", 10, ref="A", alt="G")
        acc.add("chr1", 20, ref="A", alt="AT")
        by_name = {c["contig"]: c for c in acc.contigs()}
        assert by_name["chr1"]["snps"] == 1
        assert by_name["chr1"]["indels"] == 1

    def test_per_kb_density(self):
        acc = DensityAccumulator(contig_lengths=[("chr1", 2000)], bin_count=10)
        for pos in range(1, 11):
            acc.add("chr1", pos, ref="A", alt="G")
        assert acc.contigs()[0]["per_kb"] == 5.0

    def test_bins_span_the_whole_reference(self):
        acc = DensityAccumulator(
            contig_lengths=[("chr1", 1000), ("chr2", 1000)], bin_count=10
        )
        acc.add("chr1", 1, ref="A", alt="G")
        acc.add("chr2", 999, ref="A", alt="G")
        bins = acc.bins()
        assert len(bins) == 10
        assert bins[0] == 1
        assert bins[-1] == 1

    def test_unknown_contig_is_ignored_not_fatal(self):
        """A VCF can carry records for a contig absent from its own header."""
        acc = DensityAccumulator(contig_lengths=[("chr1", 1000)], bin_count=10)
        acc.add("chrUnplaced", 5, ref="A", alt="G")
        assert sum(acc.bins()) == 0

    def test_no_contig_lengths_yields_empty_rather_than_dividing(self):
        acc = DensityAccumulator(contig_lengths=[], bin_count=10)
        acc.add("chr1", 5, ref="A", alt="G")
        assert acc.bins() == []
        assert acc.contigs() == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_vcf_stats_runner.py::TestDensityAccumulator -v
```

Expected: FAIL — `ImportError: cannot import name 'DensityAccumulator'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/pipelines/vcf_stats_runner.py`, with this import at the
top of the file:

```python
from app.pipelines.bam_stats_runner import allocate_bins
```

```python
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
        # A SNP is a single base substituted for a single base; anything where
        # the lengths differ is an indel. Multi-allelic ALTs (comma-separated)
        # fall into neither and are counted only in the total.
        if len(ref) == 1 and len(alt) == 1:
            stats["snps"] += 1
        elif "," not in alt and len(ref) != len(alt):
            stats["indels"] += 1

        geom = self._geometry.get(contig)
        if geom is None:
            return
        start_bin, positions_per_bin = geom
        offset = min(
            int((pos - 1) / positions_per_bin), self._counts[contig] - 1
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec api python -m pytest tests/pipelines/test_vcf_stats_runner.py -v
```

Expected: PASS — 30 tests

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/vcf_stats_runner.py backend/tests/pipelines/test_vcf_stats_runner.py
git commit -m "feat: accumulate variant density and per-contig counts in one pass"
```

---

## Task 9: The `run_vcf_stats` job handler

**Files:**
- Modify: `backend/app/queue/variant_handlers.py`

This handler runs in a worker thread and cannot touch the database. It returns
a plain dict for `queue/results.py` to merge.

- [ ] **Step 1: Add the handler**

Append to `backend/app/queue/variant_handlers.py`. Add these imports to the
existing import block at the top:

```python
from app.pipelines import variant_db, vcf_stats_runner
```

Then the handler:

```python
@handler(
    "run_vcf_stats",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
)
def run_vcf_stats(ctx: JobContext) -> dict:
    """Summary statistics and the per-variant table for the Results tab.

    Read-only, like run_bam_stats: derives no objects except the regenerable
    TSV and SQLite database. The bounded summary returns as facts for
    `_apply_run_vcf_stats` to merge; the per-variant detail goes to
    settings.vcf_stats_dir and is referenced by filename.

    The query output is consumed as a stream and fed to the database builder
    and the density accumulator together, so a file with tens of millions of
    variants is read once and never held in memory.
    """
    bcftools = tools.require(tools.bcftools())

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("run_vcf_stats requires an 'object_id'")

    work = _prepare_workdir(ctx, "vcf_stats")

    vcf_name = Path(ctx.payload.get("vcf_name") or "variants.vcf.gz").name
    vcf = work / vcf_name
    vcf.unlink(missing_ok=True)
    vcf.symlink_to(_resolve_blob(ctx.payload, "vcf"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="stats", pct=0.1, message="summarising the call set")
    stats_path = work / "stats.txt"
    code = run_subprocess(
        ctx,
        vcf_stats_runner.build_stats_command(bcftools_path=bcftools.path, vcf=vcf),
        log_path=str(stats_path),
    )
    if code != 0:
        raise _failure(code, stats_path, "bcftools stats")
    stats = vcf_stats_runner.parse_stats(stats_path.read_text(errors="replace"))

    ctx.progress(phase="query", pct=0.4, message="extracting variants")
    query_path = work / "variants.tsv"
    code = run_subprocess(
        ctx,
        vcf_stats_runner.build_query_command(bcftools_path=bcftools.path, vcf=vcf),
        log_path=str(query_path),
    )
    if code != 0:
        raise _failure(code, query_path, "bcftools query")

    # Contig lengths come from the payload -- the ingest parser already read
    # them from the header, and the handler cannot query for them.
    contig_lengths = [
        (name, int(length))
        for name, length in (ctx.payload.get("contig_lengths") or [])
    ]
    density = vcf_stats_runner.DensityAccumulator(contig_lengths=contig_lengths)
    filter_counts: dict[str, int] = {}

    def _rows():
        """One pass: every line reaches the database, the density bins and
        the FILTER tally without the file being read three times."""
        with open(query_path, errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 8:
                    continue
                try:
                    pos = int(parts[1])
                except ValueError:
                    continue
                filter_counts[parts[5]] = filter_counts.get(parts[5], 0) + 1
                density.add(parts[0], pos, ref=parts[2], alt=parts[3])
                yield line

    ctx.progress(phase="index", pct=0.7, message="building the variant index")
    report_dir = settings.vcf_stats_dir / str(object_id)
    report_dir.mkdir(parents=True, exist_ok=True)

    # Built at a temporary path and renamed into place, so a failed recompute
    # leaves the previous working database rather than a half-built one the
    # table would query.
    tmp_db = report_dir / "variants.db.tmp"
    total = variant_db.build_variant_db(rows=_rows(), db_path=tmp_db)
    tmp_db.replace(report_dir / "variants.db")

    # The downloadable export, beside the database. Moved rather than copied:
    # bcftools already wrote it.
    query_path.replace(report_dir / "variants.tsv")

    summary = vcf_stats_runner.variant_summary(stats, filter_counts=filter_counts)

    facts = {
        "vcf_stats_status": "ok",
        "vcf_stats_tool_version": bcftools.version,
        "vcf_stats_summary": summary,
        "vcf_stats_qual_histogram": vcf_stats_runner.rebin_distribution(
            stats["qual"], value_key="qual"
        ),
        "vcf_stats_depth_histogram": vcf_stats_runner.rebin_distribution(
            stats["dp"], value_key="depth"
        ),
        "vcf_stats_substitutions": stats["st"],
        "vcf_stats_indel_lengths": stats["idd"],
        "vcf_stats_filters": [
            {"filter": k, "count": v}
            for k, v in sorted(filter_counts.items(), key=lambda kv: -kv[1])
        ],
        "vcf_stats_density_bins": density.bins(),
        "vcf_stats_density_bounds": density.boundaries(),
        "vcf_stats_contigs": density.contigs(),
        "vcf_stats_report": "variants.tsv",
        "vcf_stats_db": "variants.db",
    }

    log.info("vcf_stats_done", object_id=object_id, variants=total)
    return {"object_id": object_id, "facts": facts}
```

- [ ] **Step 2: Verify the handler registers**

```bash
docker compose restart worker && sleep 4 && docker compose logs worker --tail 20 | grep handlers_loaded
```

Expected: the `handlers_loaded` line includes `run_vcf_stats`. If it does not,
the module did not import — check for a syntax error with
`docker compose exec api python -c "import app.queue.variant_handlers"`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/queue/variant_handlers.py
git commit -m "feat: add the run_vcf_stats job handler"
```

---

## Task 10: Launch and apply

**Files:**
- Modify: `backend/app/services/pipeline_service.py`
- Modify: `backend/app/queue/results.py`
- Test: `backend/tests/pipelines/test_vcf_stats_launch.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_vcf_stats_launch.py`:

```python
"""Preconditions for launching a Variant Results computation."""

import pytest

from app.errors import ValidationError
from app.models import FormatInfo, ObjectStatus
from app.services.pipeline_service import _check_vcf_stats_callable


class _Obj:
    def __init__(self, kind: str, status=ObjectStatus.READY):
        self.id = "abc123"
        self.name = f"sample.{kind}"
        self.status = status
        self.format = FormatInfo(kind=kind)
        self.facts: dict = {}


class TestCallable:
    def test_a_ready_vcf_is_callable(self):
        _check_vcf_stats_callable(_Obj("vcf"))

    def test_a_ready_bcf_is_callable(self):
        _check_vcf_stats_callable(_Obj("bcf"))

    def test_a_bam_is_refused(self):
        with pytest.raises(ValidationError, match="not a VCF"):
            _check_vcf_stats_callable(_Obj("bam"))

    def test_an_unready_vcf_is_refused(self):
        with pytest.raises(ValidationError, match="not ready"):
            _check_vcf_stats_callable(_Obj("vcf", status=ObjectStatus.INGESTING))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_vcf_stats_launch.py -v
```

Expected: FAIL — `ImportError: cannot import name '_check_vcf_stats_callable'`

- [ ] **Step 3: Write the launcher**

In `backend/app/services/pipeline_service.py`, add near `launch_bam_stats`.
First the constant, beside the existing `BAM_STATS_CALLABLE_KINDS`:

```python
VCF_STATS_CALLABLE_KINDS = {FormatKind.VCF, FormatKind.BCF}
```

Then:

```python
def _check_vcf_stats_callable(obj) -> None:
    """Whether a Variant Results computation can run against this object.

    Unlike the BAM path there is no index precondition: `bcftools stats` and
    `bcftools query` both stream the whole file, so a `.tbi` is never
    required and there is nothing to chain an index build onto.
    """
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready for results (status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind not in VCF_STATS_CALLABLE_KINDS:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not a VCF or BCF",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )


async def launch_vcf_stats(*, object_id: PydanticObjectId):
    """Queue the Results computation for a VCF: call-set summary statistics
    and the per-variant table.

    Read-only, like launch_bam_stats: no derived objects, just facts merged
    onto the object plus a TSV and a SQLite database on disk.
    """
    from app.queue import queue

    tools.require(tools.bcftools())

    vcf = await DataObject.get(object_id)
    if vcf is None:
        raise NotFoundError(f"Object not found: {object_id}")
    _check_vcf_stats_callable(vcf)

    digest, path = await _resolve_readable(vcf)

    # Contig names and lengths come from the facts the ingest parser already
    # wrote: the handler runs in a worker thread and cannot query for them.
    lengths = vcf.facts.get("reference_lengths") or {}
    contig_lengths = [[name, length] for name, length in lengths.items()]

    payload = {
        "object_id": str(vcf.id),
        "project_id": str(vcf.project_id),
        "vcf_name": vcf.name,
        "vcf_sha256": digest,
        "vcf_path": str(path),
        "contig_lengths": contig_lengths,
    }

    return await queue.enqueue(
        "run_vcf_stats",
        payload=payload,
        job_class=JobClass.COMPUTE,
        resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
        max_attempts=2,
        dedup_key=f"vcf_stats:{vcf.id}",
        project_id=vcf.project_id,
        object_id=vcf.id,
    )
```

Check the payload key names `_resolve_blob` expects by reading how
`launch_bam_stats` builds its own payload around line 1319, and match that
convention exactly — `_resolve_blob(ctx.payload, "vcf")` reads `vcf_sha256`
and `vcf_path`.

- [ ] **Step 4: Write the applier**

In `backend/app/queue/results.py`, add beside `_apply_run_bam_stats`:

```python
async def _apply_run_vcf_stats(result: dict) -> None:
    """Record a Variant Results computation on the VCF it described.

    Read-only like QC and BAM stats: no files to ingest, just facts merged
    onto the object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("vcf_stats_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "vcf_stats_applied",
        object_id=object_id,
        variants=facts.get("vcf_stats_summary", {}).get("variants"),
    )
```

And register it in `_APPLIERS`:

```python
    "run_vcf_stats": _apply_run_vcf_stats,
```

- [ ] **Step 5: Run the tests**

```bash
docker compose exec api python -m pytest tests/pipelines/test_vcf_stats_launch.py -v
```

Expected: PASS — 4 tests

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/queue/results.py backend/tests/pipelines/test_vcf_stats_launch.py
git commit -m "feat: launch and apply the Variant Results computation"
```

---

## Task 11: The API routes

**Files:**
- Modify: `backend/app/api/v1/pipelines.py`
- Test: `backend/tests/api/test_vcf_stats_report.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_vcf_stats_report.py`:

```python
"""The paginated variant-table route."""

import pytest

from app.pipelines.variant_db import build_variant_db


@pytest.fixture
def variant_db(tmp_path, monkeypatch):
    """A database where the route expects to find one."""
    from app.config import settings

    object_id = "507f1f77bcf86cd799439011"
    monkeypatch.setattr(
        type(settings), "vcf_stats_dir", property(lambda self: tmp_path)
    )
    d = tmp_path / object_id
    d.mkdir(parents=True)
    build_variant_db(
        rows=iter(
            [
                "chr1\t100\tA\tG\t50.0\tPASS\t30\t0/1",
                "chr1\t200\tC\tT\t10.0\tLowQual\t8\t0/1",
                "chr2\t150\tT\tC\t95.0\tPASS\t50\t1/1",
            ]
        ),
        db_path=d / "variants.db",
    )
    return object_id


class TestVariantsRoute:
    def test_returns_a_page_with_a_total(self, client, variant_db):
        r = client.get(f"/api/v1/pipelines/vcfstats/variants/{variant_db}?limit=2")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3
        assert len(body["rows"]) == 2
        assert body["rows"][0]["chrom"] == "chr1"

    def test_offset_pages(self, client, variant_db):
        r = client.get(
            f"/api/v1/pipelines/vcfstats/variants/{variant_db}?offset=2&limit=2"
        )
        assert [row["pos"] for row in r.json()["rows"]] == [150]

    def test_filters_narrow_the_total(self, client, variant_db):
        r = client.get(
            f"/api/v1/pipelines/vcfstats/variants/{variant_db}?filter_value=PASS"
        )
        body = r.json()
        assert body["total"] == 2
        assert all(row["filter"] == "PASS" for row in body["rows"])

    def test_skip_count_omits_the_total(self, client, variant_db):
        """A combined predicate costs ~400ms at 5M rows, so the client sends
        this when only the page number changed."""
        r = client.get(
            f"/api/v1/pipelines/vcfstats/variants/{variant_db}?skip_count=true"
        )
        assert r.json()["total"] is None

    def test_missing_database_reports_recompute_not_500(self, client, tmp_path, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(
            type(settings), "vcf_stats_dir", property(lambda self: tmp_path)
        )
        r = client.get(
            "/api/v1/pipelines/vcfstats/variants/507f1f77bcf86cd799439099"
        )
        assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec api python -m pytest tests/api/test_vcf_stats_report.py -v
```

Expected: FAIL — 404 on every route (they do not exist yet).

If `client` is not an existing fixture, read `backend/tests/conftest.py` and
`backend/tests/api/` for the project's convention and use whatever the other
API tests use.

- [ ] **Step 3: Write the routes**

In `backend/app/api/v1/pipelines.py`, add after the BAM stats routes. Add the
import first:

```python
from app.pipelines import variant_db
```

Then:

```python
class VcfStatsRequest(BaseModel):
    object_id: PydanticObjectId


@router.post("/vcfstats", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_vcf_stats(body: VcfStatsRequest) -> JobOut:
    """Queue the Results computation for a VCF: call-set summary statistics
    and the per-variant table. Read-only."""
    job = await pipeline_service.launch_vcf_stats(object_id=body.object_id)
    return JobOut.of(job)


@router.get("/vcfstats/variants/{object_id}")
async def get_vcf_stats_variants(
    object_id: PydanticObjectId,
    offset: int = 0,
    limit: int = 100,
    contig: str | None = None,
    pos_min: int | None = None,
    pos_max: int | None = None,
    filter_value: str | None = None,
    variant_type: str | None = None,
    min_qual: float | None = None,
    skip_count: bool = False,
) -> dict:
    """A page of the variant table, filtered.

    `total` is the count *after* filtering, so pagination stays correct. It is
    omitted when `skip_count` is set: a combined qual+filter predicate costs
    ~400ms at 5M rows and cannot use a single index, so the client sends this
    when only the page number changed and the previous total still holds.

    Unlike the BAM per-contig route this reads a database rather than slicing
    a TSV -- see the spec's finding 5.
    """
    db_path = settings.vcf_stats_dir / str(object_id) / "variants.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    filters = variant_db.VariantFilters(
        contig=contig,
        pos_min=pos_min,
        pos_max=pos_max,
        filter_value=filter_value,
        variant_type=variant_type,
        min_qual=min_qual,
    )

    rows = variant_db.query_variants(
        db_path=db_path, filters=filters, offset=offset, limit=limit
    )
    total = (
        None
        if skip_count
        else variant_db.count_variants(db_path=db_path, filters=filters)
    )
    return {"total": total, "rows": rows}


@router.get("/vcfstats/report/{object_id}/{report_path:path}")
async def get_vcf_stats_report(
    object_id: PydanticObjectId, report_path: str
) -> FileResponse:
    """Serve the downloadable variants TSV.

    Same containment rules as get_bam_stats_report -- `..` and absolute paths
    are rejected outright, then the resolved path is re-checked against the
    report root.
    """
    parts = PurePosixPath(report_path).parts
    if any(p in ("..", "") for p in parts) or PurePosixPath(report_path).is_absolute():
        raise NotFoundError(f"No such report: {report_path}")

    root = (settings.vcf_stats_dir / str(object_id)).resolve()
    target = (root / report_path).resolve()
    if not target.is_file() or root not in target.parents:
        raise NotFoundError(f"No such report: {report_path}")

    return FileResponse(
        target,
        media_type="text/tab-separated-values",
        filename=Path(report_path).name,
        headers={"X-Content-Type-Options": "nosniff"},
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec api python -m pytest tests/api/test_vcf_stats_report.py -v
```

Expected: PASS — 5 tests

- [ ] **Step 5: Verify the whole backend suite is green**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/tests/api/test_vcf_stats_report.py
git commit -m "feat: add the Variant Results launch and pagination routes"
```

---

## Task 12: End-to-end backend check against real data

Per CLAUDE.md: check against the real database, not only unit tests. The tests
so far all fed hand-built rows that already looked how the code expected.

**Files:** none — this is verification.

- [ ] **Step 1: Restart the worker so it picks up the new handler**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose restart worker && sleep 4
```

- [ ] **Step 2: Run the computation against the real 6,641-variant VCF**

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject
from app.services import pipeline_service

async def main():
    await connect_to_mongo()
    vcf = await DataObject.find_one(DataObject.name == 'DRR1066343.bcftools.vcf.gz')
    job = await pipeline_service.launch_vcf_stats(object_id=vcf.id)
    print('queued', job.id)
asyncio.run(main())
"
```

- [ ] **Step 3: Wait, then inspect the facts that landed**

```bash
sleep 25 && docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject

async def main():
    await connect_to_mongo()
    v = await DataObject.find_one(DataObject.name == 'DRR1066343.bcftools.vcf.gz')
    f = v.facts
    print('status   :', f.get('vcf_stats_status'))
    print('summary  :', f.get('vcf_stats_summary'))
    print('qual bins:', len(f.get('vcf_stats_qual_histogram') or []))
    print('dp bins  :', len(f.get('vcf_stats_depth_histogram') or []))
    print('contigs  :', len(f.get('vcf_stats_contigs') or []))
    print('density  :', len(f.get('vcf_stats_density_bins') or []))
    print('filters  :', f.get('vcf_stats_filters'))
asyncio.run(main())
"
```

Expected, verified against this file directly:
- `status` is `ok`
- `summary` has `variants: 6641`, `snps: 6157`, `indels: 484`, `ti_tv: 2.42`
- **`pass_pct` is absent** and `no_filter_count` is 6641 — this file's FILTER
  is `.` throughout, and this is the assertion that proves finding 3 holds on
  real data
- `qual bins` and `dp bins` are each ≤ 40, not 805 and 211
- `contigs` is 17
- `density` is 1000

If `vcf_stats_status` is missing entirely, the job failed — check
`docker compose logs worker --tail 50`.

- [ ] **Step 4: Confirm the database was built and is queryable**

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject
from app.config import settings
from app.pipelines.variant_db import VariantFilters, count_variants, query_variants

async def main():
    await connect_to_mongo()
    v = await DataObject.find_one(DataObject.name == 'DRR1066343.bcftools.vcf.gz')
    db = settings.vcf_stats_dir / str(v.id) / 'variants.db'
    print('db exists:', db.exists(), f'{db.stat().st_size/1e6:.1f} MB')
    print('total    :', count_variants(db_path=db, filters=VariantFilters()))
    print('chr1 snps:', count_variants(db_path=db, filters=VariantFilters(contig='NC_001133.9', variant_type='snp')))
    print('first row:', query_variants(db_path=db, filters=VariantFilters(), offset=0, limit=1))
asyncio.run(main())
"
```

Expected: `total` is 6641, matching the summary; the first row is a real
variant with typed numeric `pos`, `qual`, and `dp`.

- [ ] **Step 5: Confirm the empty VCF does not crash**

```bash
docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject
from app.services import pipeline_service

async def main():
    await connect_to_mongo()
    v = await DataObject.find_one(DataObject.name == 'DRR1078403.bcftools.vcf.gz')
    print('queued', (await pipeline_service.launch_vcf_stats(object_id=v.id)).id)
asyncio.run(main())
" && sleep 15 && docker compose exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject

async def main():
    await connect_to_mongo()
    v = await DataObject.find_one(DataObject.name == 'DRR1078403.bcftools.vcf.gz')
    print('status :', v.facts.get('vcf_stats_status'))
    print('summary:', v.facts.get('vcf_stats_summary'))
asyncio.run(main())
"
```

Expected: `status` is `ok` and `variants` is 0 — an empty call set is a
normal outcome, not a failure.

- [ ] **Step 6: Commit nothing, but record what you found**

If any expectation above did not hold, fix it before moving to the frontend
and add a test that would have caught it.

---

## Task 13: Frontend types and client methods

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add the types**

Append to `frontend/src/api/types.ts`:

```ts
/** One bucket of a re-binned distribution. `value` is the bucket's lower
 *  bound, so an axis can be labelled without knowing the bucket width. */
export interface HistogramBucket {
  value: number;
  count: number;
}

export interface VariantSummary {
  variants: number;
  snps: number;
  indels: number;
  multiallelic: number;
  samples: number;
  ts: number;
  tv: number;
  ti_tv: number;
  pass_count: number;
  no_filter_count: number;
  /** Absent when the file does not use FILTER at all -- bcftools call does
   *  not stamp PASS, and reporting a rate for such a file would misstate it. */
  pass_pct?: number;
}

export interface VariantContigRow {
  contig: string;
  length: number;
  variants: number;
  snps: number;
  indels: number;
  per_kb: number;
}

export interface VcfStatsFacts extends Record<string, unknown> {
  vcf_stats_status?: string;
  vcf_stats_tool_version?: string;
  vcf_stats_summary?: VariantSummary;
  vcf_stats_qual_histogram?: HistogramBucket[];
  vcf_stats_depth_histogram?: HistogramBucket[];
  vcf_stats_substitutions?: { type: string; count: number }[];
  vcf_stats_indel_lengths?: { length: number; count: number }[];
  vcf_stats_filters?: { filter: string; count: number }[];
  vcf_stats_density_bins?: number[];
  vcf_stats_density_bounds?: { contig: string; bin_start: number }[];
  vcf_stats_contigs?: VariantContigRow[];
  vcf_stats_report?: string;
  vcf_stats_db?: string;
}

export interface VariantRow {
  chrom: string;
  pos: number;
  ref: string;
  alt: string;
  qual: number | null;
  filter: string;
  dp: number | null;
  gt: string;
}

export interface VariantsPage {
  /** Null when the request set skip_count -- the caller keeps its previous
   *  total, because only the page number changed. */
  total: number | null;
  rows: VariantRow[];
}

export interface VariantQuery {
  offset: number;
  limit: number;
  contig?: string;
  posMin?: number;
  posMax?: number;
  filterValue?: string;
  variantType?: string;
  minQual?: number;
  skipCount?: boolean;
}
```

- [ ] **Step 2: Add the client methods**

In `frontend/src/api/client.ts`, add beside `bamStatsContigs`:

```ts
  launchVcfStats: (objectId: string) =>
    request<JobSummary>("/pipelines/vcfstats", {
      method: "POST",
      body: JSON.stringify({ object_id: objectId }),
    }),

  /** A page of the variant table. Filters are applied server-side against
   *  the SQLite index rather than by slicing a TSV -- see the spec's
   *  finding 5 for why this one differs from bamStatsContigs. */
  vcfStatsVariants: (objectId: string, q: VariantQuery) => {
    const p = new URLSearchParams({
      offset: String(q.offset),
      limit: String(q.limit),
    });
    if (q.contig) p.set("contig", q.contig);
    if (q.posMin != null) p.set("pos_min", String(q.posMin));
    if (q.posMax != null) p.set("pos_max", String(q.posMax));
    if (q.filterValue) p.set("filter_value", q.filterValue);
    if (q.variantType) p.set("variant_type", q.variantType);
    if (q.minQual != null) p.set("min_qual", String(q.minQual));
    if (q.skipCount) p.set("skip_count", "true");
    return request<VariantsPage>(
      `/pipelines/vcfstats/variants/${objectId}?${p.toString()}`,
    );
  },

  /** URL for downloading the complete variants TSV. */
  vcfStatsDownloadUrl: (objectId: string, reportPath: string) =>
    `${BASE}/pipelines/vcfstats/report/${objectId}/${reportPath}`,
```

Add `VariantQuery`, `VariantsPage`, and `JobSummary` to the existing type
import at the top of `client.ts` if they are not already there.

- [ ] **Step 3: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat: add Variant Results types and client methods"
```

---

## Task 14: The variant charts

Styling note for this and the next two tasks: the app's active theme is
Broadsheet (`frontend/src/styles/broadsheet.css`), which overrides
`styles.css`'s tokens via `.theme-broadsheet` on `<html>`. Use the existing
class vocabulary — `.section-title`, `.trim-table`, `.badge`, `.qc-chart` —
and `var(--accent)` for fills. **Do not add new CSS variables or theme rules.**
Needing one means the markup has drifted from the vocabulary.

**Files:**
- Create: `frontend/src/components/VariantCharts.tsx`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/VariantCharts.tsx`:

```tsx
import type { HistogramBucket } from "../api/types";

/**
 * Where the variants are along the reference.
 *
 * Bins are allocated by the same `allocate_bins` the BAM coverage strip uses,
 * so the two are directly comparable when both are on screen -- a gap in
 * coverage and a gap in calls line up horizontally.
 */
export function VariantDensityChart({
  bins,
  boundaries,
}: {
  bins: number[];
  boundaries: { contig: string; bin_start: number }[];
}) {
  if (!bins.length) return null;

  const w = 1000;
  const h = 120;
  const max = Math.max(...bins, 1);
  const barW = w / bins.length;

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h + 16}`}
      style={{ display: "block" }}
      role="img"
      aria-label="Variant density across the reference"
    >
      {bins.map((count, i) => {
        const barH = (count / max) * h;
        return (
          <rect
            key={i}
            x={i * barW}
            y={h - barH}
            width={Math.max(barW, 0.5)}
            height={barH}
            fill="var(--accent)"
          >
            <title>{count.toLocaleString()} variants</title>
          </rect>
        );
      })}
      {/* Contig separators, skipping the first -- a line at x=0 is the axis,
          not a boundary. Only drawn when there are few enough to read. */}
      {boundaries.length > 1 &&
        boundaries.length <= 40 &&
        boundaries.slice(1).map((b) => (
          <line
            key={b.contig}
            x1={b.bin_start * barW}
            x2={b.bin_start * barW}
            y1={0}
            y2={h}
            stroke="var(--border)"
            strokeWidth={1}
          />
        ))}
      <text x={0} y={h + 13} fontSize={11} fill="var(--text-faint)">
        {boundaries[0]?.contig ?? "0"}
      </text>
      <text
        x={w}
        y={h + 13}
        fontSize={11}
        fill="var(--text-faint)"
        textAnchor="end"
      >
        {max.toLocaleString()} max
      </text>
    </svg>
  );
}

/** A re-binned distribution as a bar chart. The backend has already collapsed
 *  the one-row-per-distinct-value output into at most 40 buckets. */
export function DistributionChart({
  buckets,
  label,
  format = (v) => `${v}`,
}: {
  buckets: HistogramBucket[];
  label: string;
  format?: (v: number) => string;
}) {
  if (!buckets.length) return null;

  const w = 520;
  const h = 150;
  const max = Math.max(...buckets.map((b) => b.count), 1);
  const barW = w / buckets.length;

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h + 16}`}
      style={{ display: "block" }}
      role="img"
      aria-label={label}
    >
      {buckets.map((b, i) => {
        const barH = (b.count / max) * h;
        return (
          <rect
            key={i}
            x={i * barW}
            y={h - barH}
            width={Math.max(barW - 1, 1)}
            height={barH}
            fill="var(--accent)"
          >
            <title>
              {format(b.value)}: {b.count.toLocaleString()}
            </title>
          </rect>
        );
      })}
      <text x={0} y={h + 13} fontSize={11} fill="var(--text-faint)">
        {format(buckets[0].value)}
      </text>
      <text
        x={w}
        y={h + 13}
        fontSize={11}
        fill="var(--text-faint)"
        textAnchor="end"
      >
        {format(buckets[buckets.length - 1].value)}
      </text>
    </svg>
  );
}
```

- [ ] **Step 2: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/VariantCharts.tsx
git commit -m "feat: add the variant density and distribution charts"
```

---

## Task 15: The variant table

**Files:**
- Create: `frontend/src/components/VariantTable.tsx`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/VariantTable.tsx`:

```tsx
import { useEffect, useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { VariantContigRow } from "../api/types";

const PAGE_SIZE = 100;

/** Debounce a value so typing "1000000" is one query, not seven. */
function useDebounced<T>(value: T, ms = 300): T {
  const [held, setHeld] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setHeld(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return held;
}

/**
 * The variant table, paginated and filtered server-side against the SQLite
 * index the compute job built.
 *
 * Two concessions to plant scale, where this table holds millions of rows.
 * The text inputs are debounced. And a page-number change asks the server to
 * skip its COUNT -- a combined qual+filter predicate costs ~400ms at 5M rows
 * and cannot use a single index, and the total cannot have changed when only
 * the page did.
 */
export function VariantTable({
  objectId,
  reportPath,
  contigs,
  filters: filterValues,
  samples,
}: {
  objectId: string;
  reportPath?: string;
  contigs: VariantContigRow[];
  filters: { filter: string; count: number }[];
  samples: string[];
}) {
  const [page, setPage] = useState(0);
  const [contig, setContig] = useState("");
  const [filterValue, setFilterValue] = useState("");
  const [variantType, setVariantType] = useState("");
  const [minQualRaw, setMinQualRaw] = useState("");
  const [sample, setSample] = useState(samples[0] ?? "");

  const minQual = useDebounced(minQualRaw);

  // Every filter except the page number. Changing any of these invalidates
  // the count; changing the page alone does not.
  const filterKey = useMemo(
    () => ({ contig, filterValue, variantType, minQual }),
    [contig, filterValue, variantType, minQual],
  );

  // Reset to the first page whenever the filter set changes -- page 40 of a
  // narrower result set is usually past the end.
  useEffect(() => setPage(0), [filterKey]);

  const { data, isLoading } = useQuery({
    queryKey: ["vcfstats", "variants", objectId, filterKey, page],
    queryFn: () =>
      api.vcfStatsVariants(objectId, {
        offset: page * PAGE_SIZE,
        limit: PAGE_SIZE,
        contig: contig || undefined,
        filterValue: filterValue || undefined,
        variantType: variantType || undefined,
        minQual: minQual ? Number(minQual) : undefined,
        // The count is only needed when the filters changed.
        skipCount: page > 0,
      }),
    placeholderData: keepPreviousData,
  });

  // The server returns null for total when it skipped the count, so hold the
  // last real one rather than flashing "0 variants" on every page turn.
  const [total, setTotal] = useState<number | null>(null);
  useEffect(() => {
    if (data?.total != null) setTotal(data.total);
  }, [data?.total]);

  const totalPages = total ? Math.max(1, Math.ceil(total / PAGE_SIZE)) : 1;
  const sampleIndex = Math.max(0, samples.indexOf(sample));

  return (
    <div className="section">
      <div
        className="section-title"
        style={{ display: "flex", alignItems: "center", gap: 8 }}
      >
        <span>Variants</span>
        {reportPath && (
          <a
            href={api.vcfStatsDownloadUrl(objectId, reportPath)}
            style={{
              marginLeft: "auto",
              fontSize: 14,
              textTransform: "none",
              letterSpacing: 0,
            }}
          >
            Download TSV
          </a>
        )}
      </div>

      <div
        style={{
          display: "flex",
          gap: 8,
          marginBottom: 12,
          flexWrap: "wrap",
        }}
      >
        <select value={contig} onChange={(e) => setContig(e.target.value)}>
          <option value="">All contigs</option>
          {contigs.map((c) => (
            <option key={c.contig} value={c.contig}>
              {c.contig} ({c.variants.toLocaleString()})
            </option>
          ))}
        </select>

        {samples.length > 1 && (
          <select value={sample} onChange={(e) => setSample(e.target.value)}>
            {samples.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        )}

        <select
          value={filterValue}
          onChange={(e) => setFilterValue(e.target.value)}
        >
          <option value="">All filters</option>
          {filterValues.map((f) => (
            <option key={f.filter} value={f.filter}>
              {f.filter === "." ? "(none)" : f.filter} ({f.count.toLocaleString()})
            </option>
          ))}
        </select>

        <select
          value={variantType}
          onChange={(e) => setVariantType(e.target.value)}
        >
          <option value="">All types</option>
          <option value="snp">SNPs</option>
          <option value="indel">Indels</option>
        </select>

        <input
          value={minQualRaw}
          onChange={(e) => setMinQualRaw(e.target.value)}
          placeholder="min QUAL"
          inputMode="decimal"
          style={{ width: 110 }}
        />
      </div>

      {isLoading && !data ? (
        <div style={{ color: "var(--text-faint)", fontSize: 14 }}>Loading…</div>
      ) : !data?.rows.length ? (
        <div style={{ color: "var(--text-faint)", fontSize: 14 }}>
          No variants match these filters.
        </div>
      ) : (
        <>
          <table className="trim-table">
            <thead>
              <tr>
                <th style={{ textAlign: "left" }}>Chrom</th>
                <th>Pos</th>
                <th style={{ textAlign: "left" }}>Ref</th>
                <th style={{ textAlign: "left" }}>Alt</th>
                <th>Qual</th>
                <th style={{ textAlign: "left" }}>Filter</th>
                <th>DP</th>
                <th>GT</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row) => (
                <tr key={`${row.chrom}:${row.pos}:${row.alt}`}>
                  <td className="mono" style={{ textAlign: "left" }}>
                    {row.chrom}
                  </td>
                  <td>{row.pos.toLocaleString()}</td>
                  <td className="mono" style={{ textAlign: "left" }}>
                    {row.ref.length > 12 ? `${row.ref.slice(0, 12)}…` : row.ref}
                  </td>
                  <td className="mono" style={{ textAlign: "left" }}>
                    {row.alt.length > 12 ? `${row.alt.slice(0, 12)}…` : row.alt}
                  </td>
                  <td>{row.qual == null ? "—" : row.qual.toFixed(1)}</td>
                  <td style={{ textAlign: "left" }}>
                    <span
                      className={`badge${row.filter === "PASS" ? " ready" : ""}`}
                    >
                      {row.filter === "." ? "—" : row.filter}
                    </span>
                  </td>
                  <td>{row.dp == null ? "—" : row.dp.toLocaleString()}</td>
                  <td className="mono">
                    {row.gt.split("\t")[sampleIndex] ?? row.gt}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginTop: 12,
              fontSize: 11,
              color: "var(--text-faint)",
            }}
          >
            <span>
              {total == null
                ? `Showing ${data.rows.length}`
                : `${total.toLocaleString()} variant${total === 1 ? "" : "s"}`}
            </span>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <button
                type="button"
                className="btn"
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
              >
                Prev
              </button>
              <span>
                Page {page + 1}
                {total != null ? ` of ${totalPages.toLocaleString()}` : ""}
              </span>
              <button
                type="button"
                className="btn"
                onClick={() => setPage((p) => p + 1)}
                disabled={data.rows.length < PAGE_SIZE}
              >
                Next
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/VariantTable.tsx
git commit -m "feat: add the paginated, filtered variant table"
```

---

## Task 16: The Results tab

**Files:**
- Create: `frontend/src/components/VariantResults.tsx`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/VariantResults.tsx`:

```tsx
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type {
  ObjectDetail as ObjectDetailData,
  VcfStatsFacts,
} from "../api/types";
import { DistributionChart, VariantDensityChart } from "./VariantCharts";
import { VariantTable } from "./VariantTable";

/**
 * What the variant caller produced: how many calls and of what kind, how
 * confident they are, where they sit on the reference, and the calls
 * themselves.
 *
 * Mirrors BamResults: a compute prompt until the facts exist, then the
 * summary. The per-variant table is backed by a SQLite index rather than the
 * flat TSV the BAM per-contig table paginates -- see the spec's finding 5.
 */
export function VariantResults({ obj }: { obj: ObjectDetailData }) {
  const qc = useQueryClient();
  const f = obj.facts as VcfStatsFacts;

  const compute = useMutation({
    mutationFn: () => api.launchVcfStats(obj.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info("Computing results");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const hasResults = f.vcf_stats_status === "ok";
  const summary = f.vcf_stats_summary;

  if (!hasResults) {
    return (
      <div className="section">
        <div className="section-title">Variant results</div>
        <div className="section-note">
          Call counts, transition/transversion ratio, quality and depth
          distributions, and a browsable table of every variant — computed on
          demand from the VCF.
        </div>
        <button
          type="button"
          className="btn primary"
          onClick={() => compute.mutate()}
          disabled={compute.isPending}
        >
          {compute.isPending ? "Computing…" : "Compute results"}
        </button>
      </div>
    );
  }

  const provenance = [
    f.vcf_stats_tool_version ? `bcftools ${f.vcf_stats_tool_version}` : null,
    typeof obj.facts.variants_called_by === "string"
      ? `called by ${obj.facts.variants_called_by}` +
        (typeof obj.facts.variant_caller_version === "string"
          ? ` ${obj.facts.variant_caller_version}`
          : "")
      : null,
    summary?.samples
      ? `${summary.samples} sample${summary.samples === 1 ? "" : "s"}`
      : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const samples = Array.isArray(obj.facts.sample_names)
    ? (obj.facts.sample_names as string[])
    : [];

  return (
    <>
      <div className="qc-provenance">
        {provenance}
        {provenance && " · "}
        <button
          type="button"
          onClick={() => compute.mutate()}
          disabled={compute.isPending}
          style={{
            color: "var(--accent)",
            fontSize: "inherit",
            textTransform: "none",
            letterSpacing: 0,
          }}
        >
          {compute.isPending ? "recomputing…" : "recompute results"}
        </button>
      </div>

      {summary && summary.variants === 0 ? (
        <div className="section">
          <div className="section-note">
            No variants in this file. An empty call set is a normal outcome of
            a strict caller — it means nothing passed its thresholds, not that
            the run failed.
          </div>
        </div>
      ) : (
        <>
          {summary && <SummaryRow summary={summary} />}

          {f.vcf_stats_density_bins && f.vcf_stats_density_bins.length > 0 && (
            <div className="section">
              <div className="section-title">Variant density</div>
              <VariantDensityChart
                bins={f.vcf_stats_density_bins}
                boundaries={f.vcf_stats_density_bounds ?? []}
              />
            </div>
          )}

          <div className="qc-charts">
            {f.vcf_stats_qual_histogram &&
              f.vcf_stats_qual_histogram.length > 0 && (
                <div className="qc-chart">
                  <div className="section-title">QUAL distribution</div>
                  <DistributionChart
                    buckets={f.vcf_stats_qual_histogram}
                    label="Variant quality distribution"
                    format={(v) => v.toFixed(0)}
                  />
                </div>
              )}
            {f.vcf_stats_depth_histogram &&
              f.vcf_stats_depth_histogram.length > 0 && (
                <div className="qc-chart">
                  <div className="section-title">Depth distribution</div>
                  <DistributionChart
                    buckets={f.vcf_stats_depth_histogram}
                    label="Read depth distribution at variant sites"
                    format={(v) => `${v.toFixed(0)}×`}
                  />
                </div>
              )}
          </div>

          <div className="facts-columns">
            {f.vcf_stats_substitutions &&
              f.vcf_stats_substitutions.length > 0 && (
                <div className="section">
                  <div className="section-title">Substitution types</div>
                  <SubstitutionTable rows={f.vcf_stats_substitutions} />
                </div>
              )}

            {f.vcf_stats_filters && f.vcf_stats_filters.length > 0 && (
              <div className="section">
                <div className="section-title">Filters</div>
                <FilterTable
                  rows={f.vcf_stats_filters}
                  total={summary?.variants ?? 0}
                />
              </div>
            )}
          </div>

          {f.vcf_stats_contigs && f.vcf_stats_contigs.length > 0 && (
            <div className="section">
              <div className="section-title">Per-contig counts</div>
              <table className="trim-table">
                <thead>
                  <tr>
                    <th style={{ textAlign: "left" }}>Contig</th>
                    <th>Length</th>
                    <th>Variants</th>
                    <th>Per kb</th>
                    <th>SNPs</th>
                    <th>Indels</th>
                  </tr>
                </thead>
                <tbody>
                  {f.vcf_stats_contigs.map((c) => (
                    <tr key={c.contig}>
                      <td className="mono" style={{ textAlign: "left" }}>
                        {c.contig}
                      </td>
                      <td>{c.length.toLocaleString()}</td>
                      <td>{c.variants.toLocaleString()}</td>
                      <td>{c.per_kb.toFixed(2)}</td>
                      <td>{c.snps.toLocaleString()}</td>
                      <td>{c.indels.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <VariantTable
            objectId={obj.id}
            reportPath={f.vcf_stats_report}
            contigs={f.vcf_stats_contigs ?? []}
            filters={f.vcf_stats_filters ?? []}
            samples={samples}
          />
        </>
      )}
    </>
  );
}

function SummaryRow({
  summary,
}: {
  summary: NonNullable<VcfStatsFacts["vcf_stats_summary"]>;
}) {
  return (
    <div
      style={{
        display: "flex",
        gap: 30,
        flexWrap: "wrap",
        marginBottom: 20,
      }}
    >
      <Stat label="Variants" value={summary.variants.toLocaleString()} />
      <Stat label="SNPs" value={summary.snps.toLocaleString()} />
      <Stat label="Indels" value={summary.indels.toLocaleString()} />
      <Stat label="Ti/Tv" value={summary.ti_tv.toFixed(2)} />
      {/* Absent when the file never used FILTER: bcftools call does not stamp
          PASS, and a "100% PASS" headline on such a file asserts something
          untrue about the call set. */}
      {summary.pass_pct != null && (
        <Stat label="PASS" value={`${summary.pass_pct.toFixed(1)}%`} />
      )}
      {summary.multiallelic > 0 && (
        <Stat
          label="Multiallelic"
          value={summary.multiallelic.toLocaleString()}
        />
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div
        style={{
          color: "var(--text-faint)",
          fontSize: 11,
          letterSpacing: "0.14em",
          textTransform: "uppercase",
        }}
      >
        {label}
      </div>
      <div style={{ fontWeight: 600, fontSize: 26, lineHeight: 1.1 }}>
        {value}
      </div>
    </div>
  );
}

function SubstitutionTable({ rows }: { rows: { type: string; count: number }[] }) {
  const max = Math.max(...rows.map((r) => r.count), 1);
  return (
    <table className="trim-table">
      <tbody>
        {rows.map((r) => (
          <tr key={r.type}>
            <td className="mono" style={{ textAlign: "left", width: 70 }}>
              {r.type}
            </td>
            <td style={{ width: 70 }}>{r.count.toLocaleString()}</td>
            <td>
              <svg width="100%" height="8" style={{ display: "block" }}>
                <rect
                  width={`${(r.count / max) * 100}%`}
                  height="8"
                  fill="var(--accent)"
                />
              </svg>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FilterTable({
  rows,
  total,
}: {
  rows: { filter: string; count: number }[];
  total: number;
}) {
  return (
    <table className="trim-table">
      <tbody>
        {rows.map((r) => (
          <tr key={r.filter}>
            <td style={{ textAlign: "left" }}>
              <span className={`badge${r.filter === "PASS" ? " ready" : ""}`}>
                {r.filter === "." ? "no filter applied" : r.filter}
              </span>
            </td>
            <td>{r.count.toLocaleString()}</td>
            <td style={{ color: "var(--text-faint)", width: 70 }}>
              {total ? `${((100 * r.count) / total).toFixed(1)}%` : "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

- [ ] **Step 2: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/VariantResults.tsx
git commit -m "feat: add the Variant Results tab component"
```

---

## Task 17: Wire the tab into the detail panel

**Files:**
- Modify: `frontend/src/components/DetailPanel.tsx:268-294` (`tabsFor`)
- Modify: `frontend/src/components/DetailPanel.tsx:622-626` (the results panel)

- [ ] **Step 1: Widen the tab condition**

In `frontend/src/components/DetailPanel.tsx`, replace this in `tabsFor`:

```ts
  if (obj.format.kind === "bam") {
    tabs.push({ id: "results", label: "Results" });
  }
```

with:

```ts
  // One tab id across all three formats rather than a push per format: `tab`
  // is persisted in the URL alongside ?sel=, so a link stays on Results when
  // the selection moves between a BAM and the VCF called from it.
  const hasResults =
    obj.format.kind === "bam" ||
    obj.format.kind === "vcf" ||
    obj.format.kind === "bcf";
  if (hasResults) {
    tabs.push({ id: "results", label: "Results" });
  }
```

- [ ] **Step 2: Dispatch on format kind**

Replace:

```tsx
        {tab === "results" && (
          <TabPanel id="results" idPrefix="obj">
            <BamResults obj={obj} />
          </TabPanel>
        )}
```

with:

```tsx
        {tab === "results" && (
          <TabPanel id="results" idPrefix="obj">
            {obj.format.kind === "bam" ? (
              <BamResults obj={obj} />
            ) : (
              <VariantResults obj={obj} />
            )}
          </TabPanel>
        )}
```

And add the import beside the existing `BamResults` one:

```tsx
import { VariantResults } from "./VariantResults";
```

- [ ] **Step 3: Update the `tabsFor` doc comment**

The comment above `tabsFor` says Results "only appears for BAMs, which is the
only format it currently describes." Replace that clause with:

```
 * appears for BAMs and for called variants -- the two formats that have
 * something computed to show.
```

- [ ] **Step 4: Verify it compiles**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/DetailPanel.tsx
git commit -m "feat: show the Results tab for VCF and BCF files"
```

---

## Task 18: Manual verification in the browser

**Files:** none — this is the actual verification step for anything UI-facing.
There is no headless component-testing setup in this repo and none is expected.

- [ ] **Step 1: Rebuild and restart**

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

- [ ] **Step 2: Confirm the stack is serving the right tree**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

If any path contains `.claude/worktrees/`, the stack is on the wrong tree —
re-run the command in Step 1 from the main repo root.

- [ ] **Step 3: Check the tab against a file with real variants**

Open http://localhost:5173, select `DRR1066343.bcftools.vcf.gz`, open
**Results**. Verify:

- The tab appears at all (it did not exist for VCFs before)
- Before computing: the prompt and a working "Compute results" button
- After computing: 6,641 variants, 6,157 SNPs, 484 indels, Ti/Tv 2.42
- **No PASS statistic** — this file's FILTER is `.` throughout, and the
  Filters table shows "no filter applied" for all 6,641
- The density strip shows 17 contig separators
- QUAL and depth charts are readable histograms, not 805 hairline bars
- Per-contig table lists 17 rows, `NC_001144.5` highest at 1,838
- The variant table paginates, and Prev/Next move through it

- [ ] **Step 4: Check the filters**

- Contig dropdown narrows the table and the total
- Filter dropdown offers `(none)` with its count
- SNPs / Indels narrow correctly
- min QUAL accepts a number and does not fire a request per keystroke
- A filter matching nothing shows "No variants match these filters", not an
  error or an endless spinner
- Changing a filter resets to page 1

- [ ] **Step 5: Check the empty and single-variant files**

Select `DRR1078403.bcftools.vcf.gz` (0 variants) and compute. Expect the
"No variants in this file" explanation, not empty charts or an error. Repeat
for `DRR1078403.clair3.vcf.gz` (1 record, `RefCall`) — the Filters table
should show `RefCall`, and since that file *does* use FILTER, a PASS
statistic should appear reading 0%.

- [ ] **Step 6: Check the theme**

Confirm the tab reads as the rest of the app: Source Serif headings with no
rule beneath, uppercase small-caps column headers, cyan `#006786` chart bars,
badge pills for FILTER values. If anything looks like the dark classic theme,
a hardcoded color has crept in — search the new components for `#` literals.

- [ ] **Step 7: Confirm the BAM tab still works**

Select a BAM and open Results. Task 2 refactored `bin_depth`, which that tab
depends on — the coverage strip must still render correctly.

- [ ] **Step 8: Commit any fixes**

```bash
git add -A && git commit -m "fix: <what the browser check turned up>"
```

If nothing needed fixing, skip this step.

---

## Task 19: Verify against plant-scale data

The three VCFs in the database are too small to exercise what this design
exists for. A tab that feels instant on 6,641 yeast variants proves nothing
about a maize call set.

**Files:** none — verification.

- [ ] **Step 1: Build a plant-scale VCF**

If no real *Arabidopsis* resequencing VCF is to hand, synthesize one at the
right scale:

```bash
docker compose exec api python -c "
import gzip, random
random.seed(7)
# Arabidopsis: 5 chromosomes, ~119 Mb, ~700k variants.
lengths = [30427671, 19698289, 23459830, 18585056, 26975502]
with gzip.open('/data/tmp/athaliana.synthetic.vcf.gz', 'wt') as f:
    f.write('##fileformat=VCFv4.2\n')
    for i, L in enumerate(lengths, 1):
        f.write(f'##contig=<ID=Chr{i},length={L}>\n')
    f.write('##INFO=<ID=DP,Number=1,Type=Integer,Description=\"Depth\">\n')
    f.write('##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n')
    f.write('##FILTER=<ID=LowQual,Description=\"Low quality\">\n')
    f.write('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\n')
    bases = 'ACGT'
    for i, L in enumerate(lengths, 1):
        n = int(L / 170)
        for pos in sorted(random.sample(range(1, L), n)):
            r = random.choice(bases)
            a = random.choice([b for b in bases if b != r])
            q = round(random.uniform(3, 400), 1)
            filt = 'PASS' if q > 30 else 'LowQual'
            f.write(f'Chr{i}\t{pos}\t.\t{r}\t{a}\t{q}\t{filt}\tDP={random.randint(3,90)}\tGT\t0/1\n')
print('written')
" && docker compose exec api bash -c "ls -la /data/tmp/athaliana.synthetic.vcf.gz"
```

- [ ] **Step 2: Ingest it, then compute and time the job**

Upload it through the UI at http://localhost:5173, then open Results and click
Compute. Note the elapsed time from the job list.

Expected: roughly 30-90 seconds — dominated by `bcftools query` and the index
build, both of which scale linearly. If it takes many minutes, check that the
indexes are still being created *after* the bulk insert in
`build_variant_db`, not before.

- [ ] **Step 3: Check the artifacts**

```bash
docker compose exec api bash -c "du -sh /data/vcf_stats/*/ | tail -3"
```

Expected: a few hundred MB — mostly the database. Confirm both `variants.db`
and `variants.tsv` exist.

- [ ] **Step 4: Check API memory while paging**

With the Results tab open, page through the table and filter while watching:

```bash
docker stats --no-stream --format "{{.Name}} {{.MemUsage}}" biopipe-api-1
```

Expected: **API memory stays flat**, tens of MB above baseline. This is the
whole point of the SQLite design — if it climbs by hundreds of MB per request,
something is reading the TSV instead of querying the database.

- [ ] **Step 5: Check responsiveness**

- Page turns feel instant, with no loading flash (`keepPreviousData`)
- Filtering by contig returns promptly
- A combined filter (contig + type + min QUAL) is acceptable — this is the
  ~400ms COUNT case
- The density strip shows 5 contigs and is legible

- [ ] **Step 6: Clean up the synthetic file**

Delete it from the UI, and:

```bash
docker compose exec api rm -f /data/tmp/athaliana.synthetic.vcf.gz
```

- [ ] **Step 7: Record the numbers**

Add the observed compute time, database size, and API memory to the PR
description. If any of them are far off the projections in the spec, say so —
the spec's finding 5 is a claim about real behaviour and should be corrected
if it turns out wrong.

---

## Task 20: Final check

- [ ] **Step 1: Full backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass, no regressions.

- [ ] **Step 2: Frontend typecheck and build**

```bash
cd frontend && npx tsc --noEmit && npm run build
```

Expected: both clean.

- [ ] **Step 3: Confirm the suggestion rules need nothing**

Per CLAUDE.md, adding a tool means revisiting `suggestion_service.py`. This
adds no new *pipeline* — it is a compute action on an existing file, like BAM
results, which has no suggestion card either. Confirm by checking that no card
references `run_bam_stats`:

```bash
grep -n "bam_stats\|vcf_stats" backend/app/services/suggestion_service.py || echo "neither referenced -- nothing to update"
```

Expected: no matches. If there are matches, a card may need the same treatment
for VCFs.

- [ ] **Step 4: Review the diff**

```bash
git log --oneline main..HEAD
git diff main --stat
```

- [ ] **Step 5: Merge or open a PR**

Use the `superpowers:finishing-a-development-branch` skill.
