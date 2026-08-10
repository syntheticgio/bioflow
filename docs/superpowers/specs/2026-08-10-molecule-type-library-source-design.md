# Molecule type / library source fields — design

**Date:** 2026-08-10
**Status:** Approved, not implemented

Answers "is this RNA-seq or DNA-seq?" directly from the metadata, instead of
requiring a user to read the `Assay` value (e.g. `WGS`) and know that WGS
implies DNA. Splits the single `assay` field into three: a coarse
`molecule_type` (DNA / RNA / Other), a finer `library_source` (SRA's own
vocabulary), and the existing `assay` (WGS, ChIP-seq, ATAC-seq, etc.,
unchanged). Adds a manual, button-triggered inference of `molecule_type` from
the FASTQ's own bases (presence of `U`) for files with no SRA record to draw
on.

## Why split instead of relabel

`assay` already carries a DNA/RNA signal implicitly (`RNA-seq` vs `WGS`), but
it conflates two independent axes: *what kind of library prep* (WGS vs
ChIP-seq vs ATAC-seq — largely orthogonal to molecule type; ATAC-seq is DNA,
ChIP-seq is DNA, RNA-seq is RNA) and *what the library was made from*
(genomic DNA vs total RNA vs cDNA). Forcing both into one enum means a reader
has to already know the mapping. Splitting them lets `molecule_type` answer
the literal question this feature exists for, while `assay` keeps doing what
it already does.

`library_source` sits between the two: it is SRA's own field
(`LIBRARY_SOURCE`), structured and directly authoritative, but more granular
than a binary DNA/RNA split (`METAGENOMIC`, `SYNTHETIC`, `VIRAL RNA` don't
collapse cleanly into either bucket without a judgment call). Keeping it as
its own field means that judgment call is visible rather than hidden inside
`molecule_type`'s derivation.

## Schema — three fields in the `Experiment` group

`backend/app/metadata/schemas.py`, in `COMMON_FIELDS`, immediately before the
existing `assay` field:

```python
FieldDef(
    "molecule_type",
    "Molecule type",
    type=FieldType.ENUM,
    options=("DNA", "RNA", "Other"),
    group="Experiment",
    suggested=True,
    help="What the library was made from. Derived from the SRA record's "
         "library source, or inferred from the FASTQ's own bases via the "
         "Infer button when no SRA record is available.",
),
FieldDef(
    "library_source",
    "Library source",
    type=FieldType.ENUM,
    options=("Genomic", "Transcriptomic", "Metagenomic",
             "Metatranscriptomic", "Synthetic", "Viral RNA", "Other"),
    group="Experiment",
    open_vocabulary=True,
    help="SRA's own library source classification, where available.",
),
```

`assay`'s `FieldDef` is unchanged. All three render in the existing
`Experiment` group of `SchemaMetadataEditor`, no frontend schema-editor
changes needed — the component already renders whatever `fields_for()`
returns.

`open_vocabulary=True` on `library_source` matches `assay`'s existing
pattern: an SRA value outside the modeled options still gets stored and
shown, not dropped. `molecule_type` is closed (`open_vocabulary` unset /
`False`) since DNA/RNA/Other is meant to be an exhaustive, simple bucket —
nothing SRA emits for `LIBRARY_SOURCE` should fail to land in one of the
three.

## SRA mapping — `backend/app/metadata/sra.py`

`library_source` is already parsed onto `SraMetadata` (line 68) but
discarded by `to_metadata()`. Add two lookups next to the existing
`_STRATEGY_MAP` / `_map_strategy`:

```python
# SRA library sources map onto our library_source vocabulary the same way
# strategies do -- anything unrecognized passes through unchanged.
_SOURCE_MAP = {
    "GENOMIC": "Genomic",
    "TRANSCRIPTOMIC": "Transcriptomic",
    "METAGENOMIC": "Metagenomic",
    "METATRANSCRIPTOMIC": "Metatranscriptomic",
    "SYNTHETIC": "Synthetic",
    "VIRAL RNA": "Viral RNA",
    "OTHER": "Other",
}

# Coarse DNA/RNA bucket derived from library source. Not a judgment call SRA
# makes explicitly -- METAGENOMIC and SYNTHETIC are assumed DNA-like (both are
# overwhelmingly genomic-DNA preps in practice), anything not recognized here
# maps to "Other" rather than a guess.
_SOURCE_TO_MOLECULE = {
    "GENOMIC": "DNA",
    "METAGENOMIC": "DNA",
    "SYNTHETIC": "DNA",
    "TRANSCRIPTOMIC": "RNA",
    "METATRANSCRIPTOMIC": "RNA",
    "VIRAL RNA": "RNA",
}


def _map_source(source: str) -> str:
    return _SOURCE_MAP.get(source.strip().upper(), source)


def _map_molecule_type(source: str) -> str:
    return _SOURCE_TO_MOLECULE.get(source.strip().upper(), "Other")
```

`to_metadata()` gains, alongside the existing `library_strategy` block:

```python
if self.library_source:
    out["library_source"] = _map_source(self.library_source)
    out["molecule_type"] = _map_molecule_type(self.library_source)
```

No changes to `enrich_from_sra()` itself — its never-overwrite rule (skip
any key the user, or a prior enrichment, already set) already covers both
new keys for free, and the merge logic in
`queue/results.py:_apply_ingest_headers` (`{**obj.metadata,
**enrichment["values"]}`) already only adds keys, never clobbers.

## No filename/content inference on ingest

`molecule_type` and `library_source` are populated only from
`enrich_from_sra()`. Unlike `detect_sequence_type` (DNA vs protein from
filename tokens), there is no reliable filename signal for DNA-seq vs
RNA-seq — a name containing `rna` doesn't establish the sequencing library
was actually RNA — so no automatic detector is added for these fields.
Files with no SRA accession keep both fields blank until the user either
fills them by hand or uses the FASTQ inference button below (which only
covers `molecule_type`).

## FASTQ-based inference for `molecule_type`

**User-triggered only** — never runs as part of ingest, enrichment, or any
background job. A button next to the `molecule_type` field, visible only
when the object's format is FASTQ (mirrors the `SEQUENCE_TYPE_ELIGIBLE_FORMATS`
gating already used for `detect_sequence_type`).

**Signal:** presence of `U` in sampled sequence lines means RNA (direct RNA
sequencing — rare but unambiguous); its absence defaults to `DNA`. This is a
real limitation, not an edge case: most RNA-seq data is reverse-transcribed
to cDNA before sequencing and reads as `T`, identical to DNA. The UI must not
claim certainty it doesn't have — see UI copy below.

**Sampling:** first ~2,000 FASTQ records (every 4th line starting at line 2,
the sequence line), not the whole file. Base composition is uniform
throughout a FASTQ, so a small sample is exactly as informative as a full
scan for this signal, at a small constant cost instead of one proportional
to file size (files here run into multiple GB).

**New backend endpoint**, `backend/app/api/v1/objects.py`, following the
`reingest_object` pattern (`object_service.object_with_blob`, then act):

```
POST /api/v1/objects/{object_id}/infer-molecule-type
```

Runs synchronously (no job queue — this is a bounded, sub-second-to-few-second
read, unlike `reingest_object`'s full pipeline dispatch) and returns:

```json
{"molecule_type": "DNA", "basis": "sampled 2000 reads, no U found"}
```

It does **not** write to the object's metadata. The frontend applies the
returned value to the in-progress edit form, same as any other field edit —
the user still clicks the existing Save button to persist it. This keeps the
endpoint idempotent and side-effect-free, and keeps "infer, then decide
whether to keep it" a single undo-by-not-saving action.

**Overwrite behavior:** clicking Infer always overwrites the current form
value, including an SRA-derived one already there. This is a deliberate,
one-off user action, not automatic enrichment — the never-overwrite rule
that governs `enrich_from_sra()` exists to protect against a *background*
process silently discarding a user's edit, which doesn't apply to an
explicit button click. The user can decline to Save if they didn't mean to
overwrite it.

**Module:** `backend/app/metadata/infer_molecule.py`, reusing the
gzip-transparent file-opening pattern from
`backend/app/pipelines/tile_scanner.py:_open_fastq` (sniffs the first two
bytes for the gzip magic number rather than trusting the extension). Exposes
one function:

```python
def infer_molecule_type(path: Path, *, sample_reads: int = 2000) -> dict:
    """Sample a FASTQ's sequence lines and report DNA or RNA by base content.

    Never raises for a well-formed FASTQ; returns {"molecule_type": None,
    "basis": "..."} if the file is empty or no sequence lines are found in
    the sampled region. Caller is responsible for translating that into a
    4xx/204 at the API layer -- this function only reads and classifies.
    """
```

**Docstring/module-level note**, matching the tone of `detect_sequence_type`'s
"never a guess" framing but inverted — this function *is* a best-effort
heuristic, explicitly scoped to manual, opt-in use, and must never be called
from `enrich_from_sra()`, `ingest_headers`, or any scheduled job.

**UI copy:** the applied value should carry its basis visibly, not render as
an unqualified "DNA" indistinguishable from an SRA-sourced one. Exact
wording is a frontend-implementation detail, but the plan should show the
`basis` string somewhere near the field (tooltip, inline caption, or toast)
so "no U found → DNA" doesn't read as more certain than it is.

## Backfilling existing files

There is no periodic SRA re-verification job in this codebase — the
`Verified` timestamp visible in the UI comes from `verify_files` (a blob
existence check, `backend/app/queue/handlers.py`, scheduled every 60s), which
is unrelated to `enrich_from_sra()`. `enrich_from_sra()` only runs on initial
ingest or when a user clicks the existing per-object **Re-ingest** button.

For files that already have an SRA accession and were ingested before this
change (like the `DRR1066343_1.trimmed.fastq` example this feature was
scoped around), a **one-time backfill script** re-derives `molecule_type` and
`library_source` without touching anything else:

- Location: `backend/scripts/` (or wherever this repo's existing one-off ops
  scripts live — confirm exact convention in the plan).
- For every `DataObject` with an `sra_run`/`sra_experiment`/accession set in
  metadata and missing `molecule_type` in metadata: re-run the SRA lookup
  (`sra.lookup(accession)` → `to_metadata()`), and merge only the two new
  keys into `obj.metadata` if still absent (defensive even though the source
  data guarantees it, since this reuses fields also touched by the ordinary
  ingest path).
- Not a standing job — this repo's `docker-compose` / single-user model
  favors a run-once script over new scheduler infrastructure for a one-time
  data-shape migration. Once existing files are backfilled, every future
  file gets both fields at ingest time via the `to_metadata()` change above,
  so nothing further is needed.
- Idempotent: safe to run more than once (only fills missing keys, same
  never-overwrite guarantee `enrich_from_sra()` already relies on).

## Tests

- `backend/tests/storage/test_sra.py` — extend for `_map_source` /
  `_map_molecule_type`: recognized values, unrecognized passthrough (source)
  vs. `"Other"` fallback (molecule type), absent `library_source` (neither
  key emitted).
- `backend/tests/metadata/test_sra_resolver.py` — assert the two new
  `to_metadata()` keys against the existing fixtures
  (`sra_SRR11768093.xml`: `LIBRARY_SOURCE=GENOMIC` → `library_source:
  "Genomic"`, `molecule_type: "DNA"`).
- New `backend/tests/metadata/test_infer_molecule.py` — synthetic FASTQ
  fixtures: all-`T` sequence (→ DNA), sequence containing `U` (→ RNA),
  gzipped variant, empty file, file with headers but no sequence lines in
  the sampled window (graceful `None`, not a crash).
- New API test for `POST /{object_id}/infer-molecule-type` — asserts the
  endpoint returns a value without writing to `obj.metadata` (re-fetch the
  object after the call and confirm `metadata` is unchanged).
- Backfill script — a test or a documented manual verification step (to be
  decided in the plan) confirming it only touches objects missing the new
  keys and leaves manually-set values untouched.
