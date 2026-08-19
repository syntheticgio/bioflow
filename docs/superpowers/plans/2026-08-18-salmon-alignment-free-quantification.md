# Salmon Alignment-Free Quantification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Salmon as an alignment-free RNA-seq quantification path that produces gene-level counts consumable by the existing pyDESeq2 differential expression node.

**Architecture:** A new `salmon_runner.py` of pure functions (command construction, `quant.sf` parsing, transcript-to-gene summarization), a `salmon_quantify` job handler that mirrors `quantify` in `expression_handlers.py`, and a results applier that registers the summarized output as an ordinary `ObjectRole.COUNTS` object. The existing `differential_expression` node consumes that output unchanged, because its input port keys on the role rather than on the producing tool.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor (MongoDB), pytest, Salmon 1.10.2 from Debian trixie apt.

**Spec:** `docs/superpowers/specs/2026-08-18-salmon-alignment-free-quantification-design.md`

## Global Constraints

- **Salmon version:** 1.10.2 (`salmon:1.10.2+ds1-1+b5` from Debian trixie main, arm64 and amd64 both available). Installed via apt, not vendored.
- **License (verified upstream 2026-08-18 via `gh api repos/COMBINE-lab/salmon`):** `BSD-3-Clause`. Do not alter this string without re-verifying against the repository.
- **Homepage:** `https://combine-lab.github.io/salmon`
- **Repository:** `https://github.com/COMBINE-lab/salmon`
- **Citation:** `Patro R, Duggal G, Love MI, Irizarry RA, Kingsford C. Salmon provides fast and bias-aware quantification of transcript expression. Nature Methods. 2017;14(4):417-419.`
- **Citation URL:** `https://doi.org/10.1038/nmeth.4197`
- **Run tests from this worktree only with** `./backend/run-worktree-tests.sh`. The main-checkout `exec api` form silently tests main's tree, not this one.
- **`worker` does not hot-reload.** After changing any handler, `docker compose restart worker` from the MAIN checkout before re-testing a job.
- **Commit style:** Conventional Commits, imperative mood, lowercase after the colon, no trailing period, ~65 chars. Scope `pipelines` for runner/tools work, `queue` for handlers.
- **`-l A`** (automatic library type detection) is the fixed library-type argument for `salmon quant` in this implementation. Do not add a user-facing strandedness parameter.

---

### Task 1: Install Salmon and register the tool probe

**Files:**
- Modify: `backend/Dockerfile:80-100` (the `apt-get install` block)
- Modify: `backend/app/config.py:153` (beside `featurecounts_path`)
- Modify: `backend/app/pipelines/tools.py` (add `salmon()` near `featurecounts()` at ~786; add to the aggregate list at ~857)
- Test: `backend/tests/pipelines/test_tools.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `tools.salmon() -> Tool` — a `Tool` with `.name == "salmon"`, `.path`, `.version`, `.error`, `.available`. `settings.salmon_path: str = "salmon"`.

- [ ] **Step 1: Write the failing test**

In `backend/tests/pipelines/test_tools.py`, add:

```python
class TestSalmonProbe:
    """Salmon's probe, and the reason it needs no special-casing.

    Unlike featureCounts (which exits non-zero on `-v`), `salmon --version`
    exits zero and prints a bare "salmon 1.10.2" to stdout. Verified against
    the Debian trixie binary rather than recalled.
    """

    def test_probe_reports_version_from_stdout(self, monkeypatch):
        import subprocess

        monkeypatch.setattr(tools.shutil, "which", lambda _: "/usr/bin/salmon")

        def fake_run(cmd, **kwargs):
            assert cmd == ["/usr/bin/salmon", "--version"]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=b"salmon 1.10.2\n", stderr=b""
            )

        monkeypatch.setattr(tools.subprocess, "run", fake_run)
        tools.salmon.cache_clear()

        tool = tools.salmon()
        assert tool.name == "salmon"
        assert tool.available
        assert "1.10.2" in tool.version

    def test_probe_reports_error_when_absent(self, monkeypatch):
        monkeypatch.setattr(tools.shutil, "which", lambda _: None)
        tools.salmon.cache_clear()

        tool = tools.salmon()
        assert not tool.available
        assert "SALMON_PATH" in tool.error

    def test_salmon_is_in_the_aggregate_list(self):
        names = {t.name for t in tools.all_tools()}
        assert "salmon" in names
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -k Salmon -v
```

Expected: FAIL with `AttributeError: module 'app.pipelines.tools' has no attribute 'salmon'`.

Note: if `all_tools` is named differently in this file, read the top of `test_tools.py` and match the existing aggregate-list test rather than inventing a name.

- [ ] **Step 3: Add the settings path**

In `backend/app/config.py`, immediately after the `featurecounts_path` line:

```python
    salmon_path: str = "salmon"
```

- [ ] **Step 4: Add the probe**

In `backend/app/pipelines/tools.py`, immediately after `featurecounts()`:

```python
@lru_cache(maxsize=1)
def salmon() -> Tool:
    # `salmon --version` exits zero and prints a bare "salmon 1.10.2" to
    # stdout, so none of featureCounts' special-casing applies. Verified
    # against the Debian trixie binary (1.10.2+ds1-1+b5) rather than recalled.
    return _probe("salmon", settings.salmon_path, ["--version"])
```

Add `salmon(),` to the aggregate list beside `featurecounts(),`.

- [ ] **Step 5: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -k Salmon -v
```

Expected: PASS (3 tests).

- [ ] **Step 6: Add Salmon to the image**

In `backend/Dockerfile`, add `salmon \` to the existing `apt-get install -y --no-install-recommends` list, alphabetically near `samtools`. Add a comment above the block entry only if the surrounding entries carry one; otherwise no comment is needed, since the package name and the binary name match (unlike `subread`/`featureCounts`).

- [ ] **Step 7: Verify the package resolves**

```bash
docker run --rm debian:trixie-slim sh -c "apt-get update -qq && apt-cache policy salmon"
```

Expected: `Candidate: 1.10.2+ds1-1+b5`.

- [ ] **Step 8: Commit**

```bash
git add backend/Dockerfile backend/app/config.py backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat(pipelines): install salmon and probe for it"
```

---

### Task 2: Document Salmon in TOOL_META

**Files:**
- Modify: `backend/app/pipelines/tools.py` (the `TOOL_META` dict, beside the `"featurecounts"` entry at ~2197)
- Test: `backend/tests/pipelines/test_tools.py` (`test_every_tool_is_documented` already exists and will now cover it)

**Interfaces:**
- Consumes: `tools.salmon()` from Task 1.
- Produces: `TOOL_META["salmon"]` — a `ToolMeta` with `pipelines=(PipelineType.EXPRESSION,)`.

- [ ] **Step 1: Run the existing documentation test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -k test_every_tool_is_documented -v
```

Expected: FAIL naming `salmon` as undocumented. This test is the gate; it already exists, so no new test is written for the requirement itself.

- [ ] **Step 2: Add the TOOL_META entry**

In `backend/app/pipelines/tools.py`, add to `TOOL_META` beside `"featurecounts"`:

```python
    "salmon": ToolMeta(
        pipelines=(PipelineType.EXPRESSION,),
        one_liner="Quantifies transcript abundance without aligning",
        summary=(
            "Estimates how much of each transcript is present directly from "
            "reads, using selective alignment against a transcriptome rather "
            "than aligning to a genome first. Reaches the same differential "
            "expression test as the align-and-count path, in a fraction of "
            "the time, for users who want expression numbers and nothing else."
        ),
        strengths=(
            "No alignment step: minutes rather than hours on a typical sample",
            "Corrects for GC and positional bias that naive counting ignores",
            "Distributes multi-mapping reads instead of discarding them",
            "Detects the library's strandedness itself, so there is no flag to get wrong",
        ),
        homepage="https://combine-lab.github.io/salmon",
        repository="https://github.com/COMBINE-lab/salmon",
        citation=(
            "Patro R, Duggal G, Love MI, Irizarry RA, Kingsford C. Salmon "
            "provides fast and bias-aware quantification of transcript "
            "expression. Nature Methods. 2017;14(4):417-419."
        ),
        citation_url="https://doi.org/10.1038/nmeth.4197",
        # Verified 2026-08-18 against the upstream repository via
        # `gh api repos/COMBINE-lab/salmon`, not recalled.
        license="BSD-3-Clause",
        usage=(
            "Runs one sample at a time against a transcriptome index, which is "
            "built once per transcriptome and reused across every sample in "
            "the project. Transcript-level estimates are summed to genes "
            "before they are stored, so the result is an ordinary counts file "
            "that the differential expression test accepts alongside "
            "featureCounts output -- though not mixed into the same "
            "comparison, since the two describe different gene universes. "
            "The library's strandedness is detected automatically rather than "
            "asked for. Note that a CDS reference (what an NCBI genome "
            "download provides) covers coding sequences only: UTRs and "
            "non-coding transcripts are absent from the estimates."
        ),
    ),
```

- [ ] **Step 3: Run the documentation test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -k test_every_tool_is_documented -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/app/pipelines/tools.py
git commit -m "docs(pipelines): document salmon on the software page"
```

---

### Task 3: Parse `quant.sf` into per-transcript abundance

**Files:**
- Create: `backend/app/pipelines/salmon_runner.py`
- Test: `backend/tests/pipelines/test_salmon_runner.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `parse_quant(text: str) -> tuple[dict[str, float], dict]` — `({transcript_id: num_reads}, facts)`. Facts keys: `transcripts_in_index` (int), `transcripts_detected` (int), `estimated_reads` (float).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_salmon_runner.py`:

```python
"""Salmon's command construction, output parsing, and the tx2gene refusal.

The refusal is the reason most of this file exists. A transcript-to-gene map
that silently falls back to "each transcript is its own gene" produces a
counts file that merges cleanly, passes every downstream check, and tests a
gene universe nobody intended -- the same silent-success shape that cost STAR
its index sidecars.
"""

import pytest

from app.errors import ValidationError
from app.pipelines import salmon_runner


# A real quant.sf header plus three rows. Columns are Name, Length,
# EffectiveLength, TPM, NumReads -- NumReads last, and fractional, which is
# the whole reason a summarization step exists.
QUANT_SF = """Name\tLength\tEffectiveLength\tTPM\tNumReads
tx1\t1500\t1350.0\t120.5\t340.7
tx2\t900\t750.0\t80.25\t112.3
tx3\t2000\t1850.0\t0.0\t0.0
"""


class TestParseQuant:
    def test_reads_num_reads_per_transcript(self):
        per_tx, _ = salmon_runner.parse_quant(QUANT_SF)
        assert per_tx == {"tx1": 340.7, "tx2": 112.3, "tx3": 0.0}

    def test_counts_detected_separately_from_total(self):
        _, facts = salmon_runner.parse_quant(QUANT_SF)
        assert facts["transcripts_in_index"] == 3
        # tx3 is in the index and got nothing. "Detected" is the signal that
        # separates a bad sample from a wrong reference; the total alone
        # cannot say which.
        assert facts["transcripts_detected"] == 2
        assert facts["estimated_reads"] == pytest.approx(453.0)

    def test_ignores_a_blank_trailing_line(self):
        per_tx, _ = salmon_runner.parse_quant(QUANT_SF + "\n")
        assert len(per_tx) == 3

    def test_empty_table_is_not_an_error_here(self):
        # A header with no rows is a real Salmon output for an empty input.
        # The handler decides whether that is a failure; the parser does not.
        per_tx, facts = salmon_runner.parse_quant(
            "Name\tLength\tEffectiveLength\tTPM\tNumReads\n"
        )
        assert per_tx == {}
        assert facts["transcripts_in_index"] == 0
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_salmon_runner.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.pipelines.salmon_runner'`.

- [ ] **Step 3: Create the module with the parser**

Create `backend/app/pipelines/salmon_runner.py`:

```python
"""Building and reading a Salmon run.

Kept separate from the job handler for the same reason `counts_runner.py` is:
the parts worth testing -- command construction, output parsing, and the
transcript-to-gene summarization -- are pure functions over strings, paths and
dicts, with no queue or filesystem involved.

The summarization is the part that earns the care. Salmon reports fractional
reads per *transcript*; pyDESeq2 here consumes integer counts per *gene*. The
bridge between them has to agree with what featureCounts calls a gene, or two
count files that look interchangeable describe different gene universes.
"""

import re
import shlex
from pathlib import Path

from app.errors import ValidationError
from app.logging import get_logger

log = get_logger(__name__)

# quant.sf's header. Salmon writes exactly these five columns; NumReads is
# last and is a float, not an integer.
_QUANT_HEADER_RE = re.compile(r"^Name\tLength\tEffectiveLength\tTPM\tNumReads")


def parse_quant(text: str) -> tuple[dict[str, float], dict]:
    """A `quant.sf` table as {transcript_id: num_reads} plus summary facts.

    `NumReads` is an *estimate*, and fractional: Salmon distributes a
    multi-mapping read across the transcripts it is compatible with rather
    than discarding it or assigning it arbitrarily. Keeping the float here and
    rounding only after transcripts are summed to genes matters -- rounding
    per transcript first would discard a fraction of a read thousands of times
    over and drag every gene's count down.

    `transcripts_detected` is returned alongside the total for the same reason
    `counts_runner.parse_counts` returns `genes_detected`: the total alone
    cannot separate "this sample is bad" from "this is the wrong
    transcriptome", and the detected count moves differently in each case.
    """
    per_transcript: dict[str, float] = {}
    for line in text.splitlines():
        if not line.strip() or _QUANT_HEADER_RE.match(line):
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 5:
            continue
        try:
            per_transcript[parts[0]] = float(parts[-1])
        except ValueError:
            continue

    facts = {
        "transcripts_in_index": len(per_transcript),
        "transcripts_detected": sum(1 for v in per_transcript.values() if v > 0),
        "estimated_reads": sum(per_transcript.values()),
    }
    return per_transcript, facts
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_salmon_runner.py -v
```

Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/salmon_runner.py backend/tests/pipelines/test_salmon_runner.py
git commit -m "feat(pipelines): read salmon's quant.sf into per-transcript reads"
```

---

### Task 4: Verify the real NCBI CDS defline format

**Files:**
- Create: nothing (this is a verification task; its output is a recorded fact)
- Modify: `docs/superpowers/plans/2026-08-18-salmon-alignment-free-quantification.md` (record the finding in Task 5's notes)

**Interfaces:**
- Consumes: nothing.
- Produces: a verified defline sample, pasted into Task 5's test fixture.

**This task exists because the spec records the defline format as unverified (REQ-TX2GENE-2).** Do not skip it and do not write `parse_tx2gene` against a recalled format. The whole risk in this feature is that a wrong assumption here produces output that looks correct.

- [ ] **Step 1: Download a small CDS FASTA**

S. cerevisiae is small (~6000 CDS) and is already the assembly this repo's own `counts_runner` docstrings were checked against.

```bash
docker run --rm -v /tmp/salmon-check:/out debian:trixie-slim sh -c "apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends curl ca-certificates >/dev/null && curl -sL 'https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/146/045/GCF_000146045.2_R64/GCF_000146045.2_R64_cds_from_genomic.fna.gz' -o /out/cds.fna.gz"
```

- [ ] **Step 2: Read the first few deflines**

```bash
gunzip -c /tmp/salmon-check/cds.fna.gz | grep '^>' | head -5
```

Expected shape (CONFIRM, do not assume): `>lcl|NC_001133.9_cds_NP_009332.1_1 [gene=PAU8] [locus_tag=YAL068C] [protein=...] ...`

- [ ] **Step 3: Check which attributes are universally present**

```bash
gunzip -c /tmp/salmon-check/cds.fna.gz | grep -c '^>'
gunzip -c /tmp/salmon-check/cds.fna.gz | grep '^>' | grep -c 'locus_tag='
gunzip -c /tmp/salmon-check/cds.fna.gz | grep '^>' | grep -c 'gene='
```

Expected: the `locus_tag=` count equals the total; the `gene=` count may be lower. **Record all three numbers.** Whichever attribute is universal is the one `parse_tx2gene` must prefer, because `counts_runner.attributes_for_format` groups NCBI GFF3 by `locus_tag` — the two must agree or the gene universes will not match.

- [ ] **Step 4: Record the finding in this plan**

Edit Task 5 below, replacing the fixture deflines with real ones from Step 2 and confirming the preference order in `parse_tx2gene` matches Step 3's counts.

- [ ] **Step 5: Clean up**

```bash
rm -rf /tmp/salmon-check
```

- [ ] **Step 6: Commit the plan update**

```bash
git add docs/superpowers/plans/2026-08-18-salmon-alignment-free-quantification.md
git commit -m "docs(pipelines): record the verified NCBI CDS defline format"
```

---

### Task 5: Map transcripts to genes, refusing an unparseable defline

**Files:**
- Modify: `backend/app/pipelines/salmon_runner.py`
- Test: `backend/tests/pipelines/test_salmon_runner.py`

**Interfaces:**
- Consumes: `parse_quant` from Task 3; the verified defline format from Task 4.
- Produces:
  - `parse_tx2gene(headers: list[str]) -> dict[str, str]` — `{transcript_id: gene_id}`. Raises `ValidationError` on any header it cannot map.
  - `summarize_to_gene(per_tx: dict[str, float], tx2gene: dict[str, str]) -> tuple[dict[str, int], dict]` — `({gene_id: count}, facts)`. Facts keys: `genes_in_reference` (int), `genes_detected` (int), `counted_fragments` (int).

**REQ-TX2GENE-1: no fallback.** If a defline cannot be mapped, raise. Do not use the transcript ID as its own gene ID.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_salmon_runner.py`:

```python
# Real NCBI CDS deflines. REPLACE THESE with the ones recorded in Task 4 if
# they differ -- this fixture is the whole point of that verification task.
HEADERS = [
    ">lcl|NC_001133.9_cds_NP_009332.1_1 [gene=PAU8] [locus_tag=YAL068C] [protein=seripauperin PAU8]",
    ">lcl|NC_001133.9_cds_NP_009333.1_2 [gene=SEO1] [locus_tag=YAL067C] [protein=Seo1p]",
    # Two CDS belonging to one gene: the case that makes summarization more
    # than a rename.
    ">lcl|NC_001133.9_cds_NP_009334.1_3 [gene=SEO1] [locus_tag=YAL067C] [protein=Seo1p isoform]",
]


class TestParseTx2Gene:
    def test_maps_each_transcript_to_its_locus_tag(self):
        mapping = salmon_runner.parse_tx2gene(HEADERS)
        assert mapping == {
            "lcl|NC_001133.9_cds_NP_009332.1_1": "YAL068C",
            "lcl|NC_001133.9_cds_NP_009333.1_2": "YAL067C",
            # Both CDS of SEO1 collapse onto one gene, which is what makes
            # summarization more than a rename.
            "lcl|NC_001133.9_cds_NP_009334.1_3": "YAL067C",
        }

    def test_prefers_locus_tag_over_gene(self):
        # locus_tag is what counts_runner.attributes_for_format groups NCBI
        # GFF3 by. Preferring `gene=` would produce a gene universe that does
        # not match featureCounts output for the same organism.
        mapping = salmon_runner.parse_tx2gene(
            [">lcl|X_cds_1 [gene=ABC1] [locus_tag=Y0001W]"]
        )
        assert mapping == {"lcl|X_cds_1": "Y0001W"}

    def test_falls_back_to_gene_when_no_locus_tag(self):
        mapping = salmon_runner.parse_tx2gene([">lcl|X_cds_1 [gene=ABC1]"])
        assert mapping == {"lcl|X_cds_1": "ABC1"}

    def test_refuses_a_header_it_cannot_map(self):
        # REQ-TX2GENE-1. The alternative -- treating the transcript as its own
        # gene -- yields a counts file that merges cleanly and is wrong.
        with pytest.raises(ValidationError) as exc:
            salmon_runner.parse_tx2gene([">some_bare_transcript_id"])
        assert "some_bare_transcript_id" in str(exc.value)

    def test_refusal_names_the_offending_header_not_just_a_count(self):
        with pytest.raises(ValidationError) as exc:
            salmon_runner.parse_tx2gene(HEADERS + [">unmappable_one"])
        assert "unmappable_one" in str(exc.value)


class TestSummarizeToGene:
    def test_sums_transcripts_belonging_to_one_gene(self):
        per_tx = {"t1": 10.4, "t2": 5.2, "t3": 4.4}
        tx2gene = {"t1": "geneA", "t2": "geneB", "t3": "geneB"}
        counts, _ = salmon_runner.summarize_to_gene(per_tx, tx2gene)
        # geneB is 5.2 + 4.4 = 9.6, rounded once at the end -> 10.
        assert counts == {"geneA": 10, "geneB": 10}

    def test_rounds_after_summing_not_before(self):
        # Three transcripts at 0.4 each are one read's worth of evidence.
        # Rounding per transcript first would discard all of it.
        per_tx = {"t1": 0.4, "t2": 0.4, "t3": 0.4}
        tx2gene = {"t1": "g", "t2": "g", "t3": "g"}
        counts, _ = salmon_runner.summarize_to_gene(per_tx, tx2gene)
        assert counts == {"g": 1}

    def test_genes_with_no_reads_are_kept(self):
        # The gene universe must be the reference's, not the sample's.
        # Dropping zero-count genes would make two samples disagree on their
        # gene sets, which de_runner.merge_counts refuses outright.
        per_tx = {"t1": 0.0, "t2": 5.0}
        tx2gene = {"t1": "geneA", "t2": "geneB"}
        counts, facts = salmon_runner.summarize_to_gene(per_tx, tx2gene)
        assert counts == {"geneA": 0, "geneB": 5}
        assert facts["genes_in_reference"] == 2
        assert facts["genes_detected"] == 1

    def test_refuses_a_transcript_absent_from_the_map(self):
        # Salmon reported a transcript the map does not know. Summing the rest
        # silently would drop reads from the totals.
        with pytest.raises(ValidationError) as exc:
            salmon_runner.summarize_to_gene({"unknown_tx": 5.0}, {"t1": "g"})
        assert "unknown_tx" in str(exc.value)

    def test_counted_fragments_reports_the_integer_total(self):
        per_tx = {"t1": 10.4, "t2": 5.2}
        tx2gene = {"t1": "geneA", "t2": "geneB"}
        _, facts = salmon_runner.summarize_to_gene(per_tx, tx2gene)
        assert facts["counted_fragments"] == 15
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_salmon_runner.py -k "Tx2Gene or Summarize" -v
```

Expected: FAIL with `AttributeError: module 'app.pipelines.salmon_runner' has no attribute 'parse_tx2gene'`.

- [ ] **Step 3: Implement both functions**

Append to `backend/app/pipelines/salmon_runner.py`:

```python
# NCBI CDS deflines carry bracketed attributes after the sequence ID:
#   >lcl|NC_001133.9_cds_NP_009332.1_1 [gene=PAU8] [locus_tag=YAL068C] ...
# Verified against GCF_000146045.2 (S. cerevisiae R64) rather than recalled --
# see the plan's defline verification task.
_ATTR_RE = re.compile(r"\[(\w+)=([^\]]*)\]")

# locus_tag first, deliberately. `counts_runner.attributes_for_format` groups
# NCBI GFF3 by locus_tag, so preferring `gene` here would produce a gene
# universe that does not match featureCounts output for the same organism --
# two counts files that look interchangeable and are not.
_GENE_ATTRIBUTES = ("locus_tag", "gene")


def parse_tx2gene(headers: list[str]) -> dict[str, str]:
    """Transcript ID to gene ID, from a transcriptome FASTA's deflines.

    Raises rather than guessing. The tempting fallback -- when a defline
    carries no gene attribute, use the transcript ID as its own gene -- is
    what makes this function dangerous: it produces a counts file with one
    "gene" per transcript that merges cleanly, passes every downstream sanity
    check, and quietly tests a gene universe the user never chose. Nothing
    downstream can detect it. So an unmappable header is an error naming the
    header, which a user can act on.
    """
    mapping: dict[str, str] = {}
    for header in headers:
        line = header[1:] if header.startswith(">") else header
        line = line.strip()
        if not line:
            continue

        transcript_id = line.split(None, 1)[0]
        attrs = dict(_ATTR_RE.findall(line))

        gene_id = None
        for key in _GENE_ATTRIBUTES:
            value = (attrs.get(key) or "").strip()
            if value:
                gene_id = value
                break

        if gene_id is None:
            raise ValidationError(
                "This transcriptome's sequence names do not say which gene "
                "each transcript belongs to, so transcript estimates cannot "
                f"be summed into genes. First one: {transcript_id!r}. "
                "A CDS or RNA FASTA downloaded from NCBI carries "
                "[locus_tag=...] or [gene=...] on every sequence.",
                details={"header": line[:200], "transcript_id": transcript_id},
            )

        mapping[transcript_id] = gene_id

    return mapping


def summarize_to_gene(
    per_transcript: dict[str, float], tx2gene: dict[str, str]
) -> tuple[dict[str, int], dict]:
    """Transcript-level estimates summed into integer gene-level counts.

    The tximport equivalent, and the reason Salmon output can feed the same
    differential expression test featureCounts output does.

    Two details that would each be a silent error if done the other way.
    Rounding happens once, after summing: a gene with three transcripts at 0.4
    estimated reads each has a read's worth of evidence, and rounding per
    transcript first would throw all of it away, thousands of times over.
    And every gene in the map is present in the output even at zero, because
    the gene universe belongs to the reference rather than to the sample --
    `de_runner.merge_counts` refuses samples whose gene sets differ at all, so
    dropping a gene that happened to get no reads in one sample would break
    the merge for the whole experiment.
    """
    unknown = set(per_transcript) - set(tx2gene)
    if unknown:
        raise ValidationError(
            f"{len(unknown)} transcripts in the quantification are not in the "
            "transcript-to-gene map, so their reads would be silently "
            f"dropped. First: {sorted(unknown)[0]!r}. This usually means the "
            "index was built from a different file than the one being "
            "summarized.",
            details={"unknown": sorted(unknown)[:5], "count": len(unknown)},
        )

    totals: dict[str, float] = {gene: 0.0 for gene in tx2gene.values()}
    for transcript, reads in per_transcript.items():
        totals[tx2gene[transcript]] += reads

    counts = {gene: round(value) for gene, value in totals.items()}
    facts = {
        "genes_in_reference": len(counts),
        "genes_detected": sum(1 for v in counts.values() if v > 0),
        "counted_fragments": sum(counts.values()),
    }
    return counts, facts
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_salmon_runner.py -v
```

Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/salmon_runner.py backend/tests/pipelines/test_salmon_runner.py
git commit -m "feat(pipelines): sum salmon transcript estimates into gene counts"
```

---

### Task 6: Build the index and quant commands

**Files:**
- Modify: `backend/app/pipelines/salmon_runner.py`
- Test: `backend/tests/pipelines/test_salmon_runner.py`

**Interfaces:**
- Consumes: the module from Tasks 3 and 5.
- Produces:
  - `index_command(*, transcriptome: Path, index_dir: Path, salmon_path: str, threads: int = 4) -> list[str]`
  - `quant_command(*, index_dir: Path, reads: list[Path], out_dir: Path, salmon_path: str, threads: int = 4) -> list[str]`
  - `quant_file(out_dir: Path) -> Path` — `out_dir / "quant.sf"`
  - `output_name(sample_name: str) -> str` — the counts filename
  - `command_line(cmd: list[str]) -> str`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_salmon_runner.py`:

```python
from pathlib import Path


class TestIndexCommand:
    def test_builds_an_index_from_a_transcriptome(self):
        cmd = salmon_runner.index_command(
            transcriptome=Path("/w/tx.fna"),
            index_dir=Path("/w/idx"),
            salmon_path="/usr/bin/salmon",
            threads=8,
        )
        assert cmd[:2] == ["/usr/bin/salmon", "index"]
        assert "-t" in cmd and "/w/tx.fna" in cmd
        assert "-i" in cmd and "/w/idx" in cmd
        assert "-p" in cmd and "8" in cmd


class TestQuantCommand:
    def test_single_end_uses_unmated_reads_flag(self):
        cmd = salmon_runner.quant_command(
            index_dir=Path("/w/idx"),
            reads=[Path("/w/a.fastq.gz")],
            out_dir=Path("/w/out"),
            salmon_path="/usr/bin/salmon",
        )
        assert cmd[:2] == ["/usr/bin/salmon", "quant"]
        assert "-r" in cmd
        assert "-1" not in cmd

    def test_paired_end_uses_mate_flags(self):
        cmd = salmon_runner.quant_command(
            index_dir=Path("/w/idx"),
            reads=[Path("/w/r1.fastq.gz"), Path("/w/r2.fastq.gz")],
            out_dir=Path("/w/out"),
            salmon_path="/usr/bin/salmon",
        )
        assert "-1" in cmd and "/w/r1.fastq.gz" in cmd
        assert "-2" in cmd and "/w/r2.fastq.gz" in cmd
        assert "-r" not in cmd

    def test_library_type_is_always_automatic(self):
        # -l A. The featureCounts path needs the library orientation supplied
        # because a wrong -s yields near-zero counts that look like a failed
        # experiment; Salmon infers it, so there is no flag for a user to get
        # wrong and none is offered.
        cmd = salmon_runner.quant_command(
            index_dir=Path("/w/idx"),
            reads=[Path("/w/a.fastq.gz")],
            out_dir=Path("/w/out"),
            salmon_path="/usr/bin/salmon",
        )
        assert "-l" in cmd
        assert cmd[cmd.index("-l") + 1] == "A"

    def test_refuses_more_than_two_read_files(self):
        with pytest.raises(ValidationError):
            salmon_runner.quant_command(
                index_dir=Path("/w/idx"),
                reads=[Path("/w/a"), Path("/w/b"), Path("/w/c")],
                out_dir=Path("/w/out"),
                salmon_path="/usr/bin/salmon",
            )

    def test_refuses_no_reads(self):
        with pytest.raises(ValidationError):
            salmon_runner.quant_command(
                index_dir=Path("/w/idx"),
                reads=[],
                out_dir=Path("/w/out"),
                salmon_path="/usr/bin/salmon",
            )


class TestPaths:
    def test_quant_file_is_inside_the_output_directory(self):
        assert salmon_runner.quant_file(Path("/w/out")) == Path("/w/out/quant.sf")

    def test_output_name_is_derived_from_the_sample(self):
        assert salmon_runner.output_name("SRR123_1.fastq.gz").endswith(".counts.tsv")
        assert "SRR123" in salmon_runner.output_name("SRR123_1.fastq.gz")

    def test_command_line_is_copy_pasteable(self):
        assert salmon_runner.command_line(["salmon", "quant", "-i", "/a b"]) == (
            "salmon quant -i '/a b'"
        )
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_salmon_runner.py -k "Command or Paths" -v
```

Expected: FAIL with `AttributeError: ... has no attribute 'index_command'`.

- [ ] **Step 3: Implement the command builders**

Append to `backend/app/pipelines/salmon_runner.py`:

```python
def index_command(
    *,
    transcriptome: Path,
    index_dir: Path,
    salmon_path: str,
    threads: int = 4,
) -> list[str]:
    """`salmon index` for one transcriptome.

    Built once per transcriptome and reused by every sample, which is why this
    is a separate command rather than folded into the quantification: on a
    twelve-sample experiment the index is built once instead of twelve times.
    """
    return [
        salmon_path,
        "index",
        "-t",
        str(transcriptome),
        "-i",
        str(index_dir),
        "-p",
        str(threads),
    ]


def quant_command(
    *,
    index_dir: Path,
    reads: list[Path],
    out_dir: Path,
    salmon_path: str,
    threads: int = 4,
) -> list[str]:
    """`salmon quant` for one sample.

    `-l A` always. Salmon infers the library's strandedness from the data and
    reports what it inferred, so unlike the featureCounts path there is no
    orientation for a user to supply and therefore none to get wrong -- the
    failure `counts_runner.strandedness_for_align_params` exists to prevent
    cannot happen here.
    """
    if not reads:
        raise ValidationError("Salmon needs at least one reads file.")
    if len(reads) > 2:
        raise ValidationError(
            "Salmon quantifies one sample at a time: either one file of "
            f"single-end reads or two of paired-end, not {len(reads)}.",
            details={"files": [str(r) for r in reads]},
        )

    cmd = [salmon_path, "quant", "-i", str(index_dir), "-l", "A"]
    if len(reads) == 2:
        cmd += ["-1", str(reads[0]), "-2", str(reads[1])]
    else:
        cmd += ["-r", str(reads[0])]
    cmd += ["-o", str(out_dir), "-p", str(threads)]
    return cmd


def quant_file(out_dir: Path) -> Path:
    """Where `salmon quant` writes its abundance table."""
    return out_dir / "quant.sf"


def output_name(sample_name: str) -> str:
    """The counts file name for a sample, matching the counts_runner shape."""
    stem = Path(sample_name).name
    for suffix in (".gz", ".fastq", ".fq"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    stem = stem.removesuffix("_1").removesuffix("_R1")
    return f"{stem}.counts.tsv"


def command_line(cmd: list[str]) -> str:
    """The command as a copy-pasteable string, for provenance."""
    return shlex.join(cmd)
```

- [ ] **Step 4: Run the whole runner test file**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_salmon_runner.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/salmon_runner.py backend/tests/pipelines/test_salmon_runner.py
git commit -m "feat(pipelines): build salmon index and quant commands"
```

---

### Task 7: Write the counts file in the format featureCounts output is read in

**Files:**
- Modify: `backend/app/pipelines/salmon_runner.py`
- Test: `backend/tests/pipelines/test_salmon_runner.py`

**Interfaces:**
- Consumes: `summarize_to_gene` from Task 5.
- Produces: `format_counts(counts: dict[str, int]) -> str` — a table `counts_runner.parse_counts` can read back.

**Why this task exists:** `de_runner` reads counts objects through `counts_runner.parse_counts`, which requires at least 7 tab-separated columns and takes the count from the LAST column (`parts[-1]`), skipping `#` comments and the `Geneid\t` header. A Salmon counts file that does not satisfy that shape will be silently unreadable.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/pipelines/test_salmon_runner.py`:

```python
from app.pipelines import counts_runner


class TestFormatCounts:
    def test_output_round_trips_through_the_featurecounts_parser(self):
        # The contract that matters. de_runner reads every counts object with
        # counts_runner.parse_counts, so Salmon output that this parser cannot
        # read is silently unusable for differential expression.
        counts = {"YAL068C": 340, "YAL067C": 0, "YAL066W": 12}
        text = salmon_runner.format_counts(counts)
        parsed, facts = counts_runner.parse_counts(text)
        assert parsed == counts
        assert facts["genes_in_annotation"] == 3
        assert facts["genes_detected"] == 2

    def test_rows_are_sorted_for_reproducibility(self):
        text = salmon_runner.format_counts({"zzz": 1, "aaa": 2})
        body = [ln for ln in text.splitlines() if not ln.startswith(("#", "Geneid"))]
        assert body[0].startswith("aaa")

    def test_empty_counts_still_produce_a_readable_header(self):
        parsed, _ = counts_runner.parse_counts(salmon_runner.format_counts({}))
        assert parsed == {}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_salmon_runner.py -k FormatCounts -v
```

Expected: FAIL with `AttributeError: ... has no attribute 'format_counts'`.

- [ ] **Step 3: Implement the formatter**

Append to `backend/app/pipelines/salmon_runner.py`:

```python
# featureCounts' column layout, which this output has to imitate exactly.
# `counts_runner.parse_counts` requires at least seven tab-separated fields
# and reads the count from the last one, so the five positional columns
# between Geneid and the count have to be present even though Salmon has
# nothing to put in them.
_COUNTS_HEADER = "Geneid\tChr\tStart\tEnd\tStrand\tLength\tcount"


def format_counts(counts: dict[str, int]) -> str:
    """Gene counts in the layout `counts_runner.parse_counts` reads.

    Deliberately imitating featureCounts' output rather than inventing a
    simpler two-column format. Every counts object in this application is read
    back through that one parser, so a second format would need a second
    reader and a way to tell which is which -- and the failure if anything
    guessed wrong would be a differential expression run that silently saw no
    genes.

    The five coordinate columns are empty because Salmon quantifies against a
    transcriptome and never learns where a gene sits on a chromosome. Nothing
    downstream reads them; `parse_counts` indexes from the end for exactly
    this reason.
    """
    lines = ["# Generated by salmon via BioFlow", _COUNTS_HEADER]
    # Sorted so the file is byte-identical across runs of the same input.
    for gene in sorted(counts):
        lines.append(f"{gene}\t\t\t\t\t\t{counts[gene]}")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_salmon_runner.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/salmon_runner.py backend/tests/pipelines/test_salmon_runner.py
git commit -m "feat(pipelines): write salmon counts in the shape the parser reads"
```

---

### Task 8: Add the salmon index sidecar role

**Files:**
- Modify: `backend/app/models/object.py` (the `SidecarRole` enum, ~line 196 beside `WINNOWMAP_INDEX`)
- Test: `backend/tests/queue/test_results.py` or wherever `_SIDECAR_ROLES` exhaustiveness is asserted — find it first (see Step 1)

**Interfaces:**
- Consumes: nothing.
- Produces: `SidecarRole.SALMON_INDEX = "salmon-index"`.

**REQ-INDEX-1.** `results._SIDECAR_ROLES` is the derivable case (`{role.value: role for role in SidecarRole}`), so this should be covered automatically — but this is the exact registry that cost STAR's `build_index` job all eight of its index files while reporting success. Confirm the assertion holds rather than assuming it.

- [ ] **Step 1: Find the existing exhaustiveness assertion**

```bash
grep -rn "_SIDECAR_ROLES" backend/app backend/tests
```

Read what it asserts before changing anything. If it is genuinely `{role.value: role for role in SidecarRole}`, adding a member is safe and the test covers it. If it is a hand-written dict, this task must add the entry there too — check, do not assume.

- [ ] **Step 2: Add the enum member**

In `backend/app/models/object.py`, beside `WINNOWMAP_INDEX`:

```python
    # Salmon's transcriptome index -- a directory of several files, stored
    # against the transcriptome it was built from and reused by every sample
    # quantified against it.
    SALMON_INDEX = "salmon-index"
```

- [ ] **Step 3: Run the sidecar exhaustiveness test**

```bash
./backend/run-worktree-tests.sh -k "sidecar" -v
```

Expected: PASS. If it FAILS, the registry is not derivable after all — add the entry wherever it is hand-maintained, then re-run.

- [ ] **Step 4: Commit**

```bash
git add backend/app/models/object.py
git commit -m "feat(models): add the salmon index sidecar role"
```

---

### Task 9: Add the salmon_quantify job handler

**Files:**
- Modify: `backend/app/queue/expression_handlers.py`
- Test: `backend/tests/queue/test_expression_handlers.py` (create if absent; check with `ls backend/tests/queue/`)

**Interfaces:**
- Consumes: `salmon_runner` (Tasks 3, 5, 6, 7); `tools.salmon()` (Task 1); `SidecarRole.SALMON_INDEX` (Task 8).
- Produces: a `salmon_quantify` handler returning a dict with keys `object_id`, `transcriptome_object_id`, `project_id`, `job_id`, `output` (`{"tmp_path", "name"}`), `tool_version`, `annotation_name`, `annotation_sha256`, `facts`, `workdir`.

**Note on `annotation_sha256`:** it carries the **transcriptome** digest. `pipeline_service` reads that key straight out of `facts` when assembling a DE design (`pipeline_service.py:4295`), and `de_runner.merge_counts` refuses samples whose digests differ. Filling it with the transcriptome digest is what makes the gate correct for Salmon — and what correctly refuses a matrix mixing Salmon and featureCounts samples, since those digests can never match.

- [ ] **Step 1: Read the existing handler to match its shape**

```bash
sed -n '30,180p' backend/app/queue/expression_handlers.py
```

The new handler mirrors `quantify` closely: `_prepare_workdir`, symlink inputs via `_resolve_blob`, `run_subprocess` with a log path, check the output exists, parse facts, return a plain dict. It must NOT touch the database — it runs in a worker thread.

- [ ] **Step 2: Write the failing test**

In `backend/tests/queue/test_expression_handlers.py`:

```python
"""The salmon_quantify handler's contract with the results applier.

The handler runs in a worker thread and cannot touch the database, so its
entire output is the dict it returns. These tests pin the keys that dict must
carry -- particularly annotation_sha256, which is what lets
de_runner.merge_counts refuse a design mixing incompatible samples.
"""

import pytest

from app.pipelines import salmon_runner


class TestSalmonQuantifyResultContract:
    def test_transcriptome_digest_is_carried_as_annotation_sha256(self):
        # pipeline_service reads this key out of facts when it builds a DE
        # design, and merge_counts refuses samples whose values differ. For
        # Salmon the digest is the transcriptome's -- there is no annotation.
        # This also, correctly, refuses a matrix mixing Salmon and
        # featureCounts samples: their digests can never match, and they do
        # not describe the same gene universe.
        from app.queue import expression_handlers

        result = expression_handlers._salmon_result_dict(
            object_id="64b" + "0" * 21,
            transcriptome_object_id="64c" + "0" * 21,
            project_id="64d" + "0" * 21,
            job_id="64e" + "0" * 21,
            output_path="/tmp/x/SRR1.counts.tsv",
            tool_version="1.10.2",
            transcriptome_name="cds.fna",
            transcriptome_sha256="deadbeef",
            facts={"genes_detected": 12},
            workdir="/tmp/x",
        )
        assert result["annotation_sha256"] == "deadbeef"
        assert result["annotation_name"] == "cds.fna"
        assert result["facts"]["quantified_by"] == "salmon"

    def test_output_carries_the_path_and_name_the_applier_needs(self):
        from app.queue import expression_handlers

        result = expression_handlers._salmon_result_dict(
            object_id="64b" + "0" * 21,
            transcriptome_object_id="64c" + "0" * 21,
            project_id="64d" + "0" * 21,
            job_id="64e" + "0" * 21,
            output_path="/tmp/x/SRR1.counts.tsv",
            tool_version="1.10.2",
            transcriptome_name="cds.fna",
            transcriptome_sha256="deadbeef",
            facts={},
            workdir="/tmp/x",
        )
        assert result["output"]["name"] == "SRR1.counts.tsv"
        assert result["output"]["tmp_path"] == "/tmp/x/SRR1.counts.tsv"
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_expression_handlers.py -v
```

Expected: FAIL with `AttributeError: module 'app.queue.expression_handlers' has no attribute '_salmon_result_dict'`.

- [ ] **Step 4: Implement the result builder and the handler**

In `backend/app/queue/expression_handlers.py`, add `salmon_runner` to the `from app.pipelines import ...` line, then append:

```python
def _salmon_result_dict(
    *,
    object_id: str,
    transcriptome_object_id: str | None,
    project_id: str | None,
    job_id: str,
    output_path: str,
    tool_version: str | None,
    transcriptome_name: str,
    transcriptome_sha256: str | None,
    facts: dict,
    workdir: str,
) -> dict:
    """The dict `results._apply_salmon_quantify` consumes.

    Split out from the handler so the key contract can be tested without
    running Salmon.

    `annotation_sha256` carries the *transcriptome* digest. There is no
    annotation in this pipeline, but that is the key `pipeline_service` reads
    when it assembles a differential expression design, and
    `de_runner.merge_counts` refuses samples whose values differ. Filling it
    with the transcriptome digest makes that gate do the right thing here: two
    samples quantified against different transcriptomes are refused, and so is
    a design mixing Salmon output with featureCounts output -- correctly, since
    those two do not describe the same gene universe.
    """
    enriched = dict(facts)
    enriched["quantified_by"] = "salmon"
    return {
        "object_id": object_id,
        "transcriptome_object_id": transcriptome_object_id,
        "project_id": project_id,
        "job_id": job_id,
        "output": {"tmp_path": output_path, "name": Path(output_path).name},
        "tool_version": tool_version,
        "annotation_name": transcriptome_name,
        "annotation_sha256": transcriptome_sha256,
        "facts": enriched,
        "workdir": workdir,
    }


@handler(
    "salmon_quantify",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # Salmon holds the transcriptome index in memory and streams reads past
    # it. The index for a vertebrate transcriptome is the driver; 8 GB covers
    # it. HEAVY io because it reads the FASTQ files end to end.
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
    max_attempts=2,
)
def salmon_quantify(ctx: JobContext) -> dict:
    """Quantify one sample against a transcriptome, without aligning.

    One sample per job, matching `quantify` -- adding a thirteenth sample
    costs one job rather than redoing twelve, and each per-sample count is a
    first-class object with its own provenance.

    Runs off the event loop in a worker thread and so cannot touch the
    database: it returns a plain dict for `results._apply_salmon_quantify`.
    """
    salmon = tools.require(tools.salmon())

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("salmon_quantify requires an 'object_id'")

    work = _prepare_workdir(ctx, "salmon_quantify")

    transcriptome_name = Path(
        ctx.payload.get("transcriptome_name") or "transcriptome.fna"
    ).name
    transcriptome = work / transcriptome_name
    transcriptome.unlink(missing_ok=True)
    transcriptome.symlink_to(_resolve_blob(ctx.payload, "transcriptome"))

    reads: list[Path] = []
    for key, default in (("reads", "reads_1.fastq.gz"), ("reads2", "reads_2.fastq.gz")):
        if key == "reads2" and not ctx.payload.get("reads2_blob_id"):
            continue
        name = Path(ctx.payload.get(f"{key}_name") or default).name
        link = work / name
        link.unlink(missing_ok=True)
        link.symlink_to(_resolve_blob(ctx.payload, key))
        reads.append(link)

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    index_dir = work / "index"
    ctx.progress(phase="indexing", pct=0.1, message="building transcriptome index")
    code = run_subprocess(
        ctx,
        salmon_runner.index_command(
            transcriptome=transcriptome,
            index_dir=index_dir,
            salmon_path=salmon.path,
        ),
        log_path=str(log_path),
    )
    if code != 0:
        raise _failure(code, log_path, "salmon index")

    out_dir = work / "quant"
    ctx.progress(phase="quantifying", pct=0.5, message="quantifying transcripts")
    code = run_subprocess(
        ctx,
        salmon_runner.quant_command(
            index_dir=index_dir,
            reads=reads,
            out_dir=out_dir,
            salmon_path=salmon.path,
        ),
        log_path=str(log_path),
    )
    if code != 0:
        raise _failure(code, log_path, "salmon quant")

    quant = salmon_runner.quant_file(out_dir)
    if not quant.exists():
        raise PermanentError(
            "salmon reported success but wrote no quantification",
            details={"expected": str(quant)},
        )

    ctx.progress(phase="summarizing", pct=0.9, message="summing transcripts to genes")

    per_transcript, quant_facts = salmon_runner.parse_quant(
        quant.read_text(errors="replace")
    )
    headers = [
        line
        for line in transcriptome.read_text(errors="replace").splitlines()
        if line.startswith(">")
    ]
    tx2gene = salmon_runner.parse_tx2gene(headers)
    counts, gene_facts = salmon_runner.summarize_to_gene(per_transcript, tx2gene)

    output = work / salmon_runner.output_name(reads[0].name)
    output.write_text(salmon_runner.format_counts(counts))

    facts = {**quant_facts, **gene_facts}

    log.info(
        "salmon_quant_done",
        job_id=ctx.job_id,
        genes_detected=facts.get("genes_detected"),
        transcripts_detected=facts.get("transcripts_detected"),
    )

    return _salmon_result_dict(
        object_id=object_id,
        transcriptome_object_id=ctx.payload.get("transcriptome_object_id"),
        project_id=ctx.payload.get("project_id"),
        job_id=ctx.job_id,
        output_path=str(output),
        tool_version=salmon.version,
        transcriptome_name=transcriptome_name,
        transcriptome_sha256=ctx.payload.get("transcriptome_sha256"),
        facts=facts,
        workdir=str(work),
    )
```

Note: `_resolve_blob(ctx.payload, key)` — confirm its exact signature by reading `backend/app/queue/align_handlers.py` before implementing, and match how other handlers name their blob payload keys.

- [ ] **Step 5: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/queue/test_expression_handlers.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/expression_handlers.py backend/tests/queue/test_expression_handlers.py
git commit -m "feat(queue): quantify a sample with salmon in one job"
```

---

### Task 10: Register the Salmon output as a counts object

**Files:**
- Modify: `backend/app/queue/results.py` (beside `counts_provenance` at ~2765 and `_apply_quantify` at ~2784; add to the applier table at ~3028)
- Test: `backend/tests/queue/test_results.py`

**Interfaces:**
- Consumes: the result dict from Task 9.
- Produces: `_apply_salmon_quantify(result: dict, *, owner: str) -> None`, registered as `"salmon_quantify"` in the applier table; `salmon_provenance(result: dict) -> dict`.

**Key difference from `_apply_quantify`:** that one copies `metadata=dict(bam.metadata)` because the BAM carries `condition` and `sample` forward from the tagged reads. Salmon has no BAM — the parent IS the reads object, so metadata comes from it directly.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/queue/test_results.py`:

```python
class TestSalmonProvenance:
    def test_records_salmon_as_the_quantifier(self):
        from app.queue import results

        prov = results.salmon_provenance(
            {
                "tool_version": "1.10.2",
                "annotation_name": "cds.fna",
                "annotation_sha256": "deadbeef",
                "facts": {"genes_detected": 12},
            }
        )
        # counts_provenance writes counted_by="featurecounts"; the two paths
        # must be distinguishable on the object itself, not only by which job
        # produced it.
        assert prov["counted_by"] == "salmon"
        assert prov["salmon_version"] == "1.10.2"
        assert prov["annotation_sha256"] == "deadbeef"
        assert prov["genes_detected"] == 12

    def test_transcriptome_name_is_carried_for_the_merge_error_message(self):
        from app.queue import results

        prov = results.salmon_provenance(
            {"annotation_name": "cds.fna", "annotation_sha256": "x", "facts": {}}
        )
        assert prov["annotation_name"] == "cds.fna"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_results.py -k SalmonProvenance -v
```

Expected: FAIL with `AttributeError: module 'app.queue.results' has no attribute 'salmon_provenance'`.

- [ ] **Step 3: Implement the provenance and the applier**

In `backend/app/queue/results.py`, after `counts_provenance`:

```python
def salmon_provenance(result: dict) -> dict:
    """The facts a Salmon quantification stamps onto its counts file.

    `counted_by` distinguishes it from featureCounts output on the object
    itself rather than only by which job produced it, because the two are the
    same role and the same format and a user looking at two counts files has
    no other way to tell them apart.

    `annotation_sha256` holds the transcriptome digest -- see
    `expression_handlers._salmon_result_dict` for why that key rather than a
    new one.
    """
    provenance = {
        "counted_by": "salmon",
        "salmon_version": result.get("tool_version"),
        "annotation_name": result.get("annotation_name"),
        "annotation_sha256": result.get("annotation_sha256"),
    }
    provenance.update(result.get("facts") or {})
    return provenance


async def _apply_salmon_quantify(result: dict, *, owner: str) -> None:
    """Turn a finished Salmon run into a counts object.

    The counts descend from both the reads and the transcriptome, for the same
    reason featureCounts output descends from its BAM and its annotation: a
    count is a claim about a gene, and which gene depends entirely on which
    reference was used.

    Metadata is copied from the reads object, which is where `condition` and
    `sample` live. `_apply_quantify` takes them from the BAM instead only
    because a BAM is what it has; this path has no BAM and the reads are the
    parent, so it reads them from the source directly.
    """
    from app.services import object_service, run_service

    output = result.get("output")
    reads_id = result.get("object_id")
    if not output or not reads_id:
        return

    reads = await DataObject.get(PydanticObjectId(reads_id))
    if reads is None:
        log.warning("salmon_quantify_parent_missing", object_id=reads_id)
        return

    parents = [reads.id]
    transcriptome_id = result.get("transcriptome_object_id")
    if transcriptome_id:
        parents.append(PydanticObjectId(transcriptome_id))

    job_id = result.get("job_id")
    try:
        counts = await object_service.ingest_local_file(
            owner=reads.owner,
            project_id=reads.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.COUNTS,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=salmon_provenance(result),
            metadata=dict(reads.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error("salmon_counts_ingest_failed", object_id=reads_id, error=str(e))
        return

    log.info(
        "salmon_quantify_applied", reads_id=reads_id, counts_id=str(counts.id)
    )

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [counts.id], owner=counts.owner)
```

Add to the applier table beside `"quantify": _apply_quantify,`:

```python
    "salmon_quantify": _apply_salmon_quantify,
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/queue/test_results.py -k SalmonProvenance -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/results.py backend/tests/queue/test_results.py
git commit -m "feat(queue): register salmon output as a counts object"
```

---

### Task 11: Add the salmon_quantify node type

**Files:**
- Modify: `backend/app/pipelines/node_types.py` (the launcher functions at ~227 and the `NODE_TYPES` dict at ~614, beside `"quantify"`)
- Modify: `backend/app/services/pipeline_service.py` (add `launch_salmon_quantify`)
- Test: `backend/tests/pipelines/test_node_types.py`

**Interfaces:**
- Consumes: the handler from Task 9.
- Produces: `NODE_TYPES["salmon_quantify"]` with inputs `reads` (FASTQ) and `transcriptome` (FASTA + `ObjectRole.TRANSCRIPT`), output `counts` (TEXT + `ObjectRole.COUNTS`).

**REQ-NODE-1: run the FULL `TestExhaustiveness` class, not just the named test.** Per CLAUDE.md, #355 added a `NodeTypeSpec` and an exclusion in two separate commits; both landed, which satisfied `test_every_launch_function_is_classified` while silently failing `test_no_launcher_is_both_used_and_excluded` in the same class. It stayed red until someone ran the whole file (#366).

- [ ] **Step 1: Read how RunKind is defined and whether a new member is needed**

```bash
grep -rn "class RunKind" -A 30 backend/app/models/
```

If `RunKind.QUANTIFY` is generic enough to cover both, reuse it. If run kinds are used to label runs in the UI, a `SALMON_QUANTIFY` member may be clearer. Decide from what you read, and note the decision in the commit message.

- [ ] **Step 2: Write the failing test**

Append to `backend/tests/pipelines/test_node_types.py`:

```python
class TestSalmonQuantifyNode:
    def test_takes_reads_and_a_transcriptome(self):
        spec = node_types.NODE_TYPES["salmon_quantify"]
        ports = {p.name: p for p in spec.inputs}
        assert ports["reads"].type.format == FormatKind.FASTQ
        assert ports["transcriptome"].type.format == FormatKind.FASTA
        # The role is what keeps protein.faa out of this port: a protein FASTA
        # and a transcriptome FASTA are the same format.
        assert ports["transcriptome"].type.role == ObjectRole.TRANSCRIPT

    def test_output_matches_the_quantify_node_so_de_accepts_it(self):
        # The whole reason no new differential expression entry point is
        # needed: the DE node's input port keys on the role, so any node
        # producing COUNTS feeds it.
        salmon_out = node_types.NODE_TYPES["salmon_quantify"].outputs[0]
        counts_out = node_types.NODE_TYPES["quantify"].outputs[0]
        assert salmon_out.type.format == counts_out.type.format
        assert salmon_out.type.role == counts_out.type.role == ObjectRole.COUNTS
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -k SalmonQuantifyNode -v
```

Expected: FAIL with `KeyError: 'salmon_quantify'`.

- [ ] **Step 4: Add the launcher wrapper**

In `backend/app/pipelines/node_types.py`, beside `_launch_quantify`:

```python
async def _launch_salmon_quantify(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_salmon_quantify(
        reads_id=inputs["reads"],
        transcriptome_id=inputs.get("transcriptome"),
        params=params,
        owner=owner,
    )
```

Match the exact argument names `_launch_quantify` uses by reading it first — the wrapper's shape must match how the dispatcher calls it.

- [ ] **Step 5: Add the node spec**

In `NODE_TYPES`, beside `"quantify"`:

```python
    "salmon_quantify": NodeTypeSpec(
        label="Quantify (Salmon)",
        launch_name="pipeline_service.launch_salmon_quantify",
        launch=_launch_salmon_quantify,
        run_kind=RunKind.QUANTIFY,
        inputs=(
            PortSpec("reads", PortType(format=FormatKind.FASTQ)),
            # The role is load-bearing, not decoration: an NCBI genome
            # download brings protein.faa alongside the CDS FASTA, and both
            # are FormatKind.FASTA. Without the role this port would accept
            # the protein file.
            PortSpec(
                "transcriptome",
                PortType(format=FormatKind.FASTA, role=ObjectRole.TRANSCRIPT),
            ),
        ),
        outputs=(
            PortSpec("counts", PortType(format=FormatKind.TEXT, role=ObjectRole.COUNTS)),
        ),
    ),
```

- [ ] **Step 6: Implement `launch_salmon_quantify`**

In `backend/app/services/pipeline_service.py`, model it on `launch_quantify` — read that function in full first. It must resolve the transcriptome (falling back to the project's single unambiguous TRANSCRIPT-role FASTA, refusing when there is more than one, matching how `launch_quantify` resolves the annotation), compute the transcriptome's sha256 into the payload as `transcriptome_sha256`, and enqueue a `salmon_quantify` job.

- [ ] **Step 7: Run the node test**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -k SalmonQuantifyNode -v
```

Expected: PASS.

- [ ] **Step 8: Run the FULL exhaustiveness class (REQ-NODE-1)**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -v
```

Expected: PASS, including both `test_every_launch_function_is_classified` and `test_no_launcher_is_both_used_and_excluded`. Do not stop at the first green test — read the summary count.

- [ ] **Step 9: Commit**

```bash
git add backend/app/pipelines/node_types.py backend/app/services/pipeline_service.py backend/tests/pipelines/test_node_types.py
git commit -m "feat(pipelines): add a salmon quantification node"
```

---

### Task 12: Suggest Salmon for RNA-seq projects

**Files:**
- Modify: `backend/app/services/suggestion_service.py` (beside `build_quantify_card` at ~1911; the card table at ~2039 and the rule list at ~2050)
- Test: `backend/tests/services/test_suggestion_service.py`

**Interfaces:**
- Consumes: `tools.salmon()` (Task 1); the node from Task 11.
- Produces: `build_salmon_quantify_card(obj, transcriptomes) -> SuggestionCard | None`, registered with kind `"salmon_quantify"`.

**REQ-CARD-1 and REQ-CARD-2** are the two failures this rule must not repeat: `protein.faa` counted as a usable reference, and one reference stored twice counted as two.

- [ ] **Step 1: Read the existing card to match its shape**

```bash
sed -n '1900,1990p' backend/app/services/suggestion_service.py
sed -n '2030,2060p' backend/app/services/suggestion_service.py
```

- [ ] **Step 2: Write the failing test**

Append to `backend/tests/services/test_suggestion_service.py`:

```python
class TestSalmonQuantifyCard:
    """The two mistakes this repo has already made with reference-picking
    rules, pinned so a third does not happen quietly.
    """

    def test_offers_salmon_when_a_transcriptome_is_available(self):
        card = suggestion_service.build_salmon_quantify_card(
            _fastq_object(), [_transcriptome_object()]
        )
        assert card is not None
        assert card.kind == "salmon_quantify"

    def test_does_not_count_a_protein_fasta_as_a_transcriptome(self):
        # REQ-CARD-1. protein.faa is FASTA with ObjectRole.PROTEIN. This is
        # the same bug that made the Actions tab count it as an alignable
        # reference, which made a project with one usable reference refuse to
        # align.
        card = suggestion_service.build_salmon_quantify_card(
            _fastq_object(), [_protein_object()]
        )
        assert card is None or not card.available

    def test_the_same_transcriptome_twice_counts_once(self):
        # REQ-CARD-2. Two records of one reference must not read as an
        # ambiguous choice between two references.
        tx = _transcriptome_object()
        card = suggestion_service.build_salmon_quantify_card(
            _fastq_object(), [tx, tx]
        )
        assert card is not None
        assert card.available

    def test_card_is_unavailable_when_salmon_is_not_installed(self, monkeypatch):
        # Asserted in this direction deliberately. The image ships Salmon
        # installed, so asserting a card is *available* passes whether or not
        # the patch took effect -- only the unavailable direction fails when
        # the seam breaks.
        monkeypatch.setattr(
            suggestion_service.tools,
            "salmon",
            lambda: _unavailable_tool("salmon"),
        )
        card = suggestion_service.build_salmon_quantify_card(
            _fastq_object(), [_transcriptome_object()]
        )
        assert card is not None
        assert not card.available
        assert "not installed" in (card.unavailable or "").lower()
```

The helpers `_fastq_object`, `_transcriptome_object`, `_protein_object`, `_unavailable_tool` must be written to match how the existing tests in this file build fixtures — read the top of the file and reuse its helpers rather than inventing new ones.

- [ ] **Step 3: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -k SalmonQuantifyCard -v
```

Expected: FAIL with `AttributeError: ... has no attribute 'build_salmon_quantify_card'`.

- [ ] **Step 4: Implement the card**

Follow `build_quantify_card`'s structure exactly. Requirements:

- Filter candidate references on `role == ObjectRole.TRANSCRIPT`, never on format alone.
- De-duplicate by content digest (or whatever `build_quantify_card` uses for the annotation equivalent) before deciding whether the choice is ambiguous.
- When `tools.salmon()` is unavailable, return a card whose `unavailable` reason says Salmon is not installed.
- The card copy must state the CDS caveat from the spec's "Known limitation", in one short user-facing sentence.

- [ ] **Step 5: Register the card**

Add `"salmon_quantify": "salmon_quantify",` to the kind table and the rule to the rule list, matching the existing entries' shape.

- [ ] **Step 6: Run the suggestion tests**

```bash
./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -v
```

Expected: PASS.

- [ ] **Step 7: Verify the rule against the real database (REQ-CARD-3)**

Both prior suggestion-rule bugs passed a green suite because their fixtures already looked the way the rules expected. From the MAIN checkout:

```bash
docker compose exec api python -c "import asyncio; from app.services import suggestion_service; print('inspect real project objects here')"
```

Write a probe that loads a real project's objects and prints which ones the rule picks as transcriptomes. Confirm `protein.faa` is not among them. If no project on this machine has a CDS FASTA, say so explicitly in the commit message rather than claiming the check passed.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat(pipelines): offer salmon where a transcriptome is available"
```

---

### Task 13: End-to-end verification and full suite

**Files:**
- Modify: none expected (fix whatever breaks)

**Interfaces:**
- Consumes: everything above.
- Produces: a verified working feature.

- [ ] **Step 1: Rebuild the image so Salmon is present**

From the MAIN checkout:

```bash
docker compose up -d --build api web worker
```

- [ ] **Step 2: Confirm Salmon is installed and probed**

```bash
docker compose exec api python -c "from app.pipelines import tools; t = tools.salmon(); print(t.name, t.version, t.available, t.error)"
```

Expected: `salmon 1.10.2 True None`.

- [ ] **Step 3: Bring up the worktree stack for testing**

```bash
./ops/worktree-up.sh
```

- [ ] **Step 4: Run a real quantification**

In the UI at localhost:5273, on a project with FASTQ reads and a CDS FASTA: download a CDS FASTA via the NCBI dialog if the project lacks one, then run the Salmon card. Confirm a counts object appears with `counted_by: salmon` in its facts and a non-zero `genes_detected`.

If `parse_tx2gene` raises here, that is Task 4's verification having been wrong — fix the parser against the real deflines, do not add a fallback.

- [ ] **Step 5: Run differential expression on the result**

With at least two samples per condition quantified by Salmon, run the DE node on them. Confirm it produces a results table (success criterion 3).

Then confirm the negative: a design mixing one Salmon counts object with one featureCounts counts object is refused by `merge_counts` with the annotation-mismatch message. That refusal is correct behaviour, not a bug.

- [ ] **Step 6: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: no new failures. Read the count, not just the exit code.

- [ ] **Step 7: Bring the worktree stack down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 8: Commit any fixes**

```bash
git add -A
git commit -m "fix(pipelines): correct salmon defline parsing against real data"
```

(Only if Step 4 or 5 required changes. If nothing broke, skip this step.)

---

### Task 14: Close out the issue

**Files:**
- Modify: `docs/TODO.md` / `docs/TODO-done.md` only if an entry covers this work (check first: `grep -i salmon docs/TODO.md`)

- [ ] **Step 1: Rebase onto current main**

```bash
git fetch origin main
git rebase origin/main
```

- [ ] **Step 2: Verify the work survived the rebase**

```bash
git diff origin/main...HEAD --stat
```

Confirm the file list matches what was intended and nothing looks reverted.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

The PR title lands in the release notes verbatim. Use: `feat(pipelines): quantify RNA-seq without aligning, using Salmon`. The body must include `Closes #621`.

- [ ] **Step 4: Label the PR**

```bash
gh pr edit <N> --add-label "type:feature" --add-label "area:pipelines"
```

- [ ] **Step 5: Poll CI until every check reports pass**

```bash
gh pr checks <N>
```

```bash
gh pr view <N> --json mergeable,mergeStateStatus
```

Keep polling until every check is pass/fail, not pending. Fix anything red — `ruff` import ordering (`I001`) is the check that has caught this repo before, and it is not exercised by the local suite.

- [ ] **Step 6: Merge**

```bash
gh pr merge <N> --rebase --delete-branch
```

- [ ] **Step 7: Remove the worktree**

Per CLAUDE.md, a merged PR is the signal to tear the worktree down.

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| Salmon installs, apt not vendored | 1 |
| `test_every_tool_is_documented` passes; verified license | 2 |
| `parse_quant` | 3 |
| REQ-TX2GENE-2 (verify real defline) | 4 |
| REQ-TX2GENE-1 (no fallback, hard refusal) | 5 |
| `summarize_to_gene`, round after summing | 5 |
| `index_command` / `quant_command`, `-l A` | 6 |
| Output readable by `counts_runner.parse_counts` | 7 |
| REQ-INDEX-1 (SidecarRole) | 8 |
| Handler; `annotation_sha256` = transcriptome digest | 9 |
| COUNTS object registration; metadata from reads | 10 |
| REQ-NODE-1 (full TestExhaustiveness) | 11 |
| REQ-CARD-1 (protein.faa), REQ-CARD-2 (dedupe), REQ-CARD-3 (real DB) | 12 |
| Success criteria 2 and 3 (end-to-end + DE) | 13 |
| CDS caveat surfaced in `ToolMeta.usage` and card copy | 2, 12 |

No spec requirement is unassigned.

**Known gaps in this plan, stated rather than hidden:**

- **Task 11 Step 6** (`launch_salmon_quantify`) does not carry literal code. `launch_quantify` was not read in full while writing this plan, and inventing a signature for a function that must match an unread dispatcher contract would be a guess dressed as instruction. The step names the file, the model function, and the four required behaviours.
- **Task 12 Step 4** similarly gives requirements rather than a literal card body, because the fixture helpers and `SuggestionCard` construction shape in that file were not read in full.
- **Task 8** deliberately begins with a `grep` rather than an assertion about `_SIDECAR_ROLES`, because the spec flags it as a registry to confirm rather than assume.

An executor hitting any of these should read the named model function first. That is the intended workflow, not a shortcut around a missing detail.

**Type consistency:** `parse_quant -> tuple[dict[str, float], dict]` feeds `summarize_to_gene(per_tx: dict[str, float], ...)` (Task 3 → 5). `summarize_to_gene -> tuple[dict[str, int], dict]` feeds `format_counts(counts: dict[str, int])` (Task 5 → 7). `parse_tx2gene -> dict[str, str]` is the second argument to `summarize_to_gene`. Facts keys are distinct across the two producers (`transcripts_*` from `parse_quant`, `genes_*` from `summarize_to_gene`) and merged in Task 9 without collision.
