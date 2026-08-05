# Paired-read detection beyond filenames

Written 2026-08-05 for GitHub issue
[#17](https://github.com/syntheticgio/bioflow/issues/17). Backlog source:
`Mate detection is filename-only` in `docs/TODO.md`, raised 2026-07-27.

## Problem

`app/pipelines/pairing.py` matches paired-end files by stripping an R1/R2 token
from the end of the filename. The TODO entry names two consequences: files
outside the convention never pair, and two unrelated files could in principle
pair if their names collide after the token is removed.

The entry says read IDs inside the files "would be authoritative, but checking
them means decompressing two files to compare their first records". **That cost
is already paid.**

## What already exists, unused

`storage/parsers._parse_fastq` captures the first three read IDs of every FASTQ
at ingest:

```python
if len(ids) < 3:
    ids.append(header[1:].strip())
...
facts["first_read_ids"] = ids
```

`first_read_ids` has exactly two references in the repository: the line that
writes it, and a display label in `frontend/src/components/FactsTable.tsx`.
Nothing reads it for any decision.

It is captured through `_open_text`, which already handles `Compression.GZIP`
and `Compression.BGZF`, so the compressed case costs nothing extra. Measured on
the real database: **6 of 6 FASTQ objects carry it.**

There is a second unused signal. `metadata/sra.py` parses NCBI's
`LIBRARY_LAYOUT` and persists it:

```python
if self.library_layout:
    out["read_type"] = (
        "paired-end" if self.library_layout.upper() == "PAIRED" else "single-end"
    )
```

On real objects it is present and correct:

    ERR17609896_1.fastq          read_type='paired-end'   assay='WGS'
    ERR17609896_2.fastq          read_type='paired-end'   assay='WGS'
    ERR16145610.fastq            read_type='single-end'   assay='OTHER'
    SRR39891651.fastq            read_type='single-end'   assay='Amplicon'

`grep read_type` across `backend/app/` returns two hits: the line above, and a
field name in `metadata/schemas.py`. Nothing in `pairing.py`, `results.py`, or
`pipeline_service.py` reads it. The authoritative answer from NCBI -- "this run
is paired-end" -- sits on the object while pairing guesses from the filename.

So this issue needs **no new file I/O at all**. Both signals are already
persisted; the work is reading them.

## What the real data proved

Every design decision below came from querying the running database rather than
reasoning about FASTQ in the abstract. Three findings each kill an obvious
implementation.

### 1. Comparing the stored header would reject a genuine pair

    ERR17609896_1.fastq   'ERR17609896.1 LH00201:115:22JLCTLT4:1:1101:22244:1112 length=150'
    ERR17609896_2.fastq   'ERR17609896.1 LH00201:115:22JLCTLT4:1:1101:22244:1112 length=149'
                                                                                 ^^^^^^^^^^

The `length=` field differs between mates because the reads are different
lengths. That is the normal case, not an edge case. `facts` stores the **whole
header**, so tokenization has to happen at compare time: mates agree on the
first whitespace-delimited token (`ERR17609896.1`) and need not agree on
anything after it.

### 2. The real IDs are SRA-normalized

No `/1`--`/2` suffix, no Casava ` 1:N:0:` field. Any rule keyed on stripping a
trailing `/1`, or on reading the mate bit out of a Casava field, validates
nothing against this project's actual files. The usable invariant is narrower:
**mates share an identical first token.**

### 3. Identical read IDs do not imply mates

    SRR39891651.fastq          'SRR39891651.1 1 length=2882'
    SRR39891651.trimmed.fastq  'SRR39891651.1 1 length=2882'

A file and its own trimmed derivative have byte-identical first read IDs. Read
ID equality is **necessary but not sufficient** for a pair: it cannot
distinguish "R1 and R2 of one run" from "a file and a copy or descendant of
itself". Pairing on ID agreement alone would silently pair a file to its own
derivative -- exactly the failure this issue exists to prevent, introduced by
the fix meant to prevent it.

### 4. Filtering moves the first record

    ERR16145610.fastq          'ERR16145610.1 00194bc7-... length=57'
    ERR16145610.trimmed.fastq  'ERR16145610.588 966a917f-... length=128'

Trimming dropped the first 587 reads. Mates filtered independently would
diverge the same way. Any rule that treats "first IDs differ" as proof of
non-mateship would unpair legitimate pairs.

## Design

### Read IDs confirm and veto; they never originate a pair

Finding 3 is the constraint that shapes everything. Read IDs cannot *propose* a
pairing, because ID agreement is equally true of duplicates and derivatives.
They can only rule on a candidate the filename already proposed.

The two symptoms in the TODO entry therefore need two different mechanisms:

| Symptom | Mechanism | Cost |
|---|---|---|
| `foo_fwd`/`foo_rev` never pair | Extend the token vocabulary | Pure string work |
| Names collide, unrelated files pair | Read-ID and layout checks | Two dict lookups |

### Three tiers, cheapest first

None of these opens a file.

1. **Layout veto** (`metadata.read_type`). `'single-end'` on either side means
   this file has no mate; refuse. Today a single-end `foo_1.fastq` can still be
   paired with an unrelated `foo_2.fastq`.
2. **Filename match** (`pairing.is_mate_of`, unchanged). Proposes the candidate.
3. **Read-ID ruling** (`facts.first_read_ids`). Confirms or vetoes it.

### Confirmation is strong; veto is deliberately weak

This is the decision most likely to be got wrong, because the symmetric design
-- "IDs match, pair; IDs differ, refuse" -- reads as obviously correct and would
regress finding 4.

- **Confirm** when the first whitespace tokens of the two ID lists intersect.
  Positive evidence of the same run and the same record.
- **Veto** only on *positive evidence of difference*: the leading structural
  field differs. For SRA-style IDs that is the text before the first `.`
  (`ERR17609896` vs `SRR39891651`); for Illumina-style, the text before the
  first `:` (the instrument). Both are stable across every read in a run, so an
  offset caused by filtering cannot trip them.
- **Inconclusive otherwise** -- fall through to the filename decision, which is
  today's behavior.

A veto that never fires on real data is the correct outcome here. The TODO
entry's worry is "two genuinely unrelated files", which means different
accessions or different instruments; that is precisely what the leading field
catches, and nothing else needs to.

### Absence is never a veto

Missing `first_read_ids` (pre-existing objects, non-FASTQ, a parse that hit a
malformed record) and missing `read_type` (every user upload -- the field only
exists for SRA downloads) both mean *inconclusive*, not *refuse*. This is what
keeps the filename fast path intact, satisfying the issue's first acceptance
criterion by construction rather than by care.

### `pairing.py` stays pure

`pairing.py` imports only `re` and is called from six sites including hot paths
in `pipeline_service`. It must not learn about `DataObject`, Motor, or the
filesystem.

The decision function therefore takes plain data:

```python
@dataclass(frozen=True)
class PairInput:
    name: str
    facts: Mapping[str, Any]
    metadata: Mapping[str, Any]

class Verdict(StrEnum):
    CONFIRMED = "confirmed"          # names match, read IDs agree
    NAME_ONLY = "name_only"          # names match, nothing to check against
    REJECTED_LAYOUT = "rejected_layout"
    REJECTED_READ_IDS = "rejected_read_ids"
    NO_MATCH = "no_match"

def verdict(a: PairInput, b: PairInput) -> Verdict: ...
```

`facts` and `metadata` are already plain `dict` fields on `DataObject`
(`object.py:190`, `object.py:192`), and both call sites already load full
objects, so this costs **zero additional queries**.

### Both call sites, one function

There are two, and the second is easy to miss:

- `queue/results._link_mate` -- ingest. Links, and owns the decision.
- `services/pipeline_service.suggest_mate` -- the launch dialog. Re-derives
  pairing by filename for pairs "whose ingest predates mate linking", with its
  own `is_mate_of` loop.

If validation lands only in `results.py`, the launch dialog keeps proposing the
pair ingest just rejected. Both call `verdict()`.

`suggest_mate` returning `None` on a veto leaves the dialog with no
auto-suggestion, and the user pairs manually through the existing
`POST /objects/{id}/mate` path. The manual override is untouched by this work,
which is the issue's fourth acceptance criterion.

### Vocabulary extension

Adds `_fwd`/`_rev` and `_forward`/`_reverse` as a third scheme alongside `R`
and `N`. The existing scheme guard already prevents `sample_R1` from pairing
with `sample_2`; a new scheme inherits that protection for free.

Bare `.1`/`.2` is **deliberately excluded**: `sample.1.fastq` is ambiguous with
chunk numbering, and a wrong pair there is exactly the silent failure this issue
is about.

This is the lowest-confidence part of the change and the only part with no
real-data evidence behind it -- no file in the database currently fails to pair
for this reason. It is included because the issue's problem statement names it,
and it is cheap and independently revertable.

## Two defects found while specifying this

Both are real, both are small, and neither is what the issue asked about.

### The comment justifying the SRA bypass is false

`queue/results.py:527` says the SRA path links mates directly because
`` `<acc>_1.fastq` is not a shape its R1/R2 convention detects ``. Verified
against the running code:

```
ERR17609896_1.fastq -> ('ERR17609896', 'R1', 'N')
is_mate_of: True
```

It detects it. `_1`/`_2` are in `_MATE_TOKENS`. This is not comment drift: the
tree at commit `1b60a77`, which *wrote* the comment, already contained the
tokens -- they were added twelve hours earlier by `493c649`. The comment was
wrong when written.

The bypass itself is still right (fasterq-dump's own labelling is more
authoritative than an inference), so only the stated reason changes.

### The SRA path never sets `read_number`

```python
await r1.set({DataObject.mate_object_id: r2.id})   # no read_number
```

`_link_mate` sets both together. This is why both real mates in the database
have `mate_object_id` set and `read_number: None`. `object.py:240` explains
nulls as "pairs predating this field"; for these objects that explanation is
wrong, and every paired run downloaded through the app has the same gap.

## Scope

In scope:

- `pairing.verdict()` and the pure helpers it needs.
- Both call sites consuming it.
- The `_fwd`/`_rev` vocabulary extension.
- The two defects above, as separate commits.
- A backfill for `read_number` on already-linked pairs.

Out of scope:

- Opening any file at pairing time, bounded or otherwise. The issue's scope
  section allows a first-record read; this design does not need one, and adding
  it would be strictly worse than reading a field that already holds the answer.
- A new pairing model, or persisting a confidence score on the object. The
  verdict is a decision, not a stored fact.
- Backfilling `first_read_ids` onto objects ingested before it existed. Absence
  already means inconclusive, so a backfill buys nothing but a migration.

## Testing

The issue requires coverage of "compressed inputs, unconventional filenames,
valid mates, collisions, and unrelated reads". Mapping each to a real case:

| Case | Fixture |
|---|---|
| Valid mates | The **real** `ERR17609896` headers, `length=150`/`length=149` verbatim -- the case a naive implementation fails |
| Collision | Two `_1`/`_2` files whose IDs have different leading fields |
| Unrelated reads | `read_type='single-end'` on a name-matching pair |
| Derivative trap | The **real** `SRR39891651` / `SRR39891651.trimmed` identical IDs |
| Filter offset | The **real** `ERR16145610.1` vs `ERR16145610.588` -- must **not** veto |
| Unconventional | `foo_fwd`/`foo_rev` |
| Compressed | A `.fastq.gz` ingested end to end, asserting `first_read_ids` lands |
| Fast path | Both signals absent -- behavior identical to today |

Using the literal strings from the database rather than invented ones is the
point. A hand-built fixture would have said `length=150` on both mates and the
suite would have been green on an implementation that rejects every real pair.

**The compressed case has no real-data backstop**: all 6 FASTQ objects in the
database are uncompressed, so `gzip` coverage must come from a deliberate
fixture or a real `.fastq.gz` ingest. This is the one gap where a unit test is
the only evidence available.

## Open questions

None blocking. Two judgement calls are recorded above rather than deferred: the
veto is weak by choice (finding 4), and `.1`/`.2` is excluded by choice.
