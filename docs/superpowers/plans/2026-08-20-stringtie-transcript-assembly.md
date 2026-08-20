# StringTie Transcript Assembly Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reference-guided transcript assembly (StringTie) to the RNA-seq path, so the app can propose transcript models that are not already in the reference annotation.

**Architecture:** Follows the `salmon_quantify` template end to end: a pure-function runner (`stringtie_runner.py`) split from a `SUBPROCESS` job handler, with a `_apply_*` in `results.py` that ingests the produced GTF as a first-class object. The suggestion card gates on the BAM's existing `facts.aligned_by` fact, so only HISAT2/STAR alignments are offered the capability. A new `ObjectRole.ASSEMBLED_TRANSCRIPTS` keeps a produced transcript hypothesis distinct from an authoritative downloaded annotation.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor (MongoDB), Redis-backed job queue, pytest. StringTie 2.2.1 from Debian trixie.

**Spec:** `docs/superpowers/specs/2026-08-20-stringtie-transcript-assembly-design.md`

## Global Constraints

- **StringTie version:** `2.2.1+ds-3+b2` from Debian trixie, arm64. Verified present in the trixie apt repo on 2026-08-20; **no install script needed** (unlike Salmon, whose trixie arm64 build crashes with SIGILL — see `backend/Dockerfile:298`).
- **License:** `MIT`. Verified 2026-08-20 via `gh api repos/gpertea/stringtie`, not recalled. `test_every_tool_is_documented` requires `homepage`, `citation`, `license`, `usage` on every `TOOL_META` entry.
- **Citation:** Pertea M, Pertea GM, Antonescu CM, Chang TC, Mendell JT, Salzberg SL. StringTie enables improved reconstruction of a transcriptome from RNA-seq reads. Nature Biotechnology. 2015;33(3):290-295. DOI `10.1038/nbt.3122`.
- **Verified flags** (from `stringtie --help` on 2.2.1): `-G <ref>` reference annotation, `-o <out>` output GTF, `-p <n>` threads. `--version` prints a bare `2.2.1` to stdout and **exits 0** — the same shape as Salmon, so `tools._probe` works unchanged with no special-casing.
- **Verified GTF attributes** (from real StringTie output, not recall): a transcript matching the reference carries `reference_id "T1"; ref_gene_id "G1";`. A novel transcript carries **neither**. Counting novel transcripts = transcript-feature lines lacking `reference_id`.
- **Scope:** Assembly only. No `--merge` (issue #703), no `-e`/`-B` quantify-only (issue #704), no de novo (no-`-G`) mode.
- **Testing:** From a worktree, always `./backend/run-worktree-tests.sh`, **never** `docker compose exec api pytest` — the latter silently tests main's code, not the worktree's.
- **Never call `python -m pytest`.** The image puts a tool venv ahead of the app interpreter on `PATH`. Call `pytest` directly; when a real interpreter is needed use `/usr/local/bin/python3.12`.
- **Commits:** Conventional Commits. `feat`/`fix` reach the changelog; `docs`/`test`/`chore`/`refactor` are filtered out of user-facing notes.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `backend/app/pipelines/stringtie_runner.py` | Pure functions: `assemble_command()` argv construction, `parse_gtf()` output counting. No queue, no DB, no filesystem writes. |
| `backend/tests/pipelines/test_stringtie_runner.py` | Unit tests for the above. |

**Modified:**

| File | Change |
|---|---|
| `backend/Dockerfile:95` | Add `stringtie` to the trixie apt block. |
| `backend/app/config.py:160` | `stringtie_path` setting. |
| `backend/app/pipelines/tools.py` | `stringtie()` probe, probe-list entry, `cache_clear()`, `TOOL_META` entry. |
| `backend/app/models/run.py` | `RunKind.TRANSCRIPT_ASSEMBLY`. |
| `backend/app/models/object.py` | `ObjectRole.ASSEMBLED_TRANSCRIPTS`. |
| `backend/app/metadata/schemas.py:475` | Add role to `FORMAT_DERIVED_ROLES`. |
| `backend/app/queue/expression_handlers.py` | `transcript_assembly` handler. |
| `backend/app/queue/results.py` | `_apply_transcript_assembly` + dispatch entry. |
| `backend/app/services/pipeline_service.py` | `launch_transcript_assembly()`; `_is_annotation` exclusion. |
| `backend/app/pipelines/node_types.py` | `transcript_assembly` node spec + launcher adapter. |
| `backend/app/api/v1/pipelines.py` | `POST /pipelines/transcript-assembly`. |
| `backend/app/services/suggestion_service.py` | `build_transcript_assembly_card()` + registration. |
| `backend/app/pipelines/align_params.py:463,464,499` | `dta` default → `True`; update comment. |
| `backend/app/pipelines/aligner_registry.py:908-919` | `dta` `default=True`; update help text. |

---

## Task 1: Install and register StringTie

**Files:**
- Modify: `backend/Dockerfile:95-115`
- Modify: `backend/app/config.py:160`
- Modify: `backend/app/pipelines/tools.py` (probe near `salmon()` at :845, probe list at :932, `cache_clear` at :2801, `TOOL_META` near :2399)
- Test: `backend/tests/pipelines/test_tools.py` (existing `test_every_tool_is_documented`)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `tools.stringtie() -> Tool` with `.available: bool`, `.path: str`, `.name: str`, `.error: str | None`. Later tasks call `tools.require(tools.stringtie())` and `tools.stringtie().available`.

- [ ] **Step 1: Add StringTie to the image**

In `backend/Dockerfile`, add `stringtie` to the alphabetically-adjacent position in the existing `apt-get install` block (after `sra-toolkit`, near `subread`):

```dockerfile
        subread \
        stringtie \
```

No install script is needed: trixie ships `2.2.1+ds-3+b2` for arm64 and it runs correctly, unlike Salmon.

- [ ] **Step 2: Add the config setting**

In `backend/app/config.py`, beside `salmon_path` at line 160:

```python
    stringtie_path: str = "stringtie"
```

- [ ] **Step 3: Add the probe**

In `backend/app/pipelines/tools.py`, immediately after the `salmon()` probe (~line 851):

```python
@lru_cache(maxsize=1)
def stringtie() -> Tool:
    # `stringtie --version` exits zero and prints a bare "2.2.1" to stdout,
    # so none of featureCounts' special-casing applies. Verified against the
    # Debian trixie binary (2.2.1+ds-3+b2) rather than recalled.
    return _probe("stringtie", settings.stringtie_path, ["--version"])
```

- [ ] **Step 4: Register the probe in the two lists**

In the probe list (~line 932), beside `salmon()`:

```python
        stringtie(),
```

In the cache-clearing function (~line 2801), beside `salmon.cache_clear()`:

```python
    stringtie.cache_clear()
```

- [ ] **Step 5: Add the TOOL_META entry**

In `TOOL_META`, after the `"salmon"` entry (~line 2440):

```python
    "stringtie": ToolMeta(
        pipelines=(PipelineType.EXPRESSION,),
        one_liner="Assembles transcripts from spliced RNA-seq alignments",
        summary=(
            "Reconstructs the transcripts present in a sample from a "
            "splice-aware alignment, guided by a reference annotation. "
            "Unlike counting or transcriptome quantification, which can only "
            "measure transcripts someone has already annotated, StringTie "
            "proposes transcript models the annotation does not contain -- "
            "unannotated isoforms, alternative ends, and genes missing from "
            "the reference entirely."
        ),
        strengths=(
            "Finds isoforms absent from the reference annotation",
            "Guided by a reference, so assembled transcripts keep stable gene identity",
            "Handles the spliced alignments HISAT2 and STAR produce natively",
            "Fast enough to run per sample rather than on a pooled alignment",
        ),
        homepage="https://ccb.jhu.edu/software/stringtie/",
        repository="https://github.com/gpertea/stringtie",
        citation=(
            "Pertea M, Pertea GM, Antonescu CM, Chang TC, Mendell JT, "
            "Salzberg SL. StringTie enables improved reconstruction of a "
            "transcriptome from RNA-seq reads. Nature Biotechnology. "
            "2015;33(3):290-295."
        ),
        citation_url="https://doi.org/10.1038/nbt.3122",
        # Verified 2026-08-20 against the upstream repository via
        # `gh api repos/gpertea/stringtie`, not recalled.
        license="MIT",
        usage=(
            "Runs reference-guided against one splice-aware alignment at a "
            "time, producing a GTF of assembled transcripts. A reference "
            "annotation is required rather than optional: without one the "
            "assembled transcripts carry generated identifiers that nothing "
            "downstream can match to a gene. Offered only for alignments "
            "produced by HISAT2 or STAR -- a DNA-seq alignment from bwa-mem2 "
            "or minimap2 has no splice structure to assemble. Transcripts "
            "the reference already contains are reported with their original "
            "identifiers; the rest are novel models this run proposed, "
            "counted separately on the result. Abundance-only mode and "
            "multi-sample merging are not wired up."
        ),
    ),
```

- [ ] **Step 6: Rebuild the image and verify the binary**

```bash
./ops/worktree-up.sh
```

Then:

```bash
docker exec $(docker ps --filter name=api --format '{{.Names}}' | head -1) stringtie --version
```

Expected: `2.2.1`

- [ ] **Step 7: Run the documentation test**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_tools.py -q
```

Expected: PASS, including `test_every_tool_is_documented`.

- [ ] **Step 8: Commit**

```bash
git add backend/Dockerfile backend/app/config.py backend/app/pipelines/tools.py
git commit -m "feat(pipelines): install and register StringTie transcript assembler"
```

---

## Task 2: The runner — command construction and GTF parsing

**Files:**
- Create: `backend/app/pipelines/stringtie_runner.py`
- Test: `backend/tests/pipelines/test_stringtie_runner.py`

**Interfaces:**
- Consumes: `tools.stringtie()` from Task 1 (only `.path`, passed in as a string).
- Produces:
  - `assemble_command(*, bam: Path, annotation: Path, out_gtf: Path, stringtie_path: str, threads: int = 1) -> list[str]`
  - `parse_gtf(text: str) -> dict` returning keys `transcript_count: int`, `novel_transcript_count: int`, `gene_count: int`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/pipelines/test_stringtie_runner.py`:

```python
"""Unit tests for the StringTie runner.

The GTF fixtures here are real StringTie 2.2.1 output, produced against a
synthetic BAM and a two-exon reference GTF on 2026-08-20, not hand-written
from memory. The attribute names this module keys on -- `reference_id` present
on a transcript the reference already had, absent on one StringTie proposed --
are the single fact `parse_gtf` depends on, so they are worth pinning to real
output rather than recall.
"""

from pathlib import Path

from app.pipelines import stringtie_runner

# A transcript StringTie matched to the reference: carries reference_id.
MATCHED_GTF = """\
# stringtie in.bam -G ref.gtf -o out.gtf -p 2
# StringTie version 2.2.1
chr1\tStringTie\ttranscript\t101\t500\t1000\t+\t.\tgene_id "STRG.1"; transcript_id "STRG.1.1"; reference_id "T1"; ref_gene_id "G1"; cov "30.000000"; FPKM "3333333.500000"; TPM "1000000.000000";
chr1\tStringTie\texon\t101\t200\t1000\t+\t.\tgene_id "STRG.1"; transcript_id "STRG.1.1"; exon_number "1"; reference_id "T1"; ref_gene_id "G1"; cov "30.000000";
chr1\tStringTie\texon\t401\t500\t1000\t+\t.\tgene_id "STRG.1"; transcript_id "STRG.1.1"; exon_number "2"; reference_id "T1"; ref_gene_id "G1"; cov "30.000000";
"""

# A transcript StringTie proposed: no reference_id, no ref_gene_id.
NOVEL_GTF = """\
# stringtie in.bam -G ref.gtf -o out.gtf -p 2
# StringTie version 2.2.1
chr1\tStringTie\ttranscript\t1201\t1700\t1000\t+\t.\tgene_id "STRG.1"; transcript_id "STRG.1.1"; cov "60.000000"; FPKM "5000000.000000"; TPM "1000000.000000";
chr1\tStringTie\texon\t1201\t1300\t1000\t+\t.\tgene_id "STRG.1"; transcript_id "STRG.1.1"; exon_number "1"; cov "60.000000";
chr1\tStringTie\texon\t1601\t1700\t1000\t+\t.\tgene_id "STRG.1"; transcript_id "STRG.1.1"; exon_number "2"; cov "60.000000";
"""


def test_assemble_command_builds_reference_guided_argv():
    argv = stringtie_runner.assemble_command(
        bam=Path("/w/in.bam"),
        annotation=Path("/w/ref.gtf"),
        out_gtf=Path("/w/out.gtf"),
        stringtie_path="/usr/bin/stringtie",
        threads=4,
    )
    assert argv == [
        "/usr/bin/stringtie",
        "/w/in.bam",
        "-G",
        "/w/ref.gtf",
        "-o",
        "/w/out.gtf",
        "-p",
        "4",
    ]


def test_assemble_command_defaults_to_one_thread():
    argv = stringtie_runner.assemble_command(
        bam=Path("/w/in.bam"),
        annotation=Path("/w/ref.gtf"),
        out_gtf=Path("/w/out.gtf"),
        stringtie_path="stringtie",
    )
    assert argv[-2:] == ["-p", "1"]


def test_parse_gtf_counts_a_matched_transcript_as_not_novel():
    facts = stringtie_runner.parse_gtf(MATCHED_GTF)
    assert facts["transcript_count"] == 1
    assert facts["novel_transcript_count"] == 0
    assert facts["gene_count"] == 1


def test_parse_gtf_counts_a_transcript_without_reference_id_as_novel():
    facts = stringtie_runner.parse_gtf(NOVEL_GTF)
    assert facts["transcript_count"] == 1
    assert facts["novel_transcript_count"] == 1


def test_parse_gtf_ignores_exon_lines_when_counting_transcripts():
    # Both fixtures carry two exon lines per transcript. A parser keying on
    # the attribute rather than the feature column would count three.
    facts = stringtie_runner.parse_gtf(MATCHED_GTF + NOVEL_GTF)
    assert facts["transcript_count"] == 2
    assert facts["novel_transcript_count"] == 1


def test_parse_gtf_on_empty_output_reports_zeroes_rather_than_raising():
    # StringTie exits zero on a BAM with no assemblable coverage, writing a
    # header-only GTF. That is an empty result, not a failure.
    facts = stringtie_runner.parse_gtf("# StringTie version 2.2.1\n")
    assert facts == {
        "transcript_count": 0,
        "novel_transcript_count": 0,
        "gene_count": 0,
    }
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_stringtie_runner.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.pipelines.stringtie_runner'`.

- [ ] **Step 3: Write the runner**

Create `backend/app/pipelines/stringtie_runner.py`:

```python
"""Building and reading a StringTie run.

Kept separate from the job handler for the same reason `salmon_runner.py` is:
the parts worth testing -- command construction and output parsing -- are pure
functions over strings and paths, with no queue or filesystem involved.

The novel-transcript count is the part that earns the care. It is the only
number here that says what this tool did that no other tool in the app can do,
and it rests on a single fact about StringTie's output format: a transcript
the reference annotation already contained is emitted with a `reference_id`
attribute, and one StringTie proposed is not. Verified against real StringTie
2.2.1 output in both directions rather than recalled -- see the fixtures in
tests/pipelines/test_stringtie_runner.py.
"""

import re
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

# GTF is tab-separated with the feature type in column 3. Keying on the
# column rather than searching the line for "transcript" matters: the word
# also appears inside `transcript_id`, which every exon line carries, so a
# substring match would count each transcript once per exon.
_TRANSCRIPT_FEATURE = "transcript"

_GENE_ID_RE = re.compile(r'gene_id "([^"]+)"')


def assemble_command(
    *,
    bam: Path,
    annotation: Path,
    out_gtf: Path,
    stringtie_path: str,
    threads: int = 1,
) -> list[str]:
    """Argv for a reference-guided assembly of one alignment.

    `-G` is not optional here even though StringTie allows omitting it.
    Without a reference every assembled transcript gets a generated
    identifier (`STRG.1.1`) that nothing downstream can match to a gene, so
    the output would be uninterpretable rather than merely less informative.
    """
    return [
        stringtie_path,
        str(bam),
        "-G",
        str(annotation),
        "-o",
        str(out_gtf),
        "-p",
        str(threads),
    ]


def parse_gtf(text: str) -> dict:
    """Assembled-transcript counts from a StringTie GTF.

    `novel_transcript_count` is transcripts carrying no `reference_id` --
    the models this run proposed rather than measured. It is reported
    alongside the total rather than instead of it for the same reason
    `salmon_runner.parse_quant` reports `transcripts_detected` next to the
    total: the total alone cannot separate "this sample assembled well" from
    "this annotation already described everything here", and the two numbers
    move differently in each case.

    An empty GTF is an empty result, not an error. StringTie exits zero on an
    alignment with too little coverage to assemble anything, writing only its
    header comments, and a caller that treated that as a failure would report
    a crash where the honest answer is "nothing assembled".
    """
    transcript_count = 0
    novel_transcript_count = 0
    genes: set[str] = set()

    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 9 or parts[2] != _TRANSCRIPT_FEATURE:
            continue

        attributes = parts[8]
        transcript_count += 1
        if "reference_id " not in attributes:
            novel_transcript_count += 1

        match = _GENE_ID_RE.search(attributes)
        if match:
            genes.add(match.group(1))

    return {
        "transcript_count": transcript_count,
        "novel_transcript_count": novel_transcript_count,
        "gene_count": len(genes),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_stringtie_runner.py -q
```

Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/stringtie_runner.py backend/tests/pipelines/test_stringtie_runner.py
git commit -m "feat(pipelines): add StringTie command construction and GTF parsing"
```

---

## Task 3: Model members — RunKind, ObjectRole, and the schemas registry

**Files:**
- Modify: `backend/app/models/run.py:51-58`
- Modify: `backend/app/models/object.py:163`
- Modify: `backend/app/metadata/schemas.py:475-500`
- Test: `backend/tests/storage/test_metadata_schemas.py` (existing exhaustiveness test)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `RunKind.TRANSCRIPT_ASSEMBLY` (value `"transcript_assembly"`) and `ObjectRole.ASSEMBLED_TRANSCRIPTS` (value `"assembled_transcripts"`), used by Tasks 4-7.

- [ ] **Step 1: Run the exhaustiveness test first, to see it green**

```bash
./backend/run-worktree-tests.sh tests/storage/test_metadata_schemas.py -q
```

Expected: PASS. This is the baseline — the next step deliberately breaks it.

- [ ] **Step 2: Add the RunKind member**

In `backend/app/models/run.py`, after `QUANTIFY` (~line 51):

```python
    # Assembling transcript models from a spliced alignment. Separate from
    # QUANTIFY for the same display-and-grouping reason DIFFERENTIAL_EXPRESSION
    # is: "assembled transcripts" and "counted one sample" are not the same
    # line in an activity view. Keeping it distinct also means this node needs
    # no `run_tool` discriminator to stay unique -- see the comment on
    # node_types.py's salmon_quantify, which must claim run_tool="salmon"
    # precisely because it shares QUANTIFY with featureCounts.
    TRANSCRIPT_ASSEMBLY = "transcript_assembly"
```

- [ ] **Step 3: Add the ObjectRole member**

In `backend/app/models/object.py`, after `ASSEMBLY_GRAPH` (~line 163):

```python
    # Transcript models assembled from an alignment, as GTF. A separate role
    # from ANNOTATION because the two are the same format carrying opposite
    # authority: a downloaded GFF3 is what is known about the organism, and
    # this is what one run proposed about one sample. The split is the same
    # one that keeps COUNTS and DE_RESULTS apart.
    #
    # Note honestly what this role does and does not buy. Every gate that
    # decides "is this an annotation" is format-first, because an ingested
    # GFF/GTF carries role=None (see pipeline_service._is_annotation), so
    # this role does not by itself keep assembled transcripts out of an
    # annotation picker. The explicit exclusion in _is_annotation does that.
    # This role is what makes the distinction visible and queryable.
    ASSEMBLED_TRANSCRIPTS = "assembled_transcripts"
```

- [ ] **Step 4: Run the exhaustiveness test to verify it now fails**

```bash
./backend/run-worktree-tests.sh tests/storage/test_metadata_schemas.py -q
```

Expected: FAIL — `assert set(ObjectRole) == set(schemas.ROLE_FIELDS) | schemas.FORMAT_DERIVED_ROLES`, reporting `ASSEMBLED_TRANSCRIPTS` as unaccounted for. This is the forcing function working as designed.

- [ ] **Step 5: Account for the role in schemas.py**

In `backend/app/metadata/schemas.py`, inside the `FORMAT_DERIVED_ROLES` frozenset, after `ObjectRole.DE_RESULTS`:

```python
        # ASSEMBLED_TRANSCRIPTS belongs here for DE_RESULTS' reason directly
        # above: there is nothing to ask a user about it. Which alignment,
        # which reference annotation, which tool and version -- all of it is
        # provenance the applier records from the run that produced it, and a
        # transcript assembly nobody produced here is not a thing that exists.
        # The sample-level metadata (condition, sample_id, batch) is already
        # COMMON_FIELDS and travels forward from the reads.
        ObjectRole.ASSEMBLED_TRANSCRIPTS,
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/storage/test_metadata_schemas.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/run.py backend/app/models/object.py backend/app/metadata/schemas.py
git commit -m "feat(models): add transcript-assembly run kind and assembled-transcripts role"
```

---

## Task 4: The job handler

**Files:**
- Modify: `backend/app/queue/expression_handlers.py` (imports at :23, new handler after `salmon_quantify` ends ~:430)
- Test: `backend/tests/queue/test_expression_handlers.py`

**Interfaces:**
- Consumes: `stringtie_runner.assemble_command` / `parse_gtf` (Task 2), `tools.stringtie()` (Task 1).
- Produces: a `transcript_assembly` handler returning a dict with keys `object_id`, `job_id`, `output: {"tmp_path": str, "name": str}`, `assembled_by: "stringtie"`, `transcript_count`, `novel_transcript_count`, `gene_count`. Task 5's `_apply_transcript_assembly` consumes exactly this shape.

**This handler has one subprocess call, not two.** `salmon_quantify` is the
template for everything else here, but do not mirror its index step:
StringTie runs directly against the BAM, so there is no index to build, no
`index_dir` in the workdir, and no `SidecarRole` member to add. A second
`run_subprocess` call in this handler means something has gone wrong.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/queue/test_expression_handlers.py`:

```python
def test_transcript_assembly_result_dict_carries_counts_and_output(tmp_path):
    """The dict `results._apply_transcript_assembly` consumes.

    Asserted as a unit rather than through a real subprocess because the
    handler's contract with results.py is the part that breaks silently: a
    renamed key here fails nothing until a real job produces an object with
    no facts on it.
    """
    from app.queue import expression_handlers

    out_gtf = tmp_path / "sample.transcripts.gtf"
    out_gtf.write_text(
        '# StringTie version 2.2.1\n'
        'chr1\tStringTie\ttranscript\t101\t500\t1000\t+\t.\t'
        'gene_id "STRG.1"; transcript_id "STRG.1.1"; reference_id "T1";\n'
        'chr1\tStringTie\ttranscript\t1201\t1700\t1000\t+\t.\t'
        'gene_id "STRG.2"; transcript_id "STRG.2.1";\n'
    )

    result = expression_handlers._transcript_assembly_result_dict(
        object_id="64b7f0000000000000000001",
        job_id="64b7f0000000000000000002",
        out_gtf=out_gtf,
        name="sample.transcripts.gtf",
    )

    assert result["assembled_by"] == "stringtie"
    assert result["transcript_count"] == 2
    assert result["novel_transcript_count"] == 1
    assert result["gene_count"] == 2
    assert result["output"]["name"] == "sample.transcripts.gtf"
    assert result["output"]["tmp_path"] == str(out_gtf)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_expression_handlers.py -q -k transcript_assembly
```

Expected: FAIL — `AttributeError: module 'app.queue.expression_handlers' has no attribute '_transcript_assembly_result_dict'`.

- [ ] **Step 3: Add the import**

In `backend/app/queue/expression_handlers.py` line 23, extend the existing import:

```python
from app.pipelines import counts_runner, de_runner, salmon_runner, stringtie_runner, tools
```

- [ ] **Step 4: Write the result-dict helper and the handler**

Append to `backend/app/queue/expression_handlers.py`:

```python
def _transcript_assembly_result_dict(
    *,
    object_id: str,
    job_id: str,
    out_gtf: Path,
    name: str,
) -> dict:
    """The dict `results._apply_transcript_assembly` consumes.

    Split out of the handler for the same reason `_salmon_result_dict` is:
    the handler runs in a worker thread against a real subprocess, and this
    is the part with a contract worth testing on its own.
    """
    facts = stringtie_runner.parse_gtf(out_gtf.read_text())
    return {
        "object_id": object_id,
        "job_id": job_id,
        "output": {"tmp_path": str(out_gtf), "name": name},
        "assembled_by": "stringtie",
        **facts,
    }


@handler(
    "transcript_assembly",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    # StringTie holds the current locus's read bundle plus its splice graph in
    # memory and streams the BAM past it, so peak memory tracks locus depth
    # rather than genome size. 4 GB covers a vertebrate RNA-seq sample.
    # HEAVY io because it reads the BAM end to end.
    resources=JobResources(cpu=4, mem_mb=4096, io=IoClass.HEAVY),
    max_attempts=2,
)
def transcript_assembly(ctx: JobContext) -> dict:
    """Assemble transcripts from one splice-aware alignment.

    One alignment per job, matching `quantify` and `salmon_quantify`: each
    per-sample assembly is a first-class object with its own provenance.

    Runs off the event loop in a worker thread and so cannot touch the
    database: it returns a plain dict for `results._apply_transcript_assembly`.
    """
    stringtie = tools.require(tools.stringtie())

    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("transcript_assembly requires an 'object_id'")

    work = _prepare_workdir(ctx, "transcript_assembly")

    bam_name = Path(ctx.payload.get("bam_name") or "alignment.bam").name
    bam = work / bam_name
    bam.unlink(missing_ok=True)
    bam.symlink_to(_resolve_blob(ctx.payload, "bam"))

    annotation_name = Path(
        ctx.payload.get("annotation_name") or "annotation.gtf"
    ).name
    annotation = work / annotation_name
    annotation.unlink(missing_ok=True)
    annotation.symlink_to(_resolve_blob(ctx.payload, "annotation"))

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    stem = Path(bam_name).stem
    out_gtf = work / f"{stem}.transcripts.gtf"

    ctx.progress(phase="assembling", pct=0.1, message="assembling transcripts")
    code = run_subprocess(
        ctx,
        stringtie_runner.assemble_command(
            bam=bam,
            annotation=annotation,
            out_gtf=out_gtf,
            stringtie_path=stringtie.path,
            threads=int(ctx.payload.get("threads") or 4),
        ),
        log_path=str(log_path),
    )
    if code != 0:
        raise _failure(code, log_path, "stringtie assemble")

    if not out_gtf.exists():
        raise PermanentError(
            "StringTie reported success but wrote no GTF. The log names the "
            f"reason: {log_path}"
        )

    ctx.progress(phase="assembling", pct=0.9, message="reading assembled transcripts")
    return _transcript_assembly_result_dict(
        object_id=str(object_id),
        job_id=str(ctx.job_id),
        out_gtf=out_gtf,
        name=out_gtf.name,
    )
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/queue/test_expression_handlers.py -q
```

Expected: PASS, including the existing tests in that file.

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/expression_handlers.py backend/tests/queue/test_expression_handlers.py
git commit -m "feat(queue): add transcript_assembly job handler"
```

---

## Task 5: Ingest the produced GTF

**Files:**
- Modify: `backend/app/queue/results.py` (new applier near `_apply_salmon_quantify`; register in the result dispatch table)
- Test: `backend/tests/queue/test_results.py`

**Interfaces:**
- Consumes: the handler result dict from Task 4; `ObjectRole.ASSEMBLED_TRANSCRIPTS` from Task 3.
- Produces: `_apply_transcript_assembly(result: dict, *, owner: str) -> None`, ingesting a `DataObject` with `role=ObjectRole.ASSEMBLED_TRANSCRIPTS` and facts `assembled_by`, `transcript_count`, `novel_transcript_count`, `gene_count`.

- [ ] **Step 1: Read the existing dispatch registration**

```bash
grep -n "_apply_salmon_quantify" backend/app/queue/results.py
```

Note both sites: the function definition and its entry in the dispatch table. The new applier needs both — an applier defined but never dispatched is exactly the silent-skip shape CLAUDE.md warns about.

- [ ] **Step 2: Write the failing test**

Add to `backend/tests/queue/test_results.py`, following the file's existing applier-test style:

```python
@pytest.mark.asyncio
async def test_apply_transcript_assembly_ingests_gtf_with_assembled_role(
    tmp_path, monkeypatch
):
    """The produced GTF must not be indistinguishable from a downloaded one.

    Asserting the role explicitly because it is the whole point of the
    applier: a StringTie GTF ingested as ANNOTATION would become a candidate
    reference for featureCounts and for StringTie's own -G.
    """
    from app.models.object import ObjectRole
    from app.queue import results

    captured = {}

    async def fake_ingest(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=PydanticObjectId(), owner=kwargs["owner"])

    monkeypatch.setattr(
        "app.services.object_service.ingest_local_file", fake_ingest
    )

    gtf = tmp_path / "sample.transcripts.gtf"
    gtf.write_text("# StringTie version 2.2.1\n")

    source = await _make_ready_bam_object()  # existing helper in this file

    await results._apply_transcript_assembly(
        {
            "object_id": str(source.id),
            "job_id": str(PydanticObjectId()),
            "output": {"tmp_path": str(gtf), "name": gtf.name},
            "assembled_by": "stringtie",
            "transcript_count": 12,
            "novel_transcript_count": 3,
            "gene_count": 9,
        },
        owner=source.owner,
    )

    assert captured["role"] is ObjectRole.ASSEMBLED_TRANSCRIPTS
    assert captured["derived_from"] == [source.id]
    assert captured["facts"]["assembled_by"] == "stringtie"
    assert captured["facts"]["novel_transcript_count"] == 3
```

If `_make_ready_bam_object` does not exist in the file, build the source object with whatever helper the neighbouring applier tests use — read them first rather than inventing a fixture.

- [ ] **Step 3: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_results.py -q -k transcript_assembly
```

Expected: FAIL — `AttributeError: module 'app.queue.results' has no attribute '_apply_transcript_assembly'`.

- [ ] **Step 4: Write the applier**

Add to `backend/app/queue/results.py`, following `_apply_export_annotation_subset`'s structure at :1880:

```python
async def _apply_transcript_assembly(result: dict, *, owner: str) -> None:
    """Register a StringTie assembly as a derived object.

    Role is ASSEMBLED_TRANSCRIPTS rather than ANNOTATION, unlike the two
    other appliers in this file that produce GFF/GTF. Those both derive from
    an authoritative annotation and stay authoritative; this one proposes
    transcript models from one sample's alignment, and conflating the two
    would let a hypothesis become the reference for the next run.
    """
    from app.services import object_service, run_service

    object_id = result.get("object_id")
    output = result.get("output")
    if not output or not object_id:
        return

    source = await DataObject.get(PydanticObjectId(object_id))
    if source is None:
        log.warning("transcript_assembly_parent_missing", object_id=object_id)
        return

    job_id = result.get("job_id")

    try:
        assembled = await object_service.ingest_local_file(
            owner=source.owner,
            project_id=source.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.ASSEMBLED_TRANSCRIPTS,
            derived_from=[source.id],
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts={
                "assembled_by": result.get("assembled_by"),
                "transcript_count": result.get("transcript_count"),
                "novel_transcript_count": result.get("novel_transcript_count"),
                "gene_count": result.get("gene_count"),
            },
            # The assembly describes the same sample as its alignment.
            metadata=dict(source.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error(
            "transcript_assembly_ingest_failed",
            object_id=object_id,
            error=str(e),
        )
        return

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, [assembled.id], owner=assembled.owner)

    log.info(
        "transcript_assembly_applied",
        object_id=object_id,
        assembled_id=str(assembled.id),
    )
```

- [ ] **Step 5: Register the applier in the dispatch table**

At the dispatch site found in Step 1, add the `"transcript_assembly"` entry beside `"salmon_quantify"`, matching that table's exact call shape.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/queue/test_results.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/queue/results.py backend/tests/queue/test_results.py
git commit -m "feat(queue): ingest StringTie output as an assembled-transcripts object"
```

---

## Task 6: Launch path, node type, and API endpoint

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (`_is_annotation` at :4210; new `launch_transcript_assembly`)
- Modify: `backend/app/pipelines/node_types.py` (launcher adapter near :259; node spec near :767)
- Modify: `backend/app/api/v1/pipelines.py`
- Test: `backend/tests/pipelines/test_node_types.py`, `backend/tests/services/test_pipeline_service.py`

**Interfaces:**
- Consumes: `RunKind.TRANSCRIPT_ASSEMBLY`, `ObjectRole.ASSEMBLED_TRANSCRIPTS` (Task 3); the `transcript_assembly` job name (Task 4).
- Produces: `pipeline_service.launch_transcript_assembly(*, bam_id, owner, annotation_id=None, params=None) -> Job`; node type key `"transcript_assembly"`; `POST /pipelines/transcript-assembly`.

- [ ] **Step 1: Write the failing test for the `_is_annotation` exclusion**

Add to `backend/tests/services/test_pipeline_service.py`:

```python
def test_is_annotation_rejects_assembled_transcripts():
    """A StringTie GTF must not become a reference annotation.

    Without this exclusion it is offered as a featureCounts reference and as
    StringTie's own -G input, because _is_annotation is format-first and
    StringTie's output is GTF. This is the one place ObjectRole.
    ASSEMBLED_TRANSCRIPTS does load-bearing work rather than display work.
    """
    from app.models.object import FormatKind, ObjectRole, ObjectStatus
    from app.services import pipeline_service

    assembled = SimpleNamespace(
        status=ObjectStatus.READY,
        format=SimpleNamespace(kind=FormatKind.GTF),
        role=ObjectRole.ASSEMBLED_TRANSCRIPTS,
    )
    downloaded = SimpleNamespace(
        status=ObjectStatus.READY,
        format=SimpleNamespace(kind=FormatKind.GTF),
        role=None,
    )

    assert pipeline_service._is_annotation(assembled) is False
    # The ordinary case must keep working: a real ingested annotation carries
    # role=None, which is why this predicate is format-first at all.
    assert pipeline_service._is_annotation(downloaded) is True
```

- [ ] **Step 2: Run it to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/test_pipeline_service.py -q -k is_annotation
```

Expected: FAIL — `assert True is False`, because the exclusion is not written yet.

- [ ] **Step 3: Add the exclusion**

In `backend/app/services/pipeline_service.py`, in `_is_annotation` (~:4236), before the format check:

```python
    if obj.status is not ObjectStatus.READY:
        return False
    # Assembled transcripts are GTF but are not a reference: they are one
    # sample's proposed models, and feeding them back as -G or as a
    # featureCounts reference would treat a hypothesis as ground truth.
    if obj.role is ObjectRole.ASSEMBLED_TRANSCRIPTS:
        return False
    return obj.format.kind in (FormatKind.GFF, FormatKind.GTF)
```

Extend the docstring with a sentence naming the exclusion and why.

- [ ] **Step 4: Run it to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/test_pipeline_service.py -q -k is_annotation
```

Expected: PASS.

- [ ] **Step 5: Write `launch_transcript_assembly`**

In `backend/app/services/pipeline_service.py`, beside `launch_salmon_quantify`. Read that function first and mirror it exactly — its budget check (`refuse_if_over_budget`), its `tools.require`, its object fetch, its annotation resolution via `resolve_annotation`, its `PipelineRun` creation with `kind=RunKind.TRANSCRIPT_ASSEMBLY`, and its enqueue with job name `"transcript_assembly"` and a payload carrying `object_id`, `bam_sha256`/`bam_name`, `annotation_sha256`/`annotation_name`, and `threads`. The payload keys must match what Task 4's handler reads via `_resolve_blob(ctx.payload, "bam")` and `_resolve_blob(ctx.payload, "annotation")`.

- [ ] **Step 6: Add the node type**

In `backend/app/pipelines/node_types.py`, the launcher adapter beside `_launch_salmon_quantify` (~:268):

```python
async def _launch_transcript_assembly(*, inputs: dict, params: dict, owner: str):
    return await pipeline_service.launch_transcript_assembly(
        bam_id=inputs["alignment"],
        owner=owner,
        annotation_id=inputs.get("annotation"),
        params=params,
    )
```

And the spec, after `"salmon_quantify"` (~:796):

```python
    "transcript_assembly": NodeTypeSpec(
        label="Assemble transcripts (StringTie)",
        launch_name="pipeline_service.launch_transcript_assembly",
        launch=_launch_transcript_assembly,
        # A distinct RunKind, so unlike salmon_quantify this needs no
        # run_tool to keep the (run_kind, run_tool) pair unique.
        run_kind=RunKind.TRANSCRIPT_ASSEMBLY,
        inputs=(
            PortSpec("alignment", PortType(format=FormatKind.BAM)),
            # Optional on the port, resolved server-side from the project's
            # annotations exactly as `quantify` does -- but unlike Salmon's
            # transcriptome this is not optional to the *tool*: see
            # stringtie_runner.assemble_command on why -G is required.
            PortSpec(
                "annotation",
                PortType(format=FormatKind.GTF),
                required=False,
            ),
        ),
        outputs=(
            PortSpec(
                "transcripts",
                PortType(
                    format=FormatKind.GTF,
                    role=ObjectRole.ASSEMBLED_TRANSCRIPTS,
                ),
            ),
        ),
    ),
```

- [ ] **Step 7: Add the API endpoint**

In `backend/app/api/v1/pipelines.py`, add `POST /pipelines/transcript-assembly` mirroring the `salmon-quantify` endpoint: a request model with `bam_id`, optional `annotation_id`, optional `params`, calling `pipeline_service.launch_transcript_assembly` and returning the job. Read the neighbouring endpoint and match its response shape and error handling exactly.

- [ ] **Step 8: Run the full exhaustiveness class, not just one test**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_node_types.py -q
```

Expected: PASS — **including** both `test_every_launch_function_is_classified` and `test_no_launcher_is_both_used_and_excluded`. Per #355/#366, a new launcher can satisfy the first while colliding with the second; running the whole file is what catches it.

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/app/pipelines/node_types.py backend/app/api/v1/pipelines.py backend/tests/services/test_pipeline_service.py
git commit -m "feat(pipelines): launch transcript assembly and keep its output out of annotation pickers"
```

---

## Task 7: The suggestion card and its aligner gate

**Files:**
- Modify: `backend/app/services/suggestion_service.py` (new builder near `build_salmon_quantify_card` at :2289; register in `CARD_BUILDERS`)
- Test: `backend/tests/services/test_suggestion_service.py`

**Interfaces:**
- Consumes: `tools.stringtie()` (Task 1); `pipeline_service._is_annotation`-filtered `annotations` from the existing `_Prefetched` (already unconditional for every BAM).
- Produces: `build_transcript_assembly_card(obj, annotations) -> SuggestionCard | None` with `kind="transcript_assembly"`.

- [ ] **Step 1: Write the failing tests — both directions of the gate**

Add to `backend/tests/services/test_suggestion_service.py`:

```python
def _bam(aligned_by, **facts):
    from app.models.object import FormatKind, ObjectStatus
    return SimpleNamespace(
        id=PydanticObjectId(),
        status=ObjectStatus.READY,
        format=SimpleNamespace(kind=FormatKind.BAM),
        role=None,
        facts={"aligned_by": aligned_by, **facts} if aligned_by else dict(facts),
        metadata={},
    )


@pytest.mark.parametrize("aligner", ["hisat2", "star"])
def test_transcript_assembly_card_offered_for_splice_aware_alignments(aligner):
    from app.services import suggestion_service

    card = suggestion_service.build_transcript_assembly_card(
        _bam(aligner), annotations=[SimpleNamespace(id=PydanticObjectId())]
    )
    assert card is not None
    assert card.kind == "transcript_assembly"


@pytest.mark.parametrize("aligner", ["bwa-mem2", "minimap2", "bowtie2", "winnowmap"])
def test_no_card_at_all_for_dna_aligners(aligner):
    """Not UNAVAILABLE -- absent.

    The capability can never apply to a DNA-seq alignment, and a card
    advertising something impossible is worse than silence. UNAVAILABLE is
    reserved for the two states a user can act on: tool missing, annotation
    missing.
    """
    from app.services import suggestion_service

    assert suggestion_service.build_transcript_assembly_card(
        _bam(aligner), annotations=[SimpleNamespace(id=PydanticObjectId())]
    ) is None


def test_no_card_when_the_bam_does_not_say_which_aligner_made_it():
    """A deliberate false negative.

    An uploaded or register-in-place BAM has no aligned_by, and may well be
    DNA-seq. This mirrors _group_gci_candidates_by_aligner's refusal to merge
    "unknown" into a named aligner.
    """
    from app.services import suggestion_service

    assert suggestion_service.build_transcript_assembly_card(
        _bam(None), annotations=[SimpleNamespace(id=PydanticObjectId())]
    ) is None


def test_card_unavailable_when_stringtie_is_not_installed(monkeypatch):
    """The direction that fails when the seam breaks.

    The image ships StringTie installed, so asserting the card is *available*
    would pass whether or not the patch worked. Patching the probe off is
    what actually exercises the gate.
    """
    from app.pipelines import tools
    from app.services import suggestion_service
    from app.services.suggestion_service import CardStatus

    monkeypatch.setattr(
        tools,
        "stringtie",
        lambda: SimpleNamespace(
            name="stringtie", available=False, path="", error="not installed"
        ),
    )

    card = suggestion_service.build_transcript_assembly_card(
        _bam("hisat2"), annotations=[SimpleNamespace(id=PydanticObjectId())]
    )
    assert card is not None
    assert card.status is CardStatus.UNAVAILABLE


def test_card_unavailable_when_the_project_has_no_annotation():
    from app.services import suggestion_service
    from app.services.suggestion_service import CardStatus

    card = suggestion_service.build_transcript_assembly_card(
        _bam("hisat2"), annotations=[]
    )
    assert card is not None
    assert card.status is CardStatus.UNAVAILABLE
```

- [ ] **Step 2: Run them to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q -k transcript_assembly
```

Expected: FAIL — `AttributeError: ... has no attribute 'build_transcript_assembly_card'`.

- [ ] **Step 3: Write the card builder**

In `backend/app/services/suggestion_service.py`, after `build_salmon_quantify_card`:

```python
# Spelled from the registry rather than as string literals so an aligner
# rename cannot silently unhook this gate -- the failure would be a card that
# quietly stops appearing, which nothing else would catch.
_SPLICE_AWARE_ALIGNERS = frozenset({Aligner.HISAT2.value, Aligner.STAR.value})


def build_transcript_assembly_card(obj, annotations) -> SuggestionCard | None:
    """Assemble transcript models from a spliced alignment.

    Gated on `facts.aligned_by` rather than offered on any BAM, which is the
    opposite of `build_quantify_card` immediately above -- deliberately. That
    card offers on every BAM because "whether an alignment is RNA-seq is not
    knowable from the file". Here it *is* knowable: the alignment run records
    which aligner produced the BAM, and assembling transcripts from a
    bwa-mem2 alignment is not a worse answer, it is a meaningless one.

    A BAM with no `aligned_by` -- registered in place, or predating the field
    -- gets no card. That is a deliberate false negative on uploaded HISAT2
    BAMs, taken for the same reason `_group_gci_candidates_by_aligner` refuses
    to merge "unknown" into a named aligner: an unknown-provenance BAM may
    well be DNA-seq.

    `annotations` is a parameter rather than looked up here, for the same
    reason it is on `build_quantify_card`: the lookup is async and these
    builders are uniformly synchronous and pure. It arrives already filtered
    by `pipeline_service._is_annotation`, which is format-first -- a rule
    written against `ObjectRole.ANNOTATION` would match nothing on a real
    library.
    """
    if obj.format.kind is not FormatKind.BAM:
        return None

    aligned_by = str((obj.facts or {}).get("aligned_by") or "")
    if aligned_by not in _SPLICE_AWARE_ALIGNERS:
        return None

    title = "Assemble transcripts -- StringTie"
    description = (
        "Reconstruct the transcripts present in this sample, including "
        "isoforms the annotation does not list."
    )

    tool = tools.stringtie()
    if not tool.available:
        return SuggestionCard(
            kind="transcript_assembly",
            category="EXPRESSION",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=f"{tool.name} is not installed.",
        )

    if not annotations:
        return SuggestionCard(
            kind="transcript_assembly",
            category="EXPRESSION",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=(
                "This project has no gene annotation. Download a GFF or GTF "
                "with the assembly, or upload one."
            ),
        )

    return SuggestionCard(
        kind="transcript_assembly",
        category="EXPRESSION",
        title=title,
        description=description,
        why=(
            f"This alignment came from {aligned_by}, which preserves splice "
            "structure, so the transcripts in it can be reassembled. Counting "
            "and transcriptome quantification can only measure transcripts "
            "the annotation already lists; this is what proposes new ones."
        ),
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/transcript-assembly",
            # annotation_id omitted: the server resolves it, same as quantify.
            "body": {"bam_id": str(obj.id), "params": {}},
        },
    )
```

Add `Aligner` to the existing import from `app.pipelines.aligners` if the name is not already bound at module scope (it is imported at :33).

- [ ] **Step 4: Register the builder**

Add `build_transcript_assembly_card` to `CARD_BUILDERS` beside `build_salmon_quantify_card`, matching that table's call signature (it takes `annotations` from `_Prefetched`, already fetched unconditionally for every BAM).

- [ ] **Step 5: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/services/test_suggestion_service.py -q
```

Expected: PASS — all 9 new cases plus the existing file.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat(pipelines): offer transcript assembly on splice-aware alignments only"
```

---

## Task 8: Close the `--dta` loop

**Files:**
- Modify: `backend/app/pipelines/align_params.py:463,464,499`
- Modify: `backend/app/pipelines/aligner_registry.py:908-919`
- Test: `backend/tests/pipelines/test_align_params.py`

**Interfaces:**
- Consumes: nothing (independent of Tasks 1-7; sequenced last because it changes behaviour for alignments unrelated to StringTie, and should land once its consumer exists).
- Produces: no new symbols — a default value change only.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/pipelines/test_align_params.py`:

```python
def test_hisat2_formats_for_transcript_assembly_by_default():
    """--dta is on by default now that a consumer exists.

    Before StringTie landed this defaulted to False and the auto-suggested
    RNA-seq card never set it, so the default path produced BAMs that were
    never formatted for the tool align_params' own comment named.
    """
    from app.pipelines.align_params import Hisat2Params

    assert Hisat2Params().dta is True


def test_hisat2_dta_default_survives_a_params_round_trip():
    """The default must live in from_dict too.

    A default that applies to a fresh dialog but not to a dict round-tripped
    through the queue is a split-brain default: the flag reads as set in the
    UI and is absent from the command line.
    """
    from app.pipelines.align_params import Hisat2Params

    assert Hisat2Params.from_dict({}).dta is True
    # An explicit False must still win -- this is a default, not a constant.
    assert Hisat2Params.from_dict({"dta": False}).dta is False
```

- [ ] **Step 2: Run them to verify they fail**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_params.py -q -k dta
```

Expected: FAIL — `assert False is True` on both.

- [ ] **Step 3: Flip the default in all three places**

`backend/app/pipelines/align_params.py:463-464`:

```python
    # Formats output for downstream transcript assembly. StringTie is the
    # consumer: `transcript_assembly` runs against these BAMs, and --dta is
    # what makes their junction anchors long enough for it to assemble from.
    # On by default since the assembler landed -- before that this was an
    # opt-in checkbox nothing downstream consumed, so the RNA-seq path
    # produced BAMs formatted for a tool that did not exist.
    dta: bool = True
```

`backend/app/pipelines/align_params.py:499`:

```python
            dta=bool(data.get("dta", True)),
```

`backend/app/pipelines/aligner_registry.py:908-919`:

```python
            ParamField(
                key="dta",
                label="Format for transcript assembly",
                kind="bool",
                default=True,
                group="biology",
                help=(
                    "Tailors the output for StringTie, which assembles "
                    "transcripts from this alignment. Costs a little "
                    "alignment sensitivity for junction reads; turn it off "
                    "if you will only be counting against a known "
                    "annotation."
                ),
            ),
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_align_params.py -q
```

Expected: PASS — including any existing round-trip tests in that file, which may need their expected dicts updated for the new default. Update them if so; a test asserting `dta: False` was asserting the old default, not a behaviour worth keeping.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/align_params.py backend/app/pipelines/aligner_registry.py backend/tests/pipelines/test_align_params.py
git commit -m "feat(pipelines): format HISAT2 output for transcript assembly by default"
```

---

## Task 9: Provenance, activity, and end-to-end verification

**Files:**
- Modify: `backend/app/services/provenance_walker.py`, `provenance_report.py`, `provenance_prompt.py`, `running_now.py`
- Test: full suite

**Interfaces:**
- Consumes: everything above.
- Produces: no new symbols — registration entries only.

- [ ] **Step 1: Find every place Salmon is registered for display**

```bash
grep -n "salmon" backend/app/services/provenance_walker.py backend/app/services/provenance_report.py backend/app/services/provenance_prompt.py backend/app/services/running_now.py
```

Add the matching `transcript_assembly` / `TRANSCRIPT_ASSEMBLY` entry at each site, following the surrounding style. These are display registries: a missing entry produces a run that renders with no label rather than an error, which is precisely the silent-skip shape to avoid.

- [ ] **Step 2: Decide `_SUPPORTING_ROLES`**

`provenance_walker.py:386` has `_SUPPORTING_ROLES = frozenset({ObjectRole.REFERENCE, ObjectRole.ANNOTATION})`. Read what that set governs before changing it. `ASSEMBLED_TRANSCRIPTS` is a *result*, not a supporting input, so the default expectation is to leave it out — but confirm against the walker's own behaviour rather than assuming, and write a one-line comment recording the decision either way.

- [ ] **Step 3: Run the full suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: PASS. Fix **every** failure this reports, including any that predate this branch — per CLAUDE.md, a pre-existing failure that survives your PR has now survived a second person looking straight at it.

- [ ] **Step 4: Lint the whole tree the way the pre-commit hook does**

```bash
ruff check --config backend/pyproject.toml backend/app backend/tests ops e2e
```

Fix everything reported, not only findings in files this branch touched. CI does not lint, so nothing downstream catches what is skipped here.

- [ ] **Step 5: Verify the gate against the real database, not fixtures**

Per CLAUDE.md — the Actions tab's rules once passed a green suite while being wrong about real objects. From the **main checkout**, not this worktree:

```bash
docker compose exec api python3.12 -c "import asyncio,collections;from app.db.client import connect_to_mongo;from app.models.object import DataObject,FormatKind;asyncio.run(__import__('app.db.client',fromlist=['x']).connect_to_mongo())"
```

Simpler and sufficient: query the BAMs and print their `aligned_by` values, confirming the gate's `frozenset` matches what real objects actually carry — in particular that HISAT2 BAMs record exactly `"hisat2"` and not a variant spelling.

- [ ] **Step 6: End-to-end run**

Bring up the worktree stack, open the UI at localhost:5273, find (or produce) a HISAT2 alignment, and confirm:

1. The "Assemble transcripts -- StringTie" card appears on it.
2. The card is **absent** on a bwa-mem2 or minimap2 BAM.
3. Launching produces a GTF object with `role=assembled_transcripts` and non-null `transcript_count` / `novel_transcript_count` facts.
4. That GTF does **not** appear as a candidate annotation in the quantify dialog.

- [ ] **Step 7: Commit and tear down**

```bash
git add backend/app/services/
git commit -m "feat(pipelines): show transcript assembly runs in provenance and activity"
```

```bash
./ops/worktree-up.sh --down
```

---

## Task 10: Close out the issue

- [ ] **Step 1: Rebase onto current main**

```bash
git fetch origin main && git rebase origin/main
```

- [ ] **Step 2: Confirm the work survived the rebase**

```bash
git diff origin/main...HEAD --stat
```

Check the file list matches the File Structure table above, and skim for anything reverted.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

Label it `type:feature` and `area:pipelines` — `.github/release.yml` categorizes by label, not by the commit prefix, so an unlabelled PR lands under "Other changes". Include `Closes #622` in the body.

- [ ] **Step 4: Watch CI to completion**

```bash
gh pr checks <N>
```

Poll until every check reports pass or fail — not until the command returns. Fix anything red and re-poll.

- [ ] **Step 5: Merge once green**

```bash
gh pr merge <N> --rebase --delete-branch
```

- [ ] **Step 6: Remove the worktree**

Per CLAUDE.md, a merged PR is the "done with this" signal. Tear the worktree down rather than leaving it for the end-of-session prompt.
