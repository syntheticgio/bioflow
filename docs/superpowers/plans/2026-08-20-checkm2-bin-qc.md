# CheckM2 bin QC — implementation plan

Date: 2026-08-20.

Closes [#729](https://github.com/syntheticgio/bioflow/issues/729). Companion to
`docs/superpowers/specs/2026-08-20-checkm2-bin-qc-design.md` (decisions Q1–Q4,
requirements R1–R6).

**Depends on [#728](https://github.com/syntheticgio/bioflow/issues/728)** —
needs bins to score.

The tool is ordinary. **The database is the work**, and `kraken_db_registry.py`
plus `kraken_handlers.download_kraken_db` are a working implementation of
every constraint it has — read both before writing anything.

## Spike first

- **S-1. CheckM2 on bioconda for `linux-aarch64`?** CheckM2 is Python with a
  **DIAMOND** dependency, and DIAMOND is the part most likely to be x86-64
  only. Check that subdir specifically; check apt with a control package in the
  same run before believing "not packaged".
- **S-2. The database's pinned Zenodo record, byte size, and checksum.** Q1
  requires a dated record, never a `latest` alias.
- **S-3. Peak RSS with the database loaded** — Q1 forbids fitting `mem_mb` from
  the memory model, so this must be measured.
- **S-4. Output table columns** on the installed version, for the parser.
  Capture a real table as a fixture.
- **S-5. `CHECKM2DB` env var vs `--database_path` flag** — which the installed
  version honours.

## Files to touch

| File | Change |
|---|---|
| `backend/scripts/install-checkm2.sh` + `backend/Dockerfile` | Per S-1. Own-venv pattern (`install-medaka.sh`) if pip-based. End with a **real run**, not `--version`. |
| `backend/app/config.py` | `checkm2_path`, `checkm2_db_dir`. |
| `backend/app/pipelines/checkm2_db_registry.py` | **New.** A `CheckM2DbSpec` mirroring `KrakenDbSpec` (`key`, `label`, `url`, `download_bytes`, `mem_mb`, `md5`, `description`) and a `db_present(key)` checking the **actual files**, not the directory. One entry — CheckM2 has one database. |
| `backend/app/pipelines/tools.py` | `checkm2()` probe + `all_tools()` + `cache_clear`; `TOOL_META["checkm2"]`. |
| `backend/app/pipelines/checkm2_runner.py` | **New, pure.** `build_predict_command(...)`, `parse_quality_report(...)` per S-4. |
| `backend/app/queue/checkm2_handlers.py` | **New.** `download_checkm2_db` (modelled on `download_kraken_db`) + the QC handler. |
| `backend/app/queue/results.py` | Applier merging scores onto each bin. |
| `backend/app/services/pipeline_service.py` | `launch_bin_qc(...)`, chaining the download when absent (Q2). |
| `backend/app/api/v1/pipelines.py` | `POST /pipelines/bin-qc` + report endpoint. |
| `backend/app/services/suggestion_service.py` | `build_bin_qc_card`. |
| `running_now.py`, `provenance_walker.py`, `node_types.py` | The usual four registries — **four of the six fail silently** (CLAUDE.md). |
| `frontend/src/lib/metricInfo.ts` | Entries for the new stats, with the threshold conventions (Q4). |

## Ordered steps

1. **The database registry** (Q1), copying `kraken_db_registry.py`'s shape and
   its reasoning:
   - **pinned dated record**, never `latest`. Write the test that asserts the
     URL contains no `latest` — cheap, and it catches the exact regression the
     pin exists to prevent;
   - `mem_mb` from S-3's **measurement**, not from the memory model. A fit from
     unrelated jobs under-provisions into an OOM, which also poisons the timing
     records;
   - `db_present` checks the **specific files** CheckM2 needs. A bare directory
     with files missing must read as **absent** — the download extracts to
     `.partial` and renames, so a half-populated directory means a bug, not an
     in-flight download.
2. **The download handler**, modelled on `download_kraken_db`: stream with
   urllib (nothing else here streams HTTP to disk), verify the checksum
   **before** extracting, extract to `.partial`, rename only on success. **No
   applier** — it fetches shared reference data, not something derived from an
   object.
   **Do not use `checkm2 database --download`** even though it exists: it
   resolves its own URL at runtime, which is the moving target the pin exists
   to prevent, and it puts integrity outside this repo's control.
3. **Command builder + table parser**, pure, against S-4's captured fixture.
   **The parser must not clamp contamination to 100** (R6). Values above 100%
   are real and mean several organisms merged into one bin — clamping hides the
   worst bins by making them look mediocre. Test a 137% row survives as 137.
4. **The QC handler** (Q3) — **one job over the directory of bins**, not one
   per bin. CheckM2's fixed cost is loading the DIAMOND database and it already
   takes a directory; forty jobs would pay that cost forty times and put forty
   queue entries behind one click.
5. **Chaining** (Q2/R2). When the database is absent, enqueue the download and
   make the QC job depend on it — the `launch_classify_reads` posture, not a
   refusal. A missing database is not a missing *decision*: there is one
   database and the user does not choose it, so refusing would be busywork.
   Surface the download as a **phase** so a multi-gigabyte fetch does not look
   like a hang. `queue.py`'s `_failed_dependencies` already fails a job whose
   dependency failed rather than blocking forever.
   Test both directions: absent → download enqueued and depended on; present →
   no download enqueued.
6. **Applier** — scores onto each bin by per-key `facts.<key>` path, never a
   whole-dict merge (#606).
7. **Registries + whole test classes.** Partitions, so a half-fix passes one
   and fails its sibling (#355).
8. **Frontend** — render completeness and contamination per bin. **No
   filtering, hiding, or auto-discard** (Q4/R5): a 40%-complete bin is a
   legitimate result for a low-abundance organism, and dropping it destroys the
   finding that the organism is present. Render the conventional tiers as a
   *label* beside the numbers, and put the thresholds (high-quality ≥90/≤5,
   medium ≥50/≤10) in the InfoMarker — that is the interpretive knowledge the
   numbers need.
9. **Real-data check.** Score real bins from a #728 run. Confirm the numbers
   are plausible against bin sizes, and that a **deliberately merged bin scores
   high contamination** — that is the falsifiable prediction, and the one that
   shows the database and parser are actually wired up rather than emitting
   plausible defaults.

## Verification

```bash
./backend/run-worktree-tests.sh tests/ -q
```

From the worktree, never `docker compose exec api`. Restart the worker after
handler edits. Then `ruff check --config backend/pyproject.toml backend/app
backend/tests ops e2e`, fixing pre-existing findings too.

## Out of scope

Per the spec: auto-filtering by quality, GTDB-Tk placement, automatic
re-binning, and CheckM1.
