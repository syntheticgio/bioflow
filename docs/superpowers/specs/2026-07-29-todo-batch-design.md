# Three deferred TODOs: dirty-state warning, GC sampling, FASTA lengths

Date: 2026-07-29

Addresses three entries deferred during the object-role work, recorded in
`docs/TODO.md`:

- Warn before a role conversion discards in-progress metadata edits
- Sample GC content across the file instead of a prefix
- Extract per-sequence lengths for FASTA

The three are independent. They share no state and touch overlapping files only
in `AssemblyFacts.tsx`, where each adds a separate row. They can land in any
order, or separately.

## 1. Dirty-state warning before role conversion

### Problem

`SchemaMetadataEditor` keeps form state locally and its resync effect bails
while `dirty` (`SchemaMetadataEditor.tsx:45-54`). Converting a file mid-edit
would otherwise save the previous role's values against the new role's schema.
What shipped is a `key={obj.role ?? "none"}` remount in `DetailPanel.tsx:777`,
which discards the local state. That is correct but silent: a user who types
into the metadata form and then clicks Convert loses the typing with no
warning.

### Design

`SchemaMetadataEditor` gains an optional prop:

```ts
onDirtyChange?: (dirty: boolean) => void;
```

fired from a `useEffect` on `dirty`. The `dirty` state itself stays local --
the prop only mirrors it outward, so the resync effect is untouched and the
component behaves identically when the prop is omitted.

`DetailPanel` holds the mirrored flag:

```ts
const [metadataDirty, setMetadataDirty] = useState(false);
```

passes `onDirtyChange={setMetadataDirty}` to the editor and `dirty={metadataDirty}`
to `RoleConverter`. The `key={obj.role ?? "none"}` remount stays -- it is still
what makes the conversion safe. The warning stops it being silent; it does not
replace it.

`RoleConverter` gains a two-step confirm that engages only when dirty:

- **Not dirty**: single click converts, exactly as today. No new friction.
- **Dirty**: first click sets local `confirming` state. The helper text is
  replaced by a `.warn-box` reading "You have unsaved metadata edits.
  Converting will discard them." The button becomes "Convert anyway" and a
  Cancel button appears beside it. The second click mutates.
- `confirming` resets to false whenever `dirty` transitions to false, so a user
  who saves in another part of the panel is not left staring at a stale
  warning.

The class comment on `RoleConverter` currently reads "the change is cheap and
reversible -- so there is no confirmation step, which would be friction without
benefit." That reasoning holds for the clean case and is rewritten to scope
itself to it: the conversion is still cheap and reversible, but the discarded
edits are not.

### Verification

Manual, in the browser at localhost:5173, per `CLAUDE.md` -- there is no
headless component-testing setup in this repo and none is expected:

1. Open a FASTA object, type into a metadata field, click Convert. Expect the
   warning and a button that does not convert on first click.
2. Click Cancel. Expect the form state intact and the original button back.
3. Click Convert, then "Convert anyway". Expect the conversion to proceed as
   before.
4. Without editing anything, click Convert. Expect a single-click conversion,
   unchanged from today.

## 2. Strided GC sampling

### Problem

`sequence_stats.fasta_stats` caps at `max_bases=50_000_000` and reads
sequentially from byte 0 (`sequence_stats.py:159-172`). On a multi-GB reference
that means the reported `gc_content_percent` describes chr1, not the assembly.
GC content varies enough between chromosomes that the number misleads when
compared across references.

The cap is a deliberate performance guard and stays. The fix makes the sample
representative rather than larger.

### Design

`fasta_stats` splits by seekability. Total bytes read is unchanged in every
case, so the cost profile is identical -- it is the same budget spent across
the file instead of at the front.

**Plain files** (`Compression.NONE`) where `file_size` exceeds what the cap
would read: divide the file into N blocks and take an equal share of the budget
at each. N = 100, enough to cross every chromosome on a human reference while
keeping each block large enough that per-seek overhead stays negligible. At
each offset:

1. Seek to the offset.
2. Discard the remainder of the line landed in -- a mid-line seek otherwise
   starts counting from an arbitrary column, and could land inside a header.
3. Read lines until this block's share of the budget is consumed, skipping
   `>` header lines as the current code already does.

**Gzip and BGZF**: unchanged. Neither seeks cheaply, and BGZF block-seeking
would mean a pysam dependency in `sequence_stats` plus a third code path for
plain gzip, which still cannot seek. Compressed references keep the sequential
prefix read.

Both paths add a fact recording which method produced the number:

```
stats_sampling: "strided" | "prefix" | "complete"
```

`complete` means the file was smaller than the budget and every base was
counted -- the figure is exact, not an estimate. This value is emitted for
small files on both the plain and compressed paths.

`stats_sampled_bases` continues to be emitted unchanged in all cases.

### Frontend

`AssemblyFacts.tsx` reads `stats_sampling` and labels the GC row accordingly:

| Value | Label | Suffix |
|---|---|---|
| `complete` | "GC content" | none |
| `strided` | "GC content (sampled across file)" | "from N sampled" |
| `prefix` | "GC content (sampled)" | "from N sampled" |
| absent | "GC content (sampled)" | "from N sampled" |

The absent case covers objects ingested before this change, whose facts have no
`stats_sampling` key; they keep today's wording. The label stops being a
blanket caveat and starts describing what actually happened.

The existing comment in `AssemblyFacts.tsx:88-90` pointing at `docs/TODO.md` is
replaced.

### Tests

In `backend/tests/storage/test_sequence_stats.py`:

- A synthetic FASTA with GC deliberately skewed by region -- high-GC sequence
  at the front, low-GC at the back -- large enough to exceed a small injected
  `max_bases`. Assert the strided estimate lands near the true whole-file GC,
  and that a prefix read of the same file does not. This is the test that would
  fail today.
- `stats_sampling` is `"complete"` for a file under the budget, `"strided"` for
  a plain file over it, `"prefix"` for a gzip file over it.
- A gzip file over the budget still returns a plausible `gc_content_percent`
  and does not raise.
- Existing `fasta_stats` tests continue to pass unchanged.

## 3. Per-sequence FASTA lengths

### Problem

`_parse_fasta` collects sequence names only and counts bases in aggregate
(`parsers.py:416-463`), so there is no way to report the longest or shortest
sequence in an assembly. The reference detail panel wants that row; it was cut
from the initial implementation rather than adding parser work.

### Design

The existing loop already accumulates `total_bases`. Add a per-record counter
`current_len`, flushed when the next `>` is seen and again at EOF. Two
accumulators consume the flushed values:

- `sequence_lengths` -- a dict for the first `MAX_STORED_CONTIGS` (50) records,
  aligned with `sequence_names` so the UI can pair name to length.
- Running longest and shortest name+length across **every** record parsed, not
  only the stored 50. Bounding these to the first 50 would report the wrong
  longest contig for any assembly with more than 50 sequences, which is most of
  them.

Emitted as:

```
sequence_lengths: {name: length, ...}   # <= 50 entries
sequence_longest: {"name": str, "length": int}
sequence_shortest: {"name": str, "length": int}
```

### Truncation

`_parse_fasta` breaks out of the loop when an uncompressed file passes the
256 MB `exact_limit` (`parsers.py:441-443`). At that point the lengths are
partial in two ways: the record being read is cut mid-sequence, and every
later record was never seen.

The in-progress record is **dropped rather than flushed**, so no sequence is
ever reported at a truncated length. The facts carry:

```
sequence_lengths_partial: true
```

set only on the truncation path. `sequence_longest` and `sequence_shortest`
are still emitted -- they are the true extremes of the portion parsed -- but
the flag marks them as not final.

Note this differs from `sequence_count`, which on truncation is replaced by
`sequence_count_estimate`. Lengths are not extrapolated: there is no sound way
to estimate an unseen contig's length from a byte ratio.

### Frontend

`AssemblyFacts.tsx` adds a row to the existing `dl.kv`, after Total bases:

```
Longest    chr1 · 248.9 Mb
Shortest   chrM · 16.6 kb
```

reusing `formatBases`. The row is suppressed entirely when either fact is
absent. When `sequence_lengths_partial` is set, a faint note follows: "partial
-- file truncated during parsing".

`sequence_lengths` is stored for later use by the sequence list but is not
rendered in this change; adding a length to each of 50 wrapped contig chips
would crowd a list whose purpose is scanning for a name.

### Tests

In `backend/tests/storage/test_parsers.py`:

- Multi-record FASTA with known, differing lengths: assert `sequence_lengths`,
  `sequence_longest`, and `sequence_shortest` all match hand-computed values.
- A FASTA with more than `MAX_STORED_CONTIGS` records where the longest and the
  shortest both fall beyond the first 50: assert `sequence_lengths` holds 50
  entries while longest/shortest come from the later records. This is the test
  that distinguishes this design from the bounded-only alternative.
- Multi-line sequence records (wrapped FASTA) sum correctly across lines.
- The truncation path: assert `sequence_lengths_partial` is true and that no
  reported length corresponds to the record cut by the limit.
- Existing `_parse_fasta` tests continue to pass unchanged.

## FactsTable

`AssemblyFacts` renders only for objects with the reference role. A FASTA left
as reads falls through to the generic `FactsTable`, which dumps every parsed
key and maintains an explicit hidden-key list for internal flags
(`FactsTable.tsx:53-70`). The new facts need entries there or they render as
raw snake_case keys:

- Hidden as internal flags: `stats_sampling`, `sequence_lengths_partial`,
  `sequence_lengths`.
- Given labels: `sequence_longest` -> "Longest sequence", `sequence_shortest`
  -> "Shortest sequence", formatted as `name · length`.

## Out of scope

- BGZF block-seeking for compressed references (section 2). Revisit if
  compressed references become the common case.
- Rendering per-contig lengths in the sequence chip list (section 3).
- The related `user_touched` schema TODO, which is a separate entry in
  `docs/TODO.md` and already partly addressed by commits `c7068b8`..`8160e44`.

## Docs

Remove all three entries from `docs/TODO.md` once each lands.
