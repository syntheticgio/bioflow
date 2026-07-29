# Read quality scoring

A 1-to-5 quality grade for read files, named in plain English, shown wherever a
read file appears: as a colored badge on the list icon, in the list row
subtitle, and in the detail panel header. Backed by a Help page explaining how
the number is derived.

## Problem

The QC tab holds everything needed to judge a FASTQ's quality and nothing that
states a judgement. A user looking at `DRR1066343_1.fastq` sees `q30_rate:
0.92134`, `qc_duplication_rate: 0.652221`, `mean_quality: 38.0`, and must know
Illumina conventions to conclude "this is good data."

Worse, that conclusion is only reachable by opening the file. The project
explorer lists a dozen FASTQs with size and format; picking the usable ones
means clicking each in turn.

## What already exists

Worth stating, because it decides what is free and what needs computing.

From ingest (`storage/sequence_stats.py`), for **every** FASTQ with no QC run
required:

- `mean_quality` (38.0 on the example file), `min_position_quality` (30.54)
- `quality_per_position` — one mean per cycle, 150 entries
- `base_composition` — includes the N count, 0.003% here
- `gc_content_percent` (30.93), `read_length`, `read_count_estimate`
- `quality_encoding`

From a `run_qc` job (`pipelines/fastp_runner.py:191`, `parse_qc_facts`):

- `qc_before_filtering.q30_rate` (0.92134), `.q20_rate` (0.969812)
- `qc_before_filtering.gc_content`, `.total_reads`, `.read1_mean_length`
- `qc_duplication_rate` (0.652221)
- `qc_adapters` when fastp detected any
- `qc_read_chemistry` — `short` here; `pipelines/qc_stats.py:30` already
  classifies long-read chemistry and returns a label *plus a reason string*.
  This design follows that precedent: a word and its justification, never a
  bare number.

From SRA metadata (`metadata/sra.py:124`), the assay vocabulary that makes
assay-aware scoring possible: `WGS`, `WES`, `RNA-seq`, `ATAC-seq`, `ChIP-seq`,
`Bisulfite-seq`, `Amplicon`, `Targeted panel`, landing on
`metadata.assay`.

**Critically:** `facts` is on the base `DataObject` interface
(`frontend/src/api/types.ts:69`), not just `ObjectDetail`. List rows already
carry every metric. This is a frontend-only change — no API, no schema, no
re-running QC on existing files.

### The assay-coverage reality

Measured against the live database: **12 of 41 objects have `metadata.assay`
set** (11 WGS, 1 ChIP-seq). The example file has none.

So the assay-aware path is the *uncommon* one, and the design must be honest
about that rather than assume rich metadata.

## Why not a naive composite

The example file is the argument. Q30 of 92% is genuinely excellent Illumina
data, but duplication is 65% — which for amplicon, RNA-seq, or a high-coverage
targeted panel is expected, not a defect. GC of 30.9% is "wrong" for human
(~41%) and right for a low-GC organism such as *Plasmodium*.

A weighted average across all metrics scores this file down for two things that
are probably not problems. A grade that is wrong in the common case is a grade
users learn to ignore.

## Design

### Scoring

Base quality always drives the tier. Caveats demote it only when not explained
by the assay. GC never demotes.

**Base-quality tier** — from `qc_before_filtering.q30_rate` when fastp has run,
otherwise from ingest's `mean_quality`. Illumina conventions:

| Tier | Word | Q30 | Fallback: mean_quality |
|------|------|-----|------------------------|
| 5 | Excellent | >= 0.90 | >= 36 |
| 4 | Good | >= 0.80 | >= 32 |
| 3 | Fair | >= 0.70 | >= 28 |
| 2 | Poor | >= 0.55 | >= 22 |
| 1 | Unsuitable | < 0.55 | < 22 |

**Demotions** — each drops at most one tier; applied cumulatively; floor of 1.

1. **Duplication > 50%** (`qc_duplication_rate`) — demote **only** when
   `metadata.assay` is absent, `WGS`, or `WES`. Suppressed for `RNA-seq`,
   `Amplicon`, `Targeted panel`, `ChIP-seq`, `ATAC-seq`, where high duplication
   is expected.
2. **N-rate > 1%** (from `base_composition`) — always demotes.
3. **Quality drop-off** — `min_position_quality < 20` while `mean_quality >= 30`.
   Always demotes: a clean average hiding collapsed cycles at the read end.

**GC** is reported as informational text only, never demoting, because the
organism's expected GC is unknown.

Because assay is usually unset, an unlabeled file and a WGS file score
identically. Setting `assay` under Metadata is what lifts the duplication
demotion from an RNA-seq or amplicon file, and **the tooltip says so** — the
remedy must be discoverable from where the penalty is visible.

**Worked example** (`DRR1066343_1.fastq`): Q30 0.92134 -> Excellent (5).
Duplication 0.652 > 0.50 and `assay` unset -> demote. N-rate 0.003% and
`min_position_quality` 30.54 -> no further demotion. Result: **Good (4/5)**,
caveat "65% duplication; normal for amplicon or RNA-seq."

### Sixth state

The function returns `null` — nothing rendered anywhere, no badge, no word —
when the object is not a read file (BAM, reference FASTA, index sidecar), or is
a FASTQ whose quality facts are absent (still ingesting, or ingest failed).

Absent, not "Unknown": an empty slot reads as "not applicable," whereas a word
implies a measurement was attempted.

### The module

`frontend/src/lib/readQuality.ts`, one pure function:

```
readQuality(obj: DataObject): ReadQuality | null

interface ReadQuality {
  tier: 1 | 2 | 3 | 4 | 5;
  word: "Excellent" | "Good" | "Fair" | "Poor" | "Unsuitable";
  basis: string;      // "Q30 92.1%" or "mean Q38.0"
  caveats: string[];  // demotion reasons, human-readable
  tooltip: string;    // assembled: word, score, basis, caveats, assay hint
}
```

All four surfaces and the Help page read this one function, so the thresholds
exist in exactly one place. Pure and dependency-free, so it is unit-testable
without a DOM.

### Surfaces

**1. Badge on the list icon** (`ProjectExplorer.tsx:387`) — a small dot on the
corner of the `📄` glyph, positioned against the existing `.row-icon` rule
(`styles.css:313`). Colors come from existing theme vars so light and dark both
track:

| 5 Excellent | 4 Good | 3 Fair | 2 Poor | 1 Unsuitable |
|---|---|---|---|---|
| `--success` | `--success` at 65% opacity | `--warn` | `--warn` deepened | `--error` |

Tiers 5/4 and 3/2 differ only by shade, so **color is never the only signal**:
the word sits adjacent in the row subtitle, and the badge carries the full
tooltip including the numeric score, so hovering the icon alone answers "how
good is this file?" Also what keeps the badge usable for colorblind users.

**2. Row subtitle** (`ProjectExplorer.tsx:396`) — appended after the existing
size and format spans: `2.1 GB · FASTQ · Good`.

**3. Detail panel header** (`DetailPanel.tsx:468`) — appended to
`.detail-subtitle`, after organism, above `<Tabs>`. Carries the tooltip.

**4. Tooltip** — shared text on badge, row, and header:

```
Good (4/5) — Q30 92.1%
65% duplication; normal for amplicon or RNA-seq.
Set Assay under Metadata to refine this score.
```

### Help -> BioFlow Calculations

`Header.tsx:8` has `File`, `View`, `Help` as inert placeholder buttons with no
dropdown machinery. This adds the first real one.

- A click-to-open dropdown on `Help`, closing on click-outside and Escape.
  `File` and `View` stay placeholders.
- One item, **BioFlow Calculations**, routing to `/help/calculations`.
- New route in `App.tsx:54`, full-width like `/activity`.
- `HelpCalculations.tsx`: section-per-topic. First and only section is Read
  Quality Score — the threshold table, the three demotion rules, the assay
  list, the GC rationale, and the sixth state. Built so a future topic is one
  more section.

## Testing

No jsdom or component testing exists in this repo and none is added — the UI
surfaces are verified manually in the browser at localhost:5173, per CLAUDE.md.

The scoring function itself is different: `package.json` already defines
`test: vitest run` with Vitest 2.1.8 installed and zero test files so far.
`readQuality` is pure and DOM-free, so it gets the repo's first unit test at
`frontend/src/lib/readQuality.test.ts` — run with `npm test` in `frontend/`.
Cases that matter:

- The example file: Q30 0.921 + 65% dup + no assay -> Good (4/5)
- The same facts with `assay: "RNA-seq"` -> Excellent (5/5), no demotion
- Ingest-only FASTQ (no `qc_*`) -> tier from `mean_quality`, basis says so
- BAM, reference FASTA, still-ingesting FASTQ -> `null`
- Floor: Q30 0.40 with N-rate 5% -> Unsuitable (1), not 0 or negative
- Clean mean hiding a collapsed tail: mean 38, `min_position_quality` 12 -> demoted

Manual check covers badge color in both light and dark themes, tooltip on all
three surfaces, and the Help route.

## Out of scope

- Backend persistence of the score. It is derived, cheap, and would need a
  migration plus a re-QC of every existing file for no gain.
- Scoring aligned BAMs. Mapping rate is a different question with its own tab.
- Sorting or filtering the explorer by grade.
- Organism-aware GC evaluation, which needs a reference GC table.
