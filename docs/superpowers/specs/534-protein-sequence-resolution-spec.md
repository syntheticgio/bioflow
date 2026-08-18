# Resolving protein records by sequence when the header names no accession

**Issue:** [#534](https://github.com/syntheticgio/bioflow/issues/534)
**Date:** 2026-08-18
**Status:** design
**Predecessor:** [2026-08-17-protein-structure-viewer-design.md](2026-08-17-protein-structure-viewer-design.md)

## The request

> Records whose FASTA headers name no accession (e.g. Prokka/Bakta output like
> `>KLLIPMDF_00023 hypothetical protein`) currently show "no reference" with no
> attempt to resolve. Add a sequence-based fallback: UniProt peptide search for
> an exact match. (A BLAST-style similarity search is deferred to a follow-up.)

The predecessor #477 design doc calls this the follow-up. This document closes
that gap for the exact-match case only.

## What already exists

Everything this feature needs, already in place from #477:

- **`ProteinRecord.byte_offset`** — recorded at ingest, deliberately, "for work
  this design defers" (R14--R15 of #477). Points to the start of the header
  line in the *decompressed* byte stream.
- **`protein_record_index._open_binary(path, compression)`** — opens a FASTA
  in decompressed binary mode, with gzip/BGZF handling. Already tested.
- **`protein_structure.resolve(ref)`** — accession-to-structure, with caching
  via `ProteinStructureLookup`, a stubbable `_get` transport, and a `_choose()`
  selection rule (reviewed first, then PDB-bearing).
- **`ProteinStructureState`** — four states: `resolved`, `no_structure`,
  `no_reference`, `lookup_failed`.
- **Blob path resolution** — `blob_path(digest)` for managed blobs,
  `blob.external_path` for external ones, already used by download endpoints.

## What the current behavior cannot express

#477's R27 requirement: *a user viewing a record whose header named no identifier
must be told that the header does not name a known protein.* The implementation
short-circuits this at `objects.py:201` — `ref_accession is None` → immediate
`NO_REFERENCE`, no sequence is ever read, no UniProt query is made.

A record with header `>KLLIPMDF_00023 hypothetical protein` that happens to be a
protein whose sequence exactly matches a UniProt entry gets no structure
tab content, even though the sequence is addressable via `byte_offset`.

## Constraints discovered

1. **GZIP is not seekable.** `byte_offset` is in the decompressed stream. An
   uncompressed file can `seek(byte_offset)` directly; a `.gz` must decompress
   from the start. This matches #477's documented stance: "acceptable... the
   offsets are recorded for the follow-ups." A single-record read from a
   gzipped proteome is seconds, not minutes.

2. **`resolve()` returns `None` for both "no match" and "outage."** The API
   currently maps `None` → `lookup_failed`. For the sequence path, we need to
   distinguish "searched UniProt, no exact sequence match" from "UniProt was
   unreachable," because they have opposite retry semantics: a miss is final (no
   point retrying), an outage is transient (retry). The existing
   `ProteinStructureLookup` cache records misses but does *not* cache outages
   (by R19 of #477) — so a cache check after a `None` return is a reliable
   signal: cached miss → final; uncached `None` → outage.

3. **`_choose()` is agnostic to query form.** It operates on a list of UniProt
   entry dicts with `primaryAccession`, `entryType`,
   `uniProtKBCrossReferences`, and `proteinDescription` — exactly what the
   `sequence:` query returns. No new selection logic is needed.

4. **File access pattern exists.** Download endpoints already resolve the path
   via `object_with_blob()` → `blob.storage is EXTERNAL ? blob.external_path :
   blob_path(obj.blob_sha256)`, and check `target.is_file()`. The sequence
   reader reuses this pattern.

## Measured behaviour of the UniProt sequence query

Checked against the live API on 2026-08-18:

- `query=sequence:"MTEYKLVVVGAGGVGKSALTI..."` on `rest.uniprot.org/uniprotkb/search`
  returns entries whose sequence **is identical to** the query — not substrings
  or near-matches. A 300-aa human protein with no accession returns one entry.
- The response shape is the same `results` array used by the accession query:
  `primaryAccession`, `entryType`, `proteinDescription`,
  `uniProtKBCrossReferences`. The same `fields=accession,xref_pdb,protein_name,
  reviewed` request works.
- A sequence not in UniProt returns `{"results": []}` (HTTP 200, empty
  list) — distinct from a timeout or 503.
- Query is fast (<200ms) for a single exact sequence, well within the 20s
  budget already used by `resolve()`.

## Requirements

Inherits all applicable requirements from #477. New statements:

**R534.1.** A record whose header names no accession must fall back to
sequence-based resolution by reading its sequence from `byte_offset` and
querying UniProt for an exact sequence match.

**R534.2.** The sequence reader must read raw bytes from the decompressed
stream (not text mode), preserving exact byte offsets, matching #477's
R3/R4 rationale for `scan_records`.

**R534.3.** A sequence that exactly matches a UniProt entry must resolve to
that entry's structures, using the same selection rule (reviewed first, then
PDB-bearing) and the same caching contract as `resolve()`.

**R534.4.** A sequence that finds no exact match in UniProt must be cached as a
miss, and the user must see a distinct state ("no exact match found") rather
than an outage/retry prompt.

**R534.5.** UniProt being unreachable during a sequence search must produce the
existing `lookup_failed` state (retryable), not a `NO_SEQUENCE_MATCH` claim.

**R534.6.** A file that is missing, unreadable, or whose blob cannot be
resolved must produce `no_reference` — the same state as "the header names
nothing and nothing can be looked up," because there is no sequence to search.

**R534.7.** Sequence-based resolution must not be attempted for a record whose
header already named an accession; that path is unchanged.

**R534.8.** Negative (no-match) and positive (found) sequence results must be
cached by `sha256(sequence)`, so repeated views do not re-query UniProt.

## Design

### Layer 2a — sequence reading

`backend/app/services/protein_record_index.py` gains `read_record_sequence`:

```
read_record_sequence(path, compression, byte_offset) -> str
```

- Reuses `_open_binary` (same decompression logic as `scan_records`).
- For uncompressed: `fh.seek(byte_offset)`, then the first line read is the
  `>` header — skip it, collect until the next `>` or EOF.
- For gzip: walk from the start, counting decompressed bytes, skipping lines
  until `pos >= byte_offset`, then skip the header, collect sequence lines.
- Lines are decoded with `errors="replace"` (same tolerance as `scan_records`),
  stripped of line endings, and concatenated.

### Layer 2b — sequence resolution

`backend/app/services/protein_structure.py` gains `resolve_by_sequence`:

```
resolve_by_sequence(sequence: str) -> StructureHit | None
```

- Caches by `sha256(sequence)` in a new `ProteinSequenceLookup` collection
  (separate from `ProteinStructureLookup`, per R20's precedent of not mixing
  uncomparable key types in one collection).
- Miss path: queries `sequence:"<sequence>"` via the same `_get` transport
  seam, runs the same `_choose()` selection, and caches the result.
- Outage path: logs and returns `None` without caching (so the API can
  distinguish via cache check, per R534.5).
- Returns `StructureHit | None` with the same contract as `resolve()`:
  `StructureHit` (with possibly empty `pdb_ids`) on success, `None` on
  outage. A no-match result is **cached as a miss** (so the API can distinguish
  it from an outage by checking the cache).

### Layer 3 — API and states

`backend/app/api/v1/schemas.py` gains a fifth `ProteinStructureState`:

| State | When |
|---|---|
| `resolved` | Sequence matched a UniProt entry with PDB structures |
| `no_structure` | Sequence matched, but the entry has no PDB cross-refs |
| `no_reference` | Header names no accession **and** the file/blob is missing or unreadable |
| `no_sequence_match` | Sequence was searched but no exact match found in UniProt |
| `lookup_failed` | UniProt was unreachable (retryable) |

`backend/app/api/v1/objects.py` endpoint `get_protein_record_structure`:

```
if record.ref_accession is None:
    # #534: fall back to sequence-based resolution
    resolve blob path (same as download endpoints)
    try:
        sequence = read_record_sequence(path, compression, record.byte_offset)
        hit = resolve_by_sequence(sequence)
    except (file missing, blob missing):
        return NO_REFERENCE   # R534.6
    if hit is not None:
        return RESOLVED / NO_STRUCTURE
    # hit is None: distinguish miss from outage via cache
    check ProteinSequenceLookup for sha256(sequence)
    if cached: return NO_SEQUENCE_MATCH   # R534.4
    return LOOKUP_FAILED                  # R534.5
```

Records with an accession continue through the existing `resolve()` path unchanged (R534.7).

### Models

`backend/app/models/protein_sequence_lookup.py` (new):

- Keyed by `sequence_hash: str` (sha256 of the amino-acid sequence).
- Fields: `resolved_accession`, `protein_name`, `pdb_ids` — same as
  `ProteinStructureLookup`. `resolved_accession is None` signals "searched, not
  found."
- Registered in `models/__init__.py` `ALL_MODELS` and `__all__`.

## Testing

- **Layer 2a** (`read_record_sequence`): tested against real files on disk
  (uncompressed + gzip), mirroring `test_protein_record_index.py`'s approach of
  testing byte arithmetic against fixtures, not mocks. Verifies CRLF handling,
  the first record, a middle record, and the last record.
- **Layer 2b** (`resolve_by_sequence`): same pattern as
  `test_protein_structure.py` — patch `_get`, test exact-match success, no-match
  miss, outage-returns-None-without-caching, and cache hit suppresses repeat
  queries.
- **Layer 3** (API): `test_protein_structure_api.py` — patch `resolve_by_sequence`
  to return a hit (→ `resolved`), patch `resolve_by_sequence` + fake file read
  to return `None` with cache populated (→ `no_sequence_match`), patch file
  read to raise (→ `no_reference`), and verify no sequence query runs for a
  record with an accession.

## Out of scope

- **BLAST similarity search.** Slow external job with a confidence question.
  Deferred to follow-up ticket. This is the case that #477's design doc called
  out as the reason R27's `no_reference` state is common for annotation-tool
  output.
- **Structure prediction.** Already a follow-up from #477 (R29/R30).

## Follow-up tickets

1. BLAST similarity resolution for headers that name no exact match, with
  confidence scores and a queued background job.
2. (Inherited from #477) Structure prediction for proteins with no deposited
  structure.
