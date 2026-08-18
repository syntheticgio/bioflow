# Viewing protein structures from a protein FASTA

**Issue:** [#477](https://github.com/syntheticgio/bioflow/issues/477)
**Date:** 2026-08-17
**Status:** design

## The request

> Add in a protein viewer so users can view proteins directly in the program.
> A user is able to go to a 3D view tab and toggle between the proteins that
> are in a protein's file (.faa or other specifically protein file). It should
> try to grab the 3d structure from the protein data bank or other source based
> on either the sequence or on the sequence name.

Plus a greyed-out prediction option, deliberately unimplemented, with a
follow-up ticket to make it real.

## What already exists

This feature is largely a second front door onto machinery this repo already
has, which is the main reason it is worth building now.

- **`frontend/src/components/StructureViewerModal.tsx`** renders a 3D structure
  today, for the variants table. It embeds NCBI's iCn3D in a cross-origin
  iframe -- there is no 3D library in `package.json` and none is proposed here.
  It carries a 15-second load timeout and an offline escape hatch, both of
  which exist because an iframe fires no error event for a request that never
  answers.
- **`backend/app/services/structure_lookup.py`** resolves a gene symbol plus a
  taxid to a protein and its PDB IDs, caching negative results because 65% of
  lookups find no structure.
- **`backend/app/metadata/uniprot.py`** already wires up UniProt REST with a
  stubbable `_get` transport seam and a strict accession regex.
- **`ObjectRole.PROTEIN`** exists, and `storage/parsers._parse_fasta` already
  walks every record of a FASTA at ingest.

What does not exist is any way to get from *a record inside a protein file* to
that machinery. The variants path is keyed by gene symbol; a `.faa` has no
gene symbols, it has FASTA headers.

## What the current data model cannot express

`_parse_fasta` stores `facts["sequence_names"]`, but it cannot serve this
feature, for three independent reasons:

1. **It is capped at 50** (`MAX_STORED_CONTIGS`) and sets
   `sequence_names_truncated` beyond that. A bacterial proteome is ~4,000
   records; human RefSeq is ~120,000. Fifty names is not a browsable list.
2. **It stores only the first whitespace token.** For `>NP_009342.1 Cdc19p
   [Saccharomyces cerevisiae S288C]` it keeps `NP_009342.1` and discards the
   description -- which is the part a person picks a protein by.
3. **It records no byte offsets**, so fetching one record's sequence means
   re-scanning the file.

That third point is what decides the storage question below. The two follow-up
tickets this design creates (sequence-similarity fallback, and structure
prediction) both need *the bytes of one record*, addressable without a rescan.

## Constraints discovered

Three facts about the existing code constrain the implementation and are easy
to get wrong.

**`storage/parsers.py` must stay pure.** Its module docstring commits to it:
"All functions are synchronous and cancellable; callers run them off the loop."
Every function returns a facts dict and writes nothing. The record index must
therefore be written by the *caller*, not inside the parser.

**`ingest_headers` is `HandlerMode.THREAD`.** It runs in a worker-pool thread
with no event loop. Per CLAUDE.md, reaching Motor from there via
`asyncio.run()` raises "attached to a different loop" -- the process's Mongo
client is bound to the loop `connect_to_mongo()` ran on. The write must use
`app.db.client.run_from_thread`.

**`ingest_headers` promises idempotency.** Its docstring: "a repeat run
recomputes the same facts." A record index that appends on re-run breaks that.

## Measured behaviour of the resolution path

Both query forms were checked against live UniProt on 2026-08-17 before this
design was written. Two results changed it.

**A RefSeq cross-reference query can return several entries.**
`xref:refseq-NP_000537` (human TP53) returns three: P04637 (reviewed, 295 PDB
entries) alongside isoform entries such as K7PPA8 with zero. Taking the first
result blindly can select a structure-less isoform over the entry that has 295
structures. A selection rule is required, not optional.

**A version-stripped RefSeq ID can cross-reference to an unexpected entry.**
`xref:refseq-NP_009342` resolved to P39715 at 212 aa. Yeast CDC19/PYK1 is
roughly 500 aa. Unlike the gene-symbol path, there is no residue position to
guard against this with -- `structure_lookup`'s length guard exists precisely
because a *symbol* is ambiguous, and it has nothing to check here. This is a
known limitation, and R21 is its mitigation: show the user what was resolved
so a wrong answer is visible rather than silent.

## Requirements

### Reading records out of a protein FASTA

**R1.** A protein FASTA object must have every one of its records recorded at
ingest, up to a cap, so that a user can browse them without the file being
re-read.

**R2.** Each recorded record must carry its identifier, its description text,
its sequence length, and its byte offset within the file.

**R3.** The description must preserve everything after the first whitespace
token in the header, so a user can tell two records apart by their text.

**R4.** Recording must apply only to objects whose format is FASTA and whose
role is `protein`. No other object gains records.

**R5.** At most 150,000 records must be recorded for one object. Above that,
the object must be marked as having an incomplete record list. The cap is set
above the largest realistic input -- human RefSeq is roughly 120,000 records --
so that it does not trip on an ordinary proteome, while still bounding growth
for a pathological file.

**R6.** A user viewing an object with an incomplete record list must be able to
tell that the list is incomplete. Where the file's true record count is known,
it must be stated; where it is itself an estimate -- `_parse_fasta` stops
scanning at `FASTA_EXACT_LIMIT`, so a file above 256 MB has no exact count --
it must be presented as an estimate rather than as a fact.

**R7.** Re-running ingest for an object must leave exactly one set of records
for that object, not a duplicated or appended set.

**R8.** Recording records must not change the facts `_parse_fasta` already
produces, including `sequence_names` and `sequence_names_truncated`.

**R9.** A failure to record records must not fail the ingest job. The object
must still gain its facts.

### Identifying what a record names

**R10.** A record header naming a UniProt accession in UniProt's own FASTA
format (`>sp|P00924|ENO1_YEAST ...`) must be recognised as that accession.

**R11.** A record header naming a RefSeq protein accession (`NP_`, `XP_`, or
`WP_` prefixes) must be recognised as that accession.

**R12.** A record header naming neither must be recorded as naming no
identifier. This is an ordinary outcome, not an error.

**R13.** Identification must not perform any network request. It reads the
header text only.

### Resolving an identifier to a structure

**R14.** A UniProt accession must resolve to that UniProt entry's PDB
cross-references.

**R15.** A RefSeq protein accession must resolve through UniProt's RefSeq
cross-reference index to a UniProt entry's PDB cross-references.

**R16.** When a RefSeq cross-reference query returns more than one UniProt
entry, a reviewed entry must be preferred over an unreviewed one.

**R17.** Among entries of equal review status, an entry carrying at least one
PDB cross-reference must be preferred over one carrying none.

**R18.** A resolution result must be cached by accession, so that reopening the
same record does not re-query UniProt.

**R19.** A resolution that finds no structure must be cached as such, so that
the common no-structure case does not re-query UniProt on every view.

**R20.** Resolution must not reuse the `structure_lookups` collection. That
collection is keyed by gene symbol and organism and carries a sequence-length
guard whose purpose is symbol disambiguation; an accession is unambiguous and
has no residue position to guard with.

**R21.** A user viewing a resolved structure must be able to read which UniProt
accession was resolved and that entry's protein name, so that a mis-resolution
is visible rather than silent.

**R22.** UniProt being unreachable must produce a retryable error state, not a
claim that the protein has no structure.

### Viewing

**R23.** A user viewing a protein FASTA object must be offered a structure view
alongside the object's existing tabs.

**R24.** An object that is not a protein FASTA must not be offered that view.

**R25.** A user must be able to move between the records of the file and see
the structure view update to the selected record.

**R26.** A user must be able to find a record by typing part of its identifier
or description, without paging through the whole list.

**R27.** A user viewing a record whose header named no identifier must be told
that the header does not name a known protein.

**R27b.** The fallback for such a record is an exact sequence match against
UniProt (#534): the record's sequence is read from its byte offset and searched
as `sequence:"` — no BLAST or similarity search. A cached miss (no UniProt
entry has this exact sequence) is a permanent, non-retryable state.
UniProt unreachability during the fallback is retryable, same as R22.

**R28.** A user viewing a record that resolved but has no deposited structure
must be told that no experimental structure exists, phrased so that it does not
read as a failure. This is the majority outcome and must not look like a bug.

**R29.** A user must be offered a structure-prediction control that is visibly
present and disabled, and must be able to read why it is disabled.

**R30.** The prediction control must not initiate any computation when
activated. It is a placeholder for a follow-up ticket.

**R31.** The 3D rendering must reuse the iCn3D embedding already used by the
variants structure viewer, including its load timeout and its offline escape
hatch, rather than introducing a second rendering implementation.

### Non-functional

**R32.** Recording records for a 120,000-record protein FASTA must not increase
that object's ingest wall time by more than 50%, measured against the same file
ingested without recording.

**R33.** A page of records must be served in under 500 ms for a file at the
150,000-record cap, including a search over identifier and description. Stated
as a single-request budget rather than a percentile: this is a single-user
local tool, and there is no concurrent load to take a percentile over.

**R34.** No requirement here may cause an ingest to fail on a file that
currently ingests successfully.

**R35.** Every external call must degrade to a stated in-app outcome rather
than an unhandled error, matching the contract `structure_lookup.py` and
`uniprot.py` already hold: a UniProt outage reports "no structure available",
never a 500.

## Design

### Layer 0 -- header parsing

`backend/app/metadata/protein_headers.py`, new. Pure functions, no I/O,
satisfying R10--R13. Returns a discriminated result -- a UniProt reference, a
RefSeq reference, or nothing -- from a header string.

Deliberately strict regexes, following `uniprot.is_valid_accession`'s recorded
reasoning: a loose accession pattern classifies the gene symbol EGFR as an
accession and returns nothing. This module is the TDD core of the feature;
every real header shape is a test case, and it needs no network to test.

### Layer 1 -- the record index

A `ProteinRecord` document keyed by `(object_id, ordinal)`, with a unique
compound index. Holds identifier, description, length, byte offset, and the
Layer 0 parse result.

Two indexes, because R26's search and R25's paging are different queries: the
unique `(object_id, ordinal)` index orders the list, and a second
`(object_id, identifier)` index serves identifier search. Description search
is a prefix/substring match scoped to one object rather than a Mongo text
index -- the corpus is at most 150,000 short strings under a key already in
hand, and a text index would be a third index maintained at ingest for a query
that never spans objects.

Written from `ingest_headers` in `backend/app/queue/handlers.py`, after
`parsers.parse` returns and beside the existing SRA and assembly enrichment
steps -- which is the seam that keeps `parsers.py` pure. The write goes through
`app.db.client.run_from_thread`, per the THREAD-mode constraint above.

R7's idempotency is delete-then-insert scoped to the object, not an upsert per
record: a re-ingest of a *changed* file must not leave orphaned records from
the previous one.

R9 means the whole step is wrapped so that a failure logs and returns rather
than failing the job. Facts are the primary product of this handler; records
are additive.

### Layer 2 -- resolution

`backend/app/services/protein_structure.py`, new, with its own
`ProteinStructureLookup` collection keyed by accession (R18--R20). Queries go
through `uniprot.py`'s existing `_get` transport seam so tests stub the network
at a boundary this repo already uses.

The selection rule for R16/R17 -- reviewed first, then PDB-bearing -- is
written as one function over the candidate list, which makes the multi-result
TP53 case from the measurements above a direct unit test.

Every failure path returns nothing and logs, never raises (R35), matching the
contract `structure_lookup.py` states in its own module docstring.

### Layer 3 -- the UI

A `Structure` tab added to `tabsFor()` in `DetailPanel.tsx`, gated on role
(R23/R24). Left: a paged, searchable record list (R25/R26). Right: the
resolved structure.

`StructureViewerModal`'s iframe body is extracted into a shared `Icn3dFrame`
component so the variants modal and this tab share one implementation (R31).
That extraction is a separate commit from the feature that consumes it, per
CLAUDE.md's separable-commits rule.

Five end states are worded distinctly, because the existing modal's clearest
lesson is that "no structure" is the common case and must not read as a
failure:

| State | Message | Predict button |
|---|---|---|
| Resolved | Structure found | Offered |
| Resolved, no PDB entries (R28) | No experimental structure deposited | Offered, disabled |
| Header names nothing (no sequence match, #534) | The record has no identifiable protein name | Offered, disabled |
| UniProt unreachable (R22) | Retryable error | Offered, disabled |
| iCn3D failed to load | Existing offline escape hatch | Offered, disabled |

The prediction control (R29/R30) is disabled with a tooltip naming the
follow-up ticket.

## Testing

Backend gets `pytest` coverage per layer: Layer 0 exhaustively (pure, no
network), Layers 1 and 2 with the transport stubbed at `_get`.

Per CLAUDE.md's suggestion-rules precedent, the indexer is additionally checked
against a real `.faa` in the running stack rather than only against fixtures.
That precedent is directly on point: hand-built fixtures that already look the
way the code expects are exactly what hid the `protein.faa` /
`cds_from_genomic.fna` misclassification through a fully green suite.

UI verification is manual at localhost:5273 via `./ops/worktree-up.sh`, per the
repo's stated position that there is no headless component-testing setup and
none is expected.

## Out of scope

Each of these is deliberately excluded, with the reason recorded so the
decision survives.

- **Structure prediction (AlphaFold or equivalent).** Requested as greyed out
  by the issue itself. Follow-up ticket.
- **Sequence-similarity fallback.** The exact-match variant was implemented as a
  follow-up ([#534](https://github.com/syntheticgio/bioflow/issues/534)): a record
  whose header names no accession is searched against UniProt by exact sequence.
  The similarity variant (BLAST-style, near-matches) remains deferred: it is a
  slow external job, and near-matches introduce a confidence problem that
  identifier resolution does not have. Follow-up ticket. Note that its absence
  is what makes R27's state common for annotation-tool output such as
  `>KLLIPMDF_00023 hypothetical protein`, where the prediction path is the real
  answer.
- **Residue highlighting within a structure.** The existing modal already
  defers this for a documented reason: iCn3D's selector needs an explicit
  chain, and multi-chain entries make a guessed chain a confidently wrong
  answer. Unchanged here.
- **Re-pointing `facts["sequence_names"]` at the new collection.** It feeds the
  Quality tab at its current cap of 50 and works; changing it is scope creep
  into a working surface (R8).

## Follow-up tickets

1. Structure prediction for a protein with no deposited structure -- makes
   R29's disabled control real.
2. Sequence-similarity resolution for headers that name no identifier --
   approach B above, covering annotation-tool output. (The exact-match
   variant, #534, was implemented: see `ProteinSequenceLookup` and
   `protein_structure.resolve_by_sequence`.)
