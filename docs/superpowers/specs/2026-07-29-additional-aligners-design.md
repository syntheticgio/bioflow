# Additional aligners: bowtie2, HISAT2 (and STAR next)

Adds bowtie2 and HISAT2 as selectable aligners, and builds the shared
infrastructure that makes a fifth aligner -- STAR -- a follow-on rather than
another rewrite. Along the way it replaces the tool selector's vertical card
list with a list-and-detail layout, and adds a resource estimator that warns
before a run thrashes and blocks one that cannot physically fit.

## Why now

`Aligner` is a two-value enum, and per-aligner behavior is spread across five
files: index shape in `aligners.py`, command construction and defaults in
`align_runner.py`, tool resolution in `align_handlers.py`, probe and
description in `tools.py`, and the parameter form in `AlignDialog.tsx`. Adding
one aligner today means five coordinated edits with no single place that says
what an aligner *is*. Three more tools would make that worse in a way that is
hard to walk back.

The tool selector has the same shape of problem for a different reason: it
renders a vertical list of cards each carrying a ~40-word summary and 3-5
strength bullets. Five of those is a long scroll, and comparing bowtie2
against HISAT2 means scrolling past STAR.

And neither the dialog nor the launch endpoint knows anything about what the
machine can actually do. `sort_memory_mb` is *per thread*, so 16 threads at
2048 MB is 32 GB of sort buffer alone, and nothing says so until the OOM
killer does.

## Scope

**This slice:** the aligner registry, `IndexLayout` with suffix and prefix
implementations, the per-aligner parameter model, the registry-driven
parameter form, the resource estimator, the selector redesign, and bowtie2 +
HISAT2 end to end.

**Deliberately not this slice:** STAR. Its index is a directory rather than a
set of suffix-named siblings, it requires a GTF annotation at index time, and
it needs roughly ten times the genome size in RAM. Every abstraction below is
specified with that case in mind and `DirectoryLayout` is described here, but
it is not implemented until bowtie2 and HISAT2 have proven the registry on two
tools that share a mechanism.

Bowtie2 and HISAT2 are the right first pair precisely because they are close:
both use basename-prefixed index sets, both take `-p` for threads, both have a
separate `*-build` binary. Building them together proves the registry is a
real abstraction and not a wrapper around one tool's habits.

## Design

### 1. The aligner registry

A new `backend/app/pipelines/aligner_registry.py` holds one spec per aligner,
and becomes the single place a new aligner is declared:

```python
@dataclass(frozen=True)
class AlignerSpec:
    aligner: Aligner
    tool: Callable[[], Tool]        # the probe from tools.py
    index: IndexLayout              # how the index is shaped on disk
    params_class: type[BaseAlignParams]
    fields: tuple[ParamField, ...]  # UI metadata, serialized to the dialog
    memory_model: MemoryModel       # coefficients for the estimator
```

`_aligner_tool` in `align_handlers.py` and `default_preset` in
`align_runner.py` both become registry lookups. `TOOL_META` stays where it is
-- it describes tools for the selector across every pipeline, not just
alignment -- but the registry references it rather than duplicating it.

### 2. Index layouts

`aligners.py` keeps on-disk layout knowledge and gains an `IndexLayout`
abstraction with three implementations, two of them built here:

- **`SuffixLayout`** -- today's behavior, unchanged. bwa-mem2's five-file set
  and minimap2's single `.mmi`, named by appending to the reference filename.
- **`PrefixLayout`** -- bowtie2 and HISAT2. Index files are numbered siblings
  of the reference name: bowtie2 writes `<ref_name>.1.bt2` through `.4.bt2`
  plus `.rev.1.bt2` and `.rev.2.bt2`; HISAT2 writes `<ref_name>.1.ht2`
  through `.8.ht2`. These *are* suffix
  siblings, so `materialize` needs no change; what differs is the command
  form. Both tools take a basename via `-x <ref_name>` rather than a path to
  the reference, and both build through a separate binary (`bowtie2-build`,
  `hisat2-build`) rather than a subcommand. `PrefixLayout` is therefore
  parameterized by its own suffix tuple (the two tools' sets differ in both
  count and naming) plus a builder-binary field and a `basename` accessor
  for command construction.

  The exact suffix sets must be confirmed against the installed versions
  during implementation rather than taken from this spec: a missing member
  makes the tool refuse to load the index, and `build_index` already fails
  loudly on a file the layout expected but the builder did not produce. That
  check is the verification step -- build an index for a small reference and
  confirm the produced filenames match the tuple.
- **`DirectoryLayout`** -- STAR, specified but not implemented. See
  "STAR: what is already decided" below.

`plan_links`' rule that a sidecar must start with `reference_name` is a real
safety check -- it catches an index attached to the wrong reference, which is
the failure mode that produces a plausible-looking wrong result rather than
an error. It moves onto the layout as a method so each layout keeps an
equivalent, rather than being loosened to accommodate new shapes.

`SidecarRole` gains `BOWTIE2_INDEX` and `HISAT2_INDEX`; `INDEX_ROLE` gains two
entries. `reference_index_status` already iterates `Aligner`, so it picks up
new members with no change.

### 3. Parameter model

`AlignParams` splits into a shared base and per-aligner subclasses:

```python
@dataclass
class BaseAlignParams:          # genuinely shared across all aligners
    aligner: Aligner
    threads: int = 4
    sort_memory_mb: int = 1024
    mark_duplicates: bool = False
```

`Bwa2Params` adds nothing today. `Minimap2Params` carries `preset`, and the
existing preset validation moves into it unchanged. `from_dict` becomes a
dispatcher: read `aligner`, look up the spec, delegate to
`spec.params_class.from_dict`.

**Bowtie2** exposes sensitivity preset (`--very-fast` through
`--very-sensitive`), end-to-end vs `--local`, `-X`/`--maxins` (the insert-size
ceiling, which matters for ChIP-seq fragment length), `--no-mixed` /
`--no-discordant`, `-k` (report up to N alignments), and `-p`.

**HISAT2** exposes `--rna-strandness` (FR/RF/unstranded -- a wrong value
silently breaks downstream strand-specific counting rather than failing),
`--max-intronlen`, `--no-spliced-alignment` (for DNA input),
`--dta`, `-k`, and `-p`.

Computational knobs are per-tool because they genuinely differ. Both bowtie2
and HISAT2 take `-p`, but neither has a memory-ceiling flag: their footprint
is set by index size, which is why memory is a matter for the estimator's
coefficients rather than a user-facing field. `sort_memory_mb` stays shared,
since samtools does the sorting for every aligner.

### 4. Registry-driven parameter form

Each spec carries field metadata, and the dialog renders from it:

```python
@dataclass(frozen=True)
class ParamField:
    key: str
    label: str
    kind: Literal["int", "bool", "select", "text"]
    default: Any
    min: int | None = None
    max: int | None = None
    choices: tuple[Choice, ...] = ()
    help: str = ""              # the explanatory line under the input
    group: Literal["biology", "performance"] = "biology"
```

`group` is what keeps a generated form from becoming an undifferentiated pile
of inputs: "biology" fields render in the dialog body, "performance" fields
under the existing advanced disclosure -- roughly how `AlignDialog` is
already organized.

`GET /api/v1/pipelines/aligners/{name}/schema` serves the field list.

The tradeoff, stated plainly: hand-written forms allow bespoke copy and
layout per tool, and generated forms put `help` text in a Python table. For
about six fields per tool with the biology/performance split, that is a good
trade, and it is reversible per-field if one tool later needs special
treatment.

### 5. Resource estimator

Three pieces: coefficients, an envelope, and an authoritative check.

**The model** is per-aligner coefficients:

```python
@dataclass(frozen=True)
class MemoryModel:
    index_bytes_per_ref_base: float   # dominant term: index size vs genome size
    fixed_overhead_mb: int            # process baseline
    bytes_per_thread_mb: int          # per-worker buffers
    index_build_multiplier: float     # building costs more than loading
```

Approximate values: bowtie2 and HISAT2 load roughly 0.5-1.0 bytes per
reference base (about 3.5 GB for human), bwa-mem2 about 2 bytes per base
(about 6 GB), each plus modest per-thread buffers. STAR's roughly 10 bytes per
base with a ~10x build multiplier is already representable here, which is the
point of defining the model now.

Total estimate is `fixed + index(ref_size) + threads x per_thread + samtools
sort (threads x sort_memory_mb)`. The sort term is the one users actually trip
over and the one the current dialog gives no hint of.

**The envelope** is fetched once per dialog open:

```
GET /api/v1/pipelines/align-envelope?object_id=&reference_id=
```

```json
{
  "cpu_budget": 8, "mem_budget_mb": 16384,
  "reference_bases": 3100000000,
  "input_bytes": 4200000000,
  "models": { "bowtie2": {...}, "hisat2": {...} },
  "needs_index_build": true
}
```

Budgets come from `governor.cpu_budget()` and `governor.mem_budget_bytes()`,
which already read cgroup limits -- so inside Docker this reflects the
container's real allocation rather than the host's. That distinction is what
makes the warning trustworthy. The frontend evaluates the same arithmetic
against these coefficients as inputs change, so there is no request per
keystroke and no second implementation of the formula.

**Three bands**, computed identically in TypeScript and re-checked in Python:

- **OK** -- under about 70% of budget.
- **Warn** (advisory, launch enabled) -- 70-100% of budget, or threads above
  the CPU budget. The copy names the dominant term: "Estimated 14 GB of 16 GB.
  Sort buffer is 8 GB of that (8 threads x 1024 MB)."
- **Block** (launch disabled) -- estimate exceeds budget outright. The copy
  says what to change: "Estimated 34 GB, but this machine allows 16 GB. Reduce
  threads or sort memory."

**The authoritative check** lives in `launch_alignment`, raising
`ValidationError` on a block-band configuration. The dialog can be bypassed --
the API is directly callable -- and an envelope can go stale between opening
the dialog and pressing Launch. The Python check is the one that counts; the
TypeScript copy exists for immediacy.

A limitation worth stating: these coefficients are heuristics from published
tool documentation, not measurements on this hardware. They will be roughly
right and occasionally wrong. The block band is therefore set at
genuinely-impossible rather than merely-tight, so a bad coefficient costs a
spurious warning rather than a blocked run that would have worked.

### 6. Selector redesign

`PipelineToolSelector` becomes list-and-detail:

- **Left rail** -- one compact row per tool: name, version, a one-line
  summary, and a status badge for unavailable tools ("not installed" /
  "unavailable here").
- **Right pane** -- full summary and strengths for the focused tool.

The component's existing comment explains that the selector always renders,
even when only one tool is choosable, so that a greyed-out tool's explanation
stays reachable. A detail pane threatens that: it shows one tool at a time,
and the current roving-tabindex logic *skips* disabled cards, so their reason
would never render.

The resolution: disabled rows stay **focusable and previewable** but not
**selectable**. Arrow keys move focus through every row including disabled
ones, and the detail pane follows focus. `onSelect` still refuses disabled
rows and `Continue` stays gated on a choosable selection. The skip behavior
was correct for a plain radio group and is wrong once the pane carries the
explanation.

`ToolMeta` gains a `one_liner` field for the rail; the existing `summary` is
too long for a row. The selector is shared by trim and QC, which get the same
layout.

### 7. Tool installation and discovery

`bowtie2` and `hisat2` are both packaged in Debian and install into the
backend image alongside the existing tools. `tools.py` gains two probes
(`bowtie2 --version`, `hisat2 --version`, both well-behaved on stdout with a
zero exit) and `config.py` gains `bowtie2_path`, `bowtie2_build_path`,
`hisat2_path`, and `hisat2_build_path`. `TOOL_META` gains entries for both,
and both are `runnable=True` from the start since this slice ships their
handlers.

## Testing

Backend, via `docker compose exec api python -m pytest tests/ -q`:

- Command construction per aligner, against the pure-function layer
  `align_runner` was designed for. Bowtie2 and HISAT2 specifically: that the
  basename rather than the reference path reaches `-x`.
- `PrefixLayout` filename generation, and that `plan_links`' mismatch rule
  still drops a foreign sidecar.
- Parameter validation per subclass, including that an unknown key for the
  wrong aligner is rejected rather than silently ignored.
- Estimator band boundaries. These matter most: the logic is pure arithmetic
  over coefficients, and the band edges are exactly where a wrong comparison
  hides.

Frontend verification is manual at localhost:5173, per CLAUDE.md -- there is
no headless component-testing setup and none is expected. Worth exercising:
the selector's keyboard navigation across a disabled row, the warn and block
bands rendering as parameters change, and a real bowtie2 alignment end to end.

After any change to a queue handler, `docker compose restart worker` before
re-testing -- the worker does not hot-reload, and a job that appears to run
with the fix may still be executing the old in-memory code.

## STAR: what is already decided

Recorded here so the follow-on slice does not relitigate it.

STAR's index is a directory of about ten fixed-name files (`SA`, `SAindex`,
`Genome`, `chrName.txt`, and so on) rather than suffix-named siblings, it
requires a GTF annotation at index time, and it needs roughly ten times the
genome size in RAM -- about 32 GB for human.

**Storage:** each file becomes its own sidecar, with the stored name carrying
a subdirectory (`<ref>_STAR/SA`). Sidecars persist by `name` through
`_apply_build_index` with no model change, so this needs no new object type --
`materialize` creates parent directories before symlinking, and
`DirectoryLayout`'s mismatch check becomes `startswith(f"{reference_name}_STAR/")`.
Verified against the current `_apply_build_index` and `ingest_local_file`
paths.

**Identity:** a STAR index is keyed by *(reference, GTF, sjdbOverhang)*, not
by reference alone. The GTF blob digest folds into the index's identity so
that switching annotation triggers a rebuild rather than silently reusing a
mismatched index. This is the part with no analogue in the current model,
and the main reason STAR is its own slice.

**Load:** STAR is the case the block band exists for. Its memory model is
already representable in `MemoryModel`; a human genome on a 16 GB Docker
allocation should block rather than be OOM-killed twenty minutes in with a log
that says nothing useful.
