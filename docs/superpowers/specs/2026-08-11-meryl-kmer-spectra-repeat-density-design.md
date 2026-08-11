# Meryl-based k-mer repeat density and genome characterization

Design for [#213](https://github.com/syntheticgio/bioflow/issues/213) — the
tool integration that unblocks [#177](https://github.com/syntheticgio/bioflow/issues/177)
(repeat-density ring for the Circos plot).

**meryl is already installed and probed** (Marbl meryl 1.4.2 at `/opt/meryl/bin/meryl`,
`tools.py:709`). It currently builds k-mer databases that Merqury compares for
QV scoring. This design exposes it as a first-class pipeline tool that
produces two new facts: a k-mer frequency spectrum from reads (genome size,
heterozygosity) and a repeat-density track from the assembly (#177's input).

## One handler, two analyses

A single `analyze_meryl_tracks` handler takes both an assembly and a read
set, runs two meryl pipelines sequentially, and merges two facts:

```
launch_meryl_analysis(assembly, reads)
  → analyze_meryl_tracks
    → meryl count k=21 over reads       (reuses cached MERYL_DB)
    → meryl statistics → kmer_spectra
    → meryl count k=21 over assembly
    → meryl print greater-than 3 → repeat_density per window
    → merge {kmer_spectra, repeat_density} onto assembly
```

**Why one handler, not two.** A spectrum needs reads; repeat density needs the
assembly. They run different meryl commands on different inputs. But the user
asking "analyze this genome" wants both at once, and a single job with two
facts is simpler than two jobs that race to build independent meryl databases
on the same file. They're dispatched together and succeed or fail together.

**`HandlerMode.SUBPROCESS`** — meryl is an external tool. `JobResources(cpu=4,
mem_mb=8192, io=IoClass.HEAVY)`. `max_attempts=1`. Read-set resolution follows
`launch_assembly_qv`: `group_read_sets`, prefer trimmed.

**No run record**, following `launch_completeness` and every other read-only
assembly QC job. These are facts on an existing object, not a new artifact.

## Fact schemas

### `kmer_spectra`

Stored on the assembly object. The histogram is raw meryl `statistics` output:
a positional array of `[frequency, count]` pairs with no keys per row.

```
{
  "k": 21,
  "read_set_name": "SRR...",
  "total_kmers": 234_567_890,
  "distinct_kmers": 45_678_901,
  "histogram": [[1, 12_345_678], [2, 8_901_234], ...],
  "genome_size_est": 4_567_890_123,
  "heterozygosity": null | 0.012
}
```

`genome_size_est` is derived from the histogram peak — absent when no clear
peak is found. `heterozygosity` is `None` for unimodal spectra, a fraction
for bimodal (heterozygous diploid).

### `repeat_density`

Stored on the assembly object, matching #151's windowing scheme exactly:

```
{
  "k": 21,
  "threshold": 3,
  "window_count": 500,
  "contigs": [
    {"name": "chrI", "length": 230218, "window_bases": 460,
     "density": [0.12, 0.08, null, ...],
     "count": [145, 89, null, ...]}
  ]
}
```

- **500 windows per contig**, floored at `MIN_WINDOW_BASES = 100`.
- **Longest `MAX_STORED_CONTIGS` (50) contigs kept**, `repeat_density_partial` when truncated.
- **`null` for all-N windows** — zero repeats and "unassessed" are different claims.
- `density` = fraction of k-mers in that window above `greater-than N`.
- `count` = raw count above threshold.

## Runner: `backend/app/pipelines/meryl_runner.py`

Pure functions following `merqury_runner.py`'s precedent:

| Function | Purpose |
|---|---|
| `build_meryl_count_command` | Already exists in `merqury_runner.py`. Reused directly. |
| `build_meryl_statistics_command` | `meryl statistics <db>` → histogram to stdout |
| `build_meryl_print_gt_command` | `meryl print greater-than <N> <db>` → k-mer positions |
| `parse_meryl_histogram(text)` | Tab-separated `frequency count` → `list[list[int]]` |
| `compute_genome_size(histogram)` | Peak-finding → `{total, distinct, genome_size_est, heterozygosity}` |
| `compute_repeat_density(lines, contig_lengths, window_count=500)` | Bin k-mer hits into #151 windowing scheme |

### Genome size estimation

The histogram peak is the expected coverage depth. Under the simple model:
`genome_size ≈ total_kmers / peak_coverage`.

For a homozygous diploid (peak at 2×), the estimate is halved. Heterozygosity
is detected from a bimodal distribution: the first peak (heterozygous k-mers,
half the coverage) and the second peak (homozygous, full coverage). The
distance between them yields the heterozygosity rate. When the spectrum is
unimodal, `heterozygosity` is `None`.

This is simple peak-finding, not a full GenomeScope model. It yields a
reasonable estimate for the common cases this pipeline sees (homozygous
assemblies and heterozygous diploids) without adding a dependency.

### Repeat density binning

`meryl print greater-than 3` emits lines like:

```
>contig_1:1000-1021 AAAAAAAAAAAAAAAAAAAAA
```

Only the contig name and start position are parsed (the k-mer sequence is
discarded). Each hit is binned into its window by `pos // window_width`.
The window width is `max(MIN_WINDOW_BASES, contig_length // 500)`.

Contig lengths come from the fact that `sequence_lengths` fact (already
stored on every assembly by `parsers.py`). The handler reads it; the runner
takes it as input rather than opening the FASTA.

Output is in #151's parallel-array format: `density` and `count` are
positional arrays of numbers, not lists of window objects.

## Handler: `backend/app/queue/assembly_qc_handlers.py`

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

Step-by-step:

1. Resolve reads: reuse `_materialize_meryl_cache` if cached, otherwise build fresh
2. `meryl statistics` on reads DB → parse histogram → compute genome size
3. `meryl count k=21` on assembly → `meryl print greater-than 3` → bin hits
4. Merge `{"facts": {"kmer_spectra": ..., "repeat_density": ...}}`

If reads DB has zero k-mers (empty reads), skip spectra and leave
`kmer_spectra` absent. If assembly produces no high-freq k-mers (too small or
all-N), skip `repeat_density`.

Extend the lease — this is not a short job on a large genome.

## Launcher: `backend/app/services/pipeline_service.py`

`launch_meryl_analysis(assembly, read_object_id=None, *, owner)` — modelled on
`launch_assembly_qv` including its read-set resolution. `dedup_key =
f"analyze_meryl_tracks:{assembly.id}"`.

## Suggestion rules

Two cards in `suggestion_service.py`:

1. **K-mer spectrum** — available when project has a draft assembly + at least
   one read set + meryl installed. Same gating logic as `assembly_qv`.

2. **Repeat density** — available when meryl is installed and the assembly has
   `sequence_lengths` with ≤ 50 contigs (a draft with 200,000 contigs has no
   meaningful density track). Gate on contig count rather than letting it run
   and store data that won't render.

Both must test the *unavailable* direction (assert the card flips when the
probe is patched off — the image ships meryl, so an "available" assertion
passes whether or not the patch worked).

## TOOL_META

The existing `meryl` entry in `tools.py:1722` needs:

- `pipelines` expanded to include the new capabilities
- `usage` updated to describe repeat density and genome characterization
  alongside the existing Merqury text

## Error handling

- Zero k-mers from reads → skip spectra, don't write a meaningless histogram
- No clear histogram peak → `genome_size_est` omitted, not guessed
- All-N assembly → `repeat_density` with `null` across all windows
- Cancel: `HandlerMode.SUBPROCESS` — the handler kills the subprocess
  and the lease extension loop catches it. No manual cancel check needed.

## Testing

Backend-only; no frontend change in this design.

- `parse_meryl_histogram` — known meryl output
- `compute_genome_size` — unimodal (haploid) and bimodal (heterozygous diploid)
- `compute_repeat_density` — hits binned to windows, cap, partial flag, `null` gaps
- Handler — mock `asyncio.create_subprocess_exec`, assert both facts land
- Suggestion rules — unavailable direction

## The evaluation step (pre-build)

Before building `compute_repeat_density` against meryl, test the proxy:

1. `meryl count k=21` on E. coli K-12 and B. subtilis 168 assemblies (both
   have published repeat annotations with known answers)
2. `meryl print greater-than 3` → bin hits per 500-window contig
3. Compare visually against published repeat annotation
4. Measure runtime and peak memory

If the proxy tracks known repeats, we ship. If not, `compute_repeat_density`
pivots to accepting input from RED or RepeatMasker — the runner architecture
doesn't change, only the source of k-mer hits.

## Sequencing

Independent of #214 (Bakta annotation). Uses no new dependencies. The existing
`DEFAULT_MERYL_K = 21` and `_materialize_meryl_cache` are reused.
