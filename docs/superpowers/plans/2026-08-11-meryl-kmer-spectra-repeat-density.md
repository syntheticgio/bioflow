# Meryl K-mer Spectra and Repeat Density Implementation Plan

**Goal:** Expose meryl as a standalone pipeline tool producing k-mer frequency
spectra (genome size, heterozygosity) and per-window repeat density tracks.

**Architecture:** New runner with pure command-builders and parsers. One
handler runs both analyses sequentially. Two suggestion cards. No frontend.

Implements [#213](https://github.com/syntheticgio/bioflow/issues/213).
Spec: [`docs/superpowers/specs/2026-08-11-meryl-kmer-spectra-repeat-density-design.md`](../specs/2026-08-11-meryl-kmer-spectra-repeat-density-design.md).

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/pipelines/meryl_runner.py` | **New.** `parse_meryl_histogram`, `compute_genome_size`, `compute_repeat_density`, `build_meryl_statistics_command`, `build_meryl_print_gt_command` |
| `backend/tests/pipelines/test_meryl_runner.py` | **New.** Unit tests |
| `backend/app/queue/assembly_qc_handlers.py` | Add `analyze_meryl_tracks` handler |
| `backend/app/services/pipeline_service.py` | Add `launch_meryl_analysis` |
| `backend/app/services/suggestion_service.py` | Add two suggestion cards |
| `backend/tests/services/test_suggestion_service.py` | Add rule test cases |
| `backend/app/pipelines/tools.py` | Update meryl `TOOL_META` |

---

## Task 1: The runner

- [ ] **Step 1: Write tests**

```python
def test_parse_histogram():
    text = "1\t12345\n2\t8901\n3\t4567\n"
    result = meryl_runner.parse_meryl_histogram(text)
    assert result == [[1, 12345], [2, 8901], [3, 4567]]


def test_parse_histogram_skips_blanks():
    text = "\n1\t12345\n\n2\t8901\n\n"
    result = meryl_runner.parse_meryl_histogram(text)
    assert len(result) == 2


def test_genome_size_unimodal():
    hist = [[1, 5_000_000], [2, 2_000_000], [3, 500_000]]
    result = meryl_runner.compute_genome_size(hist, k=21)
    assert result["heterozygosity"] is None
    assert result["genome_size_est"] is not None


def test_genome_size_bimodal():
    # Heterozygous diploid: two peaks at 10x and 20x
    hist = [[1, 1_000], [5, 2_000], [10, 8_000], [15, 3_000], [20, 16_000], [25, 2_000]]
    result = meryl_runner.compute_genome_size(hist, k=21)
    assert result["heterozygosity"] is not None
    assert result["heterozygosity"] > 0.0


def test_genome_size_no_clear_peak():
    hist = [[1, 100], [2, 102], [3, 101], [4, 99]]
    result = meryl_runner.compute_genome_size(hist, k=21)
    assert "genome_size_est" not in result


def test_repeat_density_simple_case():
    lines = [
        ">chrI:0-21\tAAAAAAAAAAAAAAAAAAAAA",
        ">chrI:0-21\tTTTTTTTTTTTTTTTTTTTTT",
        ">chrI:500-521\tGGGGGGGGGGGGGGGGGGGGG",
    ]
    lengths = {"chrI": 1000}
    result = meryl_runner.compute_repeat_density(lines, lengths, window_count=2)
    c = result["contigs"]
    assert len(c) == 1
    assert c[0]["name"] == "chrI"
    assert c[0]["window_bases"] == 500
    assert c[0]["density"][0] > c[0]["density"][1]  # 2 hits in window 0, 1 in window 1


def test_repeat_density_all_n_gaps_are_null():
    lines = [">chrI:0-21\tAAAAAAAAAAAAAAAAAAAAA"]
    lengths = {"chrI": 1000, "chrN": 500}
    result = meryl_runner.compute_repeat_density(lines, lengths, window_count=2)
    n_contig = [c for c in result["contigs"] if c["name"] == "chrN"][0]
    assert all(v is None for v in n_contig["density"])


def test_repeat_density_keeps_longest_and_flags_partial():
    lengths = {f"c{i}": 2000 for i in range(60)}
    lengths["longest"] = 10000
    lines = [f">longest:{i*100}-{i*100+21}\tAAAAAAAAAAAAAAAAAAAAA" for i in range(20)]
    result = meryl_runner.compute_repeat_density(lines, lengths, window_count=2)
    assert len(result["contigs"]) <= 50
    assert any(c["name"] == "longest" for c in result["contigs"])
    assert result["repeat_density_partial"] is True
```

- [ ] **Step 2: Implement**

`parse_meryl_histogram(text: str) -> list[list[int]]` — parse tab-separated
`frequency count`, one per line. Skip blank lines.

`compute_genome_size(histogram, *, k=21) -> dict` — find the peak(s) in the
histogram. Total kmers = sum(freq * count). Distinct = sum(count).
Genome size = total_kmers / peak_freq. Detect bimodal: if there are two
clear peaks at roughly 1:2 frequency ratio, report heterozygosity.

`compute_repeat_density(lines, contig_lengths, *, window_count=500) -> dict` —
parse `>contig:pos-len` from `meryl print greater-than` output. Bin into
windows. Contigs shorter than 100 bp × window_count get proportionally fewer
windows. Keep longest `MAX_STORED_CONTIGS`. Set `repeat_density_partial` when
truncated. Windows with no k-mer hits get `density=0.0, count=0`; contigs
that appear in `contig_lengths` but have zero k-mer lines get `null` windows.

`build_meryl_statistics_command(meryl_path, db) -> list[str]` — `meryl
statistics <db>`.

`build_meryl_print_gt_command(meryl_path, db, threshold=3) -> list[str]` —
`meryl print greater-than <N> <db>`.

Reuse `build_meryl_count_command` from `merqury_runner.py`.

```
MIN_WINDOW_BASES = 100
REPEAT_DENSITY_THRESHOLD = 3
```

- [ ] **Step 3: Run tests**

```bash
./backend/run-worktree-tests.sh tests/pipelines/test_meryl_runner.py -q
```

---

## Task 2: The handler

- [ ] **Step 1: Add `analyze_meryl_tracks`**

In `assembly_qc_handlers.py`:

```python
@handler(
    "analyze_meryl_tracks",
    mode=HandlerMode.SUBPROCESS,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=4, mem_mb=8192, io=IoClass.HEAVY),
    max_attempts=1,
)
def analyze_meryl_tracks(ctx: JobContext) -> dict:
```

Reads pipeline:
1. Resolve reads: reuse `_materialize_meryl_cache` (imported from
   `pipeline_service`), or build fresh with `build_meryl_count_command`
2. Run `meryl statistics` on reads DB → parse histogram → compute genome size
3. If zero k-mers, skip `kmer_spectra`

Assembly pipeline:
4. Run `meryl count k=21` on assembly → fresh DB
5. Run `meryl print greater-than 3` → parse k-mer hits
6. Read `sequence_lengths` fact from assembly → `compute_repeat_density`
7. Merge both facts

Return `{"facts": {"kmer_spectra": ..., "repeat_density": ...}}`. Omit keys
for analyses that produced no usable data.

- [ ] **Step 2: Restart worker**

```bash
docker compose restart worker
```

---

## Task 3: The launcher

- [ ] **Step 1: Add `launch_meryl_analysis`**

In `pipeline_service.py`, modelled on `launch_assembly_qv`:

```python
async def launch_meryl_analysis(
    object_id: ObjectId, read_object_id: ObjectId | None = None, *, owner: Any
) -> ObjectId:
```

Same read-set resolution logic as `launch_assembly_qv`: `group_read_sets`,
prefer trimmed, error on multiple distinct sets. Uses `_materialize_meryl_cache`
to reuse cached read databases.

`dedup_key = f"analyze_meryl_tracks:{assembly.id}"`.

- [ ] **Step 2: Wire API route**

Same pattern as `launch_assembly_qv`'s route. Single endpoint for both
analyses — the handler decides what to run based on available inputs.

---

## Task 4: Suggestion rules

- [ ] **Step 1: Add k-mer spectrum card**

Available when: project has draft assembly + reads + meryl installed.
Copy `build_assemble_qv_card`'s tool gating.

`kind="kmer_spectra"`, `category="ASSEMBLY_QC"`.

- [ ] **Step 2: Add repeat density card**

Available when: meryl installed + assembly has `sequence_lengths` facts
with ≤ 50 contigs. Gate on contig count — a draft with 200,000 contigs
has no meaningful density track and won't render.

`kind="repeat_density"`, `category="ASSEMBLY_QC"`.

- [ ] **Step 3: Test cases**

Test the *unavailable* direction for both — assert the card flips when
meryl is patched off. The image ships meryl as installed, so an "available"
assertion passes whether or not the patch worked.

---

## Task 5: TOOL_META update

- [ ] Update `tools.py:1722` meryl entry:
  - `pipelines` — add repeat density and genome characterization
  - `usage` — describe repeat density + spectra alongside existing Merqury text

---

## Task 6: Close out

- [ ] Full suite: `./backend/run-worktree-tests.sh tests/ -q`
- [ ] Commit as separable: runner + tests, handler, launcher+suggestions
- [ ] Push and open PR with `Closes #213`
- [ ] Label `type:feature`, `area:backend`, `area:pipelines`
