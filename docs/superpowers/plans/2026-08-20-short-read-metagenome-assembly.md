# Short-read metagenome assembly — implementation plan

Date: 2026-08-20.

Closes [#731](https://github.com/syntheticgio/bioflow/issues/731). Companion to
`docs/superpowers/specs/2026-08-20-short-read-metagenome-assembly-design.md`
(decisions N1–N3, requirements R1–R6).

Two parts, and the second may produce no code at all: **ship SPAdes `--meta`**,
then **measure it against MEGAHIT and write down the decision**.

## Part 1 — SPAdes `--meta` (small)

### Spike

- **S-1. Does the installed SPAdes accept `--meta` with the paired invocation**
  the runner builds (`-1`/`-2`)? metaSPAdes historically required paired input
  and **rejected single-end**. If that holds, the mode must be unavailable (or
  explained) for unpaired read sets rather than failing several minutes into a
  job.
- **S-2. Does `--meta` change output filenames?** `SPADES_SPEC`'s own comment
  records that its outputs were "confirmed against a real 4.3.0 run of the
  bundled test dataset, not read from documentation" — do the same here rather
  than assuming `contigs.fasta` persists.
- **S-3. Does `-m <GB>` bind under `--meta`?** metaSPAdes' behaviour under a
  memory cap is exactly what MEGAHIT is being compared on, so it matters
  whether the cap is honoured or advisory.

### Files

| File | Change |
|---|---|
| `backend/app/pipelines/assembler_registry.py` | A fourth `Choice(value="meta", ...)` in `SPADES_SPEC`'s `mode` field. |
| `backend/app/pipelines/assembly_runner.py` | `_spades_command`: `elif params.mode == "meta": cmd.append("--meta")`. |
| `backend/app/queue/results.py` | `assembly_meta_mode` fact (R4), matching #727 so #728's card gates the same way regardless of assembler. |
| `backend/tests/pipelines/test_assembly_runner.py` | Command-shape tests. |

### Steps

1. **`meta` as a fourth `mode` choice** (N1). The select is already
   mutually exclusive, and SPAdes' `--meta` genuinely conflicts with
   `--isolate` / `--careful` — so the exclusivity the field already enforces
   is exactly the exclusivity the tool wants.
   **Note the contrast with #727 deliberately:** Flye's `--meta` is
   *orthogonal* to its accuracy mode and so had to be a checkbox. Implementing
   these two the same way would be wrong in one direction or the other.
2. **The command builder.** Tests:
   - `meta` → `--meta` present, and **neither** `--isolate` nor `--careful`
     (the tool rejects those combinations, so a builder emitting both fails
     late and unhelpfully);
   - each existing mode → **full-argv equality** with today's output. Not a
     `"--meta" not in argv` check: this edits a builder every existing SPAdes
     run goes through, and an exact assertion also catches a reordering or a
     dropped flag.
3. **`assembly_meta_mode`** (R4), per-key `facts.<key>` merge (#606).
4. **S-1's consequence.** If metaSPAdes rejects single-end, the dialog must say
   so for an unpaired read set rather than offering a mode that will fail.

## Part 2 — the MEGAHIT decision (measurement, then a written answer)

This is the actual deliverable of #731, and it may end in no new code.

1. **Run both on one real short-read community sample.** Record, per the
   issue's own request and regardless of outcome (R5):
   - assembly size, contig N50 — the quality columns;
   - wall time;
   - **peak RSS — the column that decides it.**
2. **Apply N2's stated criteria**, written before the numbers so the decision
   is not post-hoc:
   - **Memory justifies MEGAHIT.** The registry already models SPAdes at 90
     bytes per genome base plus 0.6 per read base, the heaviest of the three
     assemblers here, and MEGAHIT exists to assemble large communities in
     bounded memory. On the local single-machine workloads this app targets,
     an assembler that finishes where the other OOMs is the difference between
     having the capability and not.
   - **A few percent of N50 does not.** Both are legitimate assemblers, and a
     modest quality edge does not pay for six hand-maintained registries, four
     of which fail silently (CLAUDE.md).
3. **Write the decision on the issue with its reason** (R6), and close it —
   either "SPAdes `--meta` is sufficient" or "MEGAHIT is justified", in which
   case cut a normal tool-addition issue citing the measurements.
4. **If MEGAHIT is added** (N3): its own `Assembler` and `AssemblerSpec` — a
   different binary, different outputs (`final.contigs.fa`), no
   `--careful`-style modes. Its `bytes_per_genome_base` must be **measured, not
   copied from SPAdes**: bounded memory is the entire claim being tested, and
   inheriting SPAdes' 90 would model away the reason for adding it.

## Verification

```bash
./backend/run-worktree-tests.sh tests/ -q
```

From the worktree, never `docker compose exec api`. Then `ruff check --config
backend/pyproject.toml backend/app backend/tests ops e2e`.

## Out of scope

Per the spec: adding MEGAHIT within this issue, short-read binning (#728 works
on contigs from any assembler), and hybrid long+short metagenome assembly.
