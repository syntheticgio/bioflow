# Meryl-based k-mer repeat density and frequency spectra

Design for [#213](https://github.com/syntheticgio/bioflow/issues/213) (meryl
as a standalone pipeline tool). Split from the broader issue scope by
[brainstorming on 2026-08-10](#213): this spec covers the **assembly-only**
capabilities — repeat density and k-mer frequency spectra. The read-aware
capabilities (contamination detection, per-window QV, winnowmap k-mer file
exposure) are deferred to a follow-up spec that rides on the cached assembly
DB this one introduces.

## Motivation

meryl is already installed (Marbl meryl 1.4.2 at `/opt/meryl/bin/meryl`,
probed at `tools.py:709`) and used by Merqury for QV scoring. Its own k-mer
counting and manipulation capabilities are not exposed as a standalone
pipeline step. Wiring it as a first-class tool unblocks
[#177](https://github.com/syntheticgio/bioflow/issues/177) (repeat-density
ring on the Circos plot) and adds k-mer frequency spectra for genome
characterization.

This is Step 1 of the [#213 scope](../issue/213):
> Evaluate the k-mer density proxy for repeats

The evaluation experiment is the gating step — if meryl's k-mer density proxy
does not visually correspond to known repeats in a published annotation, the
implementation pivots. This spec assumes the proxy passes and designs the full
pipeline. If it fails, this spec is invalidated and we fall back to RED or
RepeatMasker under a new design.

## Architecture overview

```
Actions tab card ("Characterize k-mer repeats and spectrum")
    │
    ▼
suggestion_service.py  ──  checks meryl.available, assembly status
    │
    ▼  [user clicks Launch]
pipeline_service.py  ──  enqueues "characterize_kmers" job
    │
    ▼
assembly_qc_handlers.py  ──  new handler: _characterize_kmers()
    │
    ├─  meryl count k={k} assembly.fasta → assembly.meryl/
    ├─  store SidecarRole.MERYL_ASSEMBLY_DB sidecar
    ├─  call meryl_runner.compute_repeat_density(path, kmer_set, windowing)
    │       → repeat_density fact (shaped like gc_tracks)
    └─  call meryl_runner.compute_kmer_spectrum(path)
            → kmer_spectrum fact (frequency histogram)
```

### What stays untouched

- `merqury_runner.py` — its `build_meryl_count_command` is read-set-specific
- `winnowmap_runner.py` — its assembly-side meryl count serves a different purpose (winnowmap's repetitive-k-mer file)
- `SidecarRole.MERYL_DB` — read-side caching stays as-is
- The windowing scheme from `gc_tracks.py` — `WINDOW_COUNT=500`,
  `MIN_WINDOW_BASES=100`, `MAX_STORED_CONTIGS=50` — reused, not duplicated

### New files

- `backend/app/pipelines/meryl_runner.py` — command builders, parsers, and
  compute functions. Pure functions, testable without a binary, following
  `quast_runner.py` and `gc_tracks.py`.

### Modified files

- `backend/app/pipelines/tools.py` — expand the existing `"meryl"` TOOL_META
  entry's `pipelines` tuple and `usage` string
- `backend/app/models/object.py` — new `SidecarRole.MERYL_ASSEMBLY_DB`
- `backend/app/queue/assembly_qc_handlers.py` — new `_characterize_kmers`
  handler
- `backend/app/services/suggestion_service.py` — new
  `build_kmer_characterization_card` rule

## Runner: `meryl_runner.py`

### `build_meryl_count_command()`

```python
def build_meryl_count_command(
    *,
    meryl_path: str,
    k: int,
    assembly: Path,
    output: Path,
    threads: int = 4,
) -> list[str]:
```

Canonical `meryl count` over an assembly FASTA. `k` is user-chosen, defaulting
to 21.

Two existing modules build nearly identical commands for different purposes:
`merqury_runner.build_meryl_count_command` (reads, k=21) and
`winnowmap_runner.build_meryl_count_command` (assembly, k=15, hardcoded). This
is the one that should be canonical for assembly-side counting; the winnowmap
version can delegate to it later (not in this PR — the duplication is
pre-existing and not this spec's scope).

### `build_meryl_histogram_command()`

```python
def build_meryl_histogram_command(
    *,
    meryl_path: str,
    database: Path,
) -> list[str]:
```

`meryl histogram` over the database. Single invocation, no flags beyond the
database path. Output is TSV: `frequency<TAB>count`.

### `build_meryl_print_repetitive_command()`

```python
def build_meryl_print_repetitive_command(
    *,
    meryl_path: str,
    database: Path,
    min_count: int,
) -> list[str]:
```

`meryl print greater-than {min_count}` to extract the repetitive k-mer set.
The `min_count` is the frequency threshold — k-mers appearing this many times
or more are considered repetitive. Default TBD from the evaluation experiment.

### `compute_repeat_density()`

```python
def compute_repeat_density(
    path: Path,
    compression: Compression,
    repetitive_kmers: set[str],
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
```

Reuses the `gc_tracks.py` scanning loop verbatim: same FASTA parser, same
windowing math (500 windows per contig, floored at 100 bp minimum, longest 50
contigs), same `_check_cancel()`, same sort-by-length-and-clip.

**Per-window counting:** for each window of the assembly sequence, count how
many k-mers (sliding window of size `k`) fall in the `repetitive_kmers` set,
divided by the total number of k-mers in that window. A k-mer is looked up by
its canonical representation. meryl canonicalizes by taking the
lexicographically smaller of a k-mer and its reverse complement; the
`compute_repeat_density` function must do the same when scanning the assembly
sequence, or the set lookups will miss every k-mer on the opposite strand.

**Output shape** (identical structure to `gc_tracks`, different key name):

```python
{
    "window_count": 500,
    "k": 21,
    "contigs": [
        {
            "name": "contig_1",
            "length": 4500000,
            "window_bases": 9000,
            "repeat_density": [12.5, None, 0.0, 8.3, ...],  # 500 values
        },
        ...
    ],
    # "repeat_density_partial": True  if >50 contigs were dropped
}
```

`None` means no canonical k-mers fell in that window (unassessed, not zero
repeats). Values are rounded to one decimal place, matching `gc_tracks`'s two
decimal places for GC percentage — here precision to 0.1% is sufficient given
the intrinsic noise in k-mer counting.

### `compute_kmer_spectrum()`

```python
def compute_kmer_spectrum(
    histogram_text: str,
    stats_text: str,
    k: int,
) -> dict:
```

Parses `meryl histogram` output (TSV: `frequency\tcount`) and `meryl
statistics` output (two lines: `distinct_kmers` and `total_kmers` values).
Returns:

```python
{
    "k": 21,
    "distinct_kmers": 14200000,
    "total_kmers": 235000000,
    "histogram": [
        {"frequency": 1, "count": 8200000},
        {"frequency": 2, "count": 3100000},
        ...
    ],
}
```

The histogram is the full frequency distribution — usable for genome size
estimation (GenomeScope-like analysis), heterozygosity assessment, and ploidy
inference. Stored as a flat list, not per-contig.

The histogram is the raw output — no bucketing, no downsampling, no
truncation. A typical bacterial genome produces a few hundred rows; a
eukaryotic one, a few thousand. Both fit comfortably in a MongoDB document.

## Handler: `_characterize_kmers`

In `backend/app/queue/assembly_qc_handlers.py`, following the `_assess_qv`
pattern: a `@handler`-decorated function in `HandlerMode.SUBPROCESS` for the
shell-command phases (`meryl count`, `meryl print`, `meryl histogram`). The
pure-Python compute phase (`compute_repeat_density`) runs in the same handler
thread after the subprocess calls return — no separate mode needed.

### Steps

1. **Resolve meryl** — `tools.require(tools.meryl())`, same guard as QV
2. **Extract params** — `k = int(payload.get("k") or 21)`, `min_count` (frequency
   threshold, default TBD from evaluation)
3. **Link assembly** under a fixed name — filenames never reach argv directly
   (the QUAST XSS lesson: `_assess_misassemblies` and `_assess_qv` already do this,
   and this handler follows them)
4. **Run `meryl count`** — build via `meryl_runner.build_meryl_count_command()`,
   run via `run_subprocess()`, check exit code
5. **Run `meryl statistics`** — cheap, ~2 lines of output, captures
   `distinct_kmers` and `total_kmers`
6. **Run `meryl histogram`** — parse with `meryl_runner.compute_kmer_spectrum()`,
   store as `kmer_spectrum` fact
7. **Run `meryl print greater-than {min_count}`** — parse into a Python
   `set[str]` of repetitive k-mers
8. **Compute per-window density** — call `meryl_runner.compute_repeat_density()`
   with the repetitive k-mer set and the cancel event, store as
   `repeat_density` fact
9. **Store sidecar** — tag the meryl DB directory as
   `SidecarRole.MERYL_ASSEMBLY_DB` on the assembly, keyed by `k`

No AI calls, no `asyncio.run()` — the `run_from_thread` trap doesn't apply.

### Thread safety

Steps 7-8 are pure compute in the handler's subprocess thread — hash-set
lookup against a sliding k-mer window over a contig buffer. No event loop, no
Motor, no AI. The `run_from_thread` issue is a non-problem here.

## Sidecar: `SidecarRole.MERYL_ASSEMBLY_DB`

New entry in `backend/app/models/object.py`, sibling to the existing
`MERYL_DB`:

```python
MERYL_ASSEMBLY_DB = "meryl-assembly-db"
```

- Stored as a single archive member (a directory, not a file — same pattern
  as `MERYL_DB` for reads and `STAR_INDEX` for STAR)
- Tagged with `k` in its facts so the read-aware handler (Spec 2) can check
  for a matching cached DB rather than rebuilding
- `k` is part of the DB's identity — meryl reads `k` back out of the database
  rather than accepting it as an argument, so a DB built at one `k` cannot
  serve a run that wants another

The assembly DB is cheap to rebuild (assembly << reads), so caching it is a
convenience rather than a cost-avoidance measure. Its primary value is
enabling Spec 2's read-aware capabilities to reuse it without rebuilding.

## Facts

Both fact types are stored on the assembly object.

### `repeat_density`

Shaped identically to `gc_tracks`:

- `window_count`: 500
- `k`: the k-mer size used (preserved so the frontend can display it — a
  k=15 density track means something different from a k=31 one)
- `contigs`: array of `{name, length, window_bases, repeat_density: [float|None]}`
- `repeat_density_partial`: true when >50 contigs were dropped

`None` in the density array means unassessed (no canonical k-mers fell in that
window), never zero repeats. The distinction is sharper here than for GC: zero
repeats is a claim about the genome; unassessed is a claim about the method.

### `kmer_spectrum`

- `k`: the k-mer size used
- `distinct_kmers`: number of distinct k-mers in the assembly
- `total_kmers`: total k-mer count
- `histogram`: array of `{frequency, count}` pairs

The histogram is the raw frequency distribution — no bucketing, no
downsampling, no truncation. A typical bacterial genome produces a few hundred
rows; a eukaryotic one, a few thousand. Both fit comfortably in a MongoDB
document.

## Suggestion card: `build_kmer_characterization_card`

New rule in `backend/app/services/suggestion_service.py`, anchored on the
assembly, category `ASSEMBLY_QC` (same category as QV, completeness,
misassembly, assembly_errors).

- **Available when:** meryl is installed (`tools.meryl().available`),
  assembly is `READY`
- **Unavailable when:** meryl not installed, assembly not ready
- **Title:** "Characterize k-mer repeats and spectrum"
- **Description:** "Compute per-window repeat density and a k-mer frequency
  histogram to characterize the assembly's repeat content and genome
  structure."
- **Params payload:** `{"k": 21, "min_count": N}` where `k` defaults to 21
  and the user can override it in the card's inline field; `min_count`
  defaults to a value determined by the evaluation experiment

Single card for both outputs — the user doesn't need to decide between
density and spectrum; both come from the same meryl database and the marginal
cost of computing the second after the first is a <1s parse pass.

### Suggestion service test

Patch the meryl probe to `available=False` and assert the card flips to
`UNAVAILABLE` — the direction that fails when the seam breaks. Follows the
pattern in `test_build_assembly_qv_card` and the CLAUDE.md trap about testing
availability.

## TOOL_META update

The existing `"meryl"` entry at `tools.py:1722` gets two changes:

- `pipelines`: expand from `(PipelineType.ASSEMBLY_QC,)` to include the
  repeat-characterization capability. The appropriate enum member may need to
  be created if none matches.
- `usage`: append a sentence describing this new capability — "Also builds
  per-window repeat-density tracks and k-mer frequency spectra for genome
  characterization — invoked by the 'Characterize k-mer repeats' action card."

No new TOOL_META entry — the issue's "distinct from its existing entry as
Merqury's dependency" guidance is satisfied by expanding the current entry
rather than creating a second one for the same binary. A second entry for the
same tool would confuse `/help/software` (which renders one row per key) and
`test_every_tool_is_documented` (which enumerates `TOOL_META.keys()` against
probed tools).

## Evaluation experiment (gating step)

Before any implementation, run the evaluation:

1. **Test genomes:**
   - Bacterial: *E. coli* K-12 (NCBI: U00096.3) — well-annotated, repeat regions
     known (IS elements, Rhs elements, prophages)
   - Eukaryotic: *S. cerevisiae* S288C (NCBI: GCF_000146045.2) — annotated
     transposons and LTR retrotransposons
2. **`meryl count k=21`** on each assembly FASTA
3. **`meryl histogram`** to identify the frequency distribution — choose
   `min_count` at the elbow where repetitive k-mers dominate the tail
4. **`meryl print greater-than {min_count}`** to extract the repetitive k-mer set
5. **Compute per-window density** of those k-mers
6. **Compare** against published repeat annotations — do density peaks
   correspond to known repeat regions?

**What falsifies it:** a density track that does not visually correspond to
known repeats. If the proxy passes, this spec unblocks. If it fails, we
fall back to RED or RepeatMasker under a new design.

**Measurements to record:** runtime, peak memory, and the chosen `min_count`
threshold for each genome.

## Verification

1. **Unit tests:**
   - `meryl_runner`: test command builders produce correct argv, test
     `compute_repeat_density` against a hand-built FASTA with known repeat
     regions, test `compute_kmer_spectrum` against hand-crafted histogram
     output
   - `suggestion_service`: test the card appears when meryl is available,
     test it flips to `UNAVAILABLE` when the meryl probe is patched off
   - `test_every_tool_is_documented`: the expanded TOOL_META entry still passes

2. **Integration/manual:**
   - Launch the card against a real assembly, verify facts are stored with
     the correct shape
   - Verify the Circos plot (after #177 lands) renders the ring

3. **Worker restart:** `docker compose restart worker` after touching
   `assembly_qc_handlers.py` — worker does not hot-reload

## Relation to existing tooling

- **meryl is already installed and probed** (`tools.py:709`). This spec
  does not add a new binary — it exposes capabilities the existing one already
  has.
- **`SidecarRole.MERYL_DB`** (read-side, `object.py:169`) stays as-is. The
  new `MERYL_ASSEMBLY_DB` is a separate role for the assembly side.
- **Merqury** (`merqury_runner.py`) is untouched — its `meryl count` on reads
  and its QV scoring are unrelated to this feature.
- **winnowmap** (`winnowmap_runner.py`) has its own `build_meryl_count_command`
  for assembly-side k-mer counting at k=15. This spec introduces a canonical
  assembly-side builder at a configurable k; the winnowmap version can
  delegate to it in a follow-up.

## Success criteria

1. The Actions tab shows a "Characterize k-mer repeats and spectrum" card on
   every ready assembly when meryl is installed
2. Launching the card runs `meryl count` on the assembly, caches the DB as an
   assembly sidecar, stores both `repeat_density` and `kmer_spectrum` facts
3. The `repeat_density` fact is shaped identically to `gc_tracks` — same
   windowing scheme, same contig cap, same partial flag, same `None`-for-
   unassessed convention
4. `docker compose exec api python -m pytest tests/ -q` passes
5. #177 can be implemented as a frontend-only task reading `repeat_density`
