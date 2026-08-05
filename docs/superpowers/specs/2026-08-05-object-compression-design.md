# Object compression

Compress stored objects wherever the format allows, with one compressor, one
policy point, and no change to the content-addressed invariant.

Settles [#42](https://github.com/syntheticgio/bioflow/issues/42) for epic
[#41](https://github.com/syntheticgio/bioflow/issues/41).

## What the survey actually found

The epic's premise was measured on 2026-08-04: 117 objects, 38.7 GB, of which
31.2 GB was plain FASTQ, implying ~23 GB recoverable. **That is no longer the
state of the store.** Re-surveyed 2026-08-05 against the real `biopipe`
database:

```
49 objects, 45 blobs, 1.837 GB total
  fastq   none   n=5   0.929 GB   <- the compressible slice
  fasta   none   n=10  0.301 GB
  unknown none   n=12  0.253 GB
  gff     none   n=3   0.167 GB
  bam     bgzf   n=2   0.083 GB   (already compressed)
  gtf     none   n=2   0.053 GB
  fasta   gzip   n=1   0.045 GB
```

So roughly 1.4 GB is compressible today, saving ~1.2 GB — not 23 GB. The case
for this work is therefore **forward-looking, not reclamation**: every future
SRA download lands 6.3x smaller, and a single paired run in this store is
already 875 MB of plain FASTQ. The epic's headline number should be read as
"what the store looked like on one day", not as a standing claim.

Two incidental findings from the same survey, both out of scope here:

- **All 45 blobs are in `state: "missing"` while all 45 are present on disk
  with matching sizes**, stamped `2026-08-05T17:20:10Z` with `miss_count: 1` —
  despite `models/blob.py` documenting that two consecutive misses are required.
  Tracked separately; it is a reason not to rewrite stored bytes right now
  (see "Backfill").
- `/data/objects` holds 4647 files against 45 known blobs, so the great
  majority of files on disk are orphans from an earlier database.

## Measurements

`ERR17609896_1.fastq`, 437,782,866 bytes, real Illumina reads from the store.
20 cores. Warm page cache, so these are compute-bound numbers.

| compressor | wall | output | ratio | throughput |
|---|---|---|---|---|
| `gzip -1` | 2.3s | 82.0 MB | 5.34x | 188 MB/s |
| `gzip -6` | 18.2s | 69.5 MB | 6.30x | 24 MB/s |
| `pigz -1` | 0.1s | 82.1 MB | 5.33x | 3182 MB/s |
| `pigz -6` | 0.9s | 69.6 MB | 6.29x | 487 MB/s |
| `pigz -9` | 2.2s | 65.7 MB | 6.66x | 202 MB/s |
| **`bgzip -l6 -@20`** | **0.6s** | **69.7 MB** | **6.28x** | **747 MB/s** |
| stdlib `gzip` level 6 | 10.5s | 69.5 MB | 6.30x | 42 MB/s |

The epic assumed 3.5–4x. Real FASTQ from this store gets **6.3x**.

## Decisions

### 1. One compressor: bgzip, at level 6

BGZF is a spec-compliant gzip stream. Verified on a bgzip'd FASTQ from the
store: `gzip -t` passes, `zcat | cmp` is byte-identical to the original, and
Python's stdlib `gzip.open()` reads it. Every tool that reads `.fastq.gz`
therefore reads a bgzip'd `.fastq.gz` with no change.

That collapses the epic's per-format split. There is no reason to write plain
gzip for FASTQ and bgzip for FASTA/VCF when bgzip is *faster* than pigz
(0.6s vs 0.9s) at an indistinguishable ratio (6.28x vs 6.29x) and additionally
gives block-level seekability where `samtools faidx` and tabix need it. One
code path, no format branching, and the seekable case is free.

Level 6 over 1 or 9: `-6` is 18% smaller than `-1` for 0.3s more on 437 MB,
while `-9` buys a further 5.7% for 4x the time. The epic anticipated a "large
CPU difference" for `-6`; that is true single-threaded and disappears with
`-@`. Take the size.

**This supersedes [#43](https://github.com/syntheticgio/bioflow/issues/43)
(register pigz).** bgzip is already in the image and already deliberate —
`backend/Dockerfile:91` installs the `tabix` package "for bgzip, which is worth
having in the image even though no code path calls it." That comment stops
being true with this change and should be updated when it does.

pigz is *also* present in the image, but only transitively: `dpkg` shows it
pulled in as a dependency of `python3-cutadapt` -> `python3-xopen`, not
declared by us. Depending on an undeclared transitive package is exactly the
kind of thing that vanishes on an unrelated apt change, which is a second
reason to prefer bgzip.

Fallback when bgzip is absent: Python's stdlib `gzip` at level 6, `mtime=0`.
Measured 42 MB/s — slow but correct, and a missing bgzip must degrade rather
than fail an ingest. **One caveat: the stdlib writes plain gzip, not BGZF**, so
the fallback loses seekability. For FASTQ that is harmless. For FASTA and VCF,
where `.fai`/tabix depend on block structure, the fallback must **skip
compression entirely** rather than write an unseekable file that looks fine
until someone indexes it.

### 2. Digest identity: hash the compressed bytes, record the plaintext hash

The CAS key stays what it is today — the SHA-256 of the bytes actually stored —
so `sha256(file at objects/ab/abc...) == its name` continues to hold and the
blob verifier's full-verify mode needs no change. `Blob` gains an indexed
`content_sha256` holding the hash of the *uncompressed* stream, and
`find_present_blob` looks up dedup by that.

Both hashes come from a single streaming pass, so this costs no extra read:

```
read plaintext -> update hash A -> bgzip -> write -> update hash B
Blob.id             = B   (CAS key; invariant intact)
Blob.content_sha256 = A   (indexed; dedup lookup)
```

This makes dedup *better* than today rather than merely preserving it. Dedup is
live in this store — `SRR39891651.fastq` and `SRR39891651.trimmed.fastq`
currently share one blob — and hashing only the compressed bytes would break it
whenever two ingests used different compressors or levels (bgzip vs the stdlib
fallback produce different bytes for identical input). Keying dedup on the
plaintext hash makes it compressor-independent.

`UploadSession.client_sha256` keeps working unchanged: it is verified against
the assembled plaintext, which is exactly hash A.

Existing blobs need `content_sha256 = id` backfilled, which is correct by
construction because everything stored today is stored as-is.

**Rejected: hash the compressed bytes only.** Simplest — no schema change at
all — but it silently breaks dedup across compressor variants, and the sha256
the API reports back to a client stops being the hash of the file they sent.

**Rejected: hash the plaintext and store compressed (git's model).** Perfect
dedup and unchanged client semantics, but it breaks the CAS invariant: any
full-verify that rehashes a stored blob fails, so the verifier would have to
learn to decompress before hashing. Trading a load-bearing invariant for a
field we can just as easily add is the wrong direction.

### 3. Compression happens at ingest, not per-producer

One policy point covers upload, SRA download and pipeline output together.

The decisive argument is that **ingest already reads the whole file to hash
it**, so compression fuses into that existing pass and costs CPU only, not an
extra read. At 747 MB/s a 10 GB FASTQ costs ~13s of wall clock on a path the
user is watching — acceptable, and the reason the epic's "adds latency to the
interactive path" objection does not survive the measurement.

Per-producer was the alternative (as `fastp_runner` already does). It is faster
in the narrow case but has to be repeated for every runner added from now on,
and the epic already documents three separate ingest paths that each forgot.

This collapses [#44](https://github.com/syntheticgio/bioflow/issues/44) and
[#45](https://github.com/syntheticgio/bioflow/issues/45) into substantially one
change, though #44's SRA-specific concerns (progress reporting, cancellation,
the `EXTRACTION_FACTOR` disk precheck) remain real and separate.

### 4. What gets compressed: an allowlist, not a denylist

Driven off `detect.py`'s detected `FormatKind`/`Compression`, never off a
hand-maintained extension list.

**Compress:** `FASTQ`, `FASTA`, `VCF`, `SAM`, `GFF`, `GTF`, `BED`, `GFA`.

**Never compress, and the reason:**

| kind | why |
|---|---|
| anything with `Compression != NONE` | already compressed; never double-compress |
| `BAM`, `CRAM`, `BCF` | already block-compressed internally |
| `FAI` | samtools reads it as the index *beside* a FASTA; compressing breaks `faidx` |
| `TEXT` | small enough that it is not worth the risk to preview paths |
| `UNKNOWN` | if we cannot identify it we cannot reason about its access pattern |
| aligner indexes (`.mmi`, `.bt2`, `.ht2`, `.0123`, `.64`, `.pac`) | mmap'd or randomly read; compression defeats the access pattern they exist for |

An allowlist is what makes the aligner-index row safe without enumerating it.
Those files land as `UNKNOWN` (the survey's 12 `unknown` objects, 0.253 GB, are
largely index members), so "compress only kinds we positively recognize as
compressible text" excludes them by construction rather than by a list someone
has to remember to extend.

### 5. Naming: `DataObject.name` gains `.gz`; the stored path does not change

Blobs are stored **extensionless** at `objects/ab/<64-hex>` — there is no
filename in the CAS to change, which makes epic item 6 much smaller than it
reads.

What matters is `DataObject.name`, for two reasons:

- `_named_link` (`queue/pipeline_handlers.py:716`, and a twin in
  `queue/assembly_handlers.py:196`) symlinks the blob into the workdir as
  `in_<name>`. That symlink's name is how fastp, bwa and minimap2 decide
  whether to inflate. A blob compressed but still named `.fastq` is handed to
  a tool as plain FASTQ and fails.
- `GET /objects/{id}/download` serves the raw bytes with `filename=obj.name`.
  Updating the name keeps the download coherent for free — compressed bytes
  arrive named `.gz`.

The read paths downstream are already prepared: `detect.py` strips
`COMPRESSION_EXTENSIONS` before taking the extension, and `pipelines/pairing.py`
strips `.gz` before matching `_R1`/`_R2`. Both want a confirming test, not a
redesign.

**One risk to check per call site:** `_named_link` falls back to the bare
extensionless target when no name is supplied. A tool handed
`objects/ab/abc...` with no suffix has only magic bytes to go on, and not every
tool sniffs. Each call site that can reach that fallback with a compressible
input needs checking.

## Prerequisite: `_decompress_head` only decodes one gzip member

Adopting bgzip degrades format detection until this is fixed.

`detect()` on a bgzip'd FASTQ returns `magic_says=None`, dropping confidence
from `MAGIC` to `EXTENSION`. The cause is in `_decompress_head`
(`storage/detect.py:115`): `gzip.decompress(head)` raises `EOFError` on the
truncated final member, and the fallback
`zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(head)` decodes only the
**first** member. bgzip emits a tiny leading block:

```
BGZF members in the first 64 KiB (compressed, uncompressed):
  [(89, 66), (10852, 65228), (11141, 65234), (10547, 65242), ...]
```

66 bytes is a single FASTQ header line; `_sniff_text` needs four lines. So the
sniffer sees one line and gives up.

Looping the decompressobj over `unused_data` until the head is exhausted yields
395,354 bytes and sniffs `fastq` correctly — verified. That fix lands before or
with the switch to bgzip.

This is a **latent bug today**, not one this design introduces: any bgzip'd
file a user drags in already detects at `EXTENSION` confidence. BAM masks it,
because `BAM\x01` sits in the first four bytes of the first block and needs no
further data.

## Backfill: not now

Only ~1.2 GB is recoverable from the current store, and every blob is presently
mis-flagged `missing` while sitting on disk. Rewriting stored bytes — and
therefore digests — underneath that state is a bad combination for a gain that
small.

The policy applies to new ingests. Revisit if the store grows back toward the
2026-08-04 numbers, at which point an explicit user-triggered maintenance
action is the shape to build; an online migration that rewrites live blobs is
the highest-risk option and buys nothing a manual one does not.

## Revised sequencing for the epic

1. **Fix `_decompress_head`** for multi-member gzip. Small, independent,
   fixes a live bug, and unblocks the rest.
2. **Add the compression seam**: `Blob.content_sha256` + index, the two-hash
   streaming pass, the bgzip-with-stdlib-fallback helper, and the allowlist
   policy function. Dedup switches to `content_sha256`.
3. **Wire it into ingest**, covering upload, SRA download and pipeline output
   together, including `DataObject.name` gaining `.gz`.
4. **SRA-specific work** that survives from #44: a `ctx.progress` phase for
   compression, `ctx.check_cancel()` during it, and revisiting
   `EXTRACTION_FACTOR = 4.0` now that peak disk briefly holds both copies.

#43 (register pigz) is superseded and should be closed with a pointer here.

## Verification

Per CLAUDE.md, the suite is necessary but not sufficient — the suggestion-rules
episode is the precedent for checking against real objects rather than
hand-built fixtures.

- Round-trip a real FASTQ from the store through the seam and confirm
  `zcat | cmp` is byte-identical.
- Confirm `detect()` returns `kind=FASTQ, compression=BGZF, confidence=MAGIC`
  after the `_decompress_head` fix.
- Confirm dedup: ingest the same plaintext twice, once via bgzip and once via
  the stdlib fallback, and assert one blob results.
- Assert the fallback direction by patching the bgzip probe off — per CLAUDE.md
  the image ships tools as installed, so a test asserting bgzip is *available*
  passes whether or not the patch worked.
- Assert an aligner index and a `.fai` are left untouched.
- Confirm `pairing.py` still pairs `_R1`/`_R2` once names carry `.gz`.
- Run a real SRA download end to end on the worktree stack
  (`./ops/worktree-up.sh`, UI on 5273) and record before/after sizes. Use a
  fresh accession — `/data` is shared with the main stack.
