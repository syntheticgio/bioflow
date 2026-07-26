# TODO

Deferred work, with enough context to pick up cold. Newest first.

## Warn before a role conversion discards in-progress metadata edits

Raised: 2026-07-26, during the object-role implementation.

`SchemaMetadataEditor` keeps local form state and its resync effect bails while
`dirty`, so converting a file mid-edit would otherwise save the previous role's
values against the new role's schema. The fix shipped is a
`key={obj.role ?? "none"}` remount in `DetailPanel.tsx`, which discards that
local state — correct, but **silent**: a user who types into the metadata form
and then clicks Convert loses the typing with no warning.

The honest fix is a dirty-state confirmation in `RoleConverter` before
mutating, which needs `SchemaMetadataEditor` to expose its dirty flag (lift it
to the parent, or accept an `onDirtyChange` callback). Deferred because it
means re-architecting a component that otherwise works, for an edge case that
requires an unsaved edit plus a conversion in the same visit.

Touches: `frontend/src/components/SchemaMetadataEditor.tsx`,
`frontend/src/components/RoleConverter.tsx`,
`frontend/src/components/DetailPanel.tsx`.

## Sample GC content across the file instead of a prefix

Raised: 2026-07-26, during the object-role design.

`sequence_stats.fasta_stats` caps at `max_bases=50_000_000` and reads from the
start of the file. On a multi-GB reference that means the reported
`gc_content_percent` describes chr1, not the assembly — and GC content varies
enough between chromosomes that the number is misleading when compared across
references.

The cap itself is a deliberate performance guard and should stay. The fix is to
make the sample representative rather than larger: read strided blocks across
the file (seek to N offsets, take a chunk at each, skip partial lines) and
aggregate. Same cost, far better estimate.

Blocked on nothing. Until it lands, the reference detail panel labels the row
"GC content (sampled)" and shows `stats_sampled_bases`, so the figure is not
presented as genome-wide.

Touches: `backend/app/storage/sequence_stats.py`,
`backend/tests/storage/test_sequence_stats.py`. Once fixed, revisit the
"(sampled)" label in the Assembly section of `DetailPanel.tsx`.

## Extract per-sequence lengths for FASTA

Raised: 2026-07-26, during the object-role design.

`_parse_fasta` collects sequence *names* only, and `fasta_stats` counts bases in
aggregate, so there is no way to report the longest or shortest sequence in an
assembly. The reference detail panel wants a longest/shortest row; it was cut
from the initial implementation rather than adding parser work.

Fix: accumulate per-sequence base counts in the `_parse_fasta` loop and store
them bounded, mirroring how `reference_lengths` is already capped at
`MAX_STORED_CONTIGS` for BAM headers. Note the existing 256 MB exact-count
limit — when parsing truncates, the lengths are partial and must be flagged
as such rather than reported as final.

Touches: `backend/app/storage/parsers.py`,
`backend/tests/storage/test_parsers.py`, then add the row to the Assembly
section of `frontend/src/components/DetailPanel.tsx`.
