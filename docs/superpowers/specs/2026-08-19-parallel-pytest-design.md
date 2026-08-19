# Parallel pytest with per-run/per-worker database isolation

**Issue:** [#679](https://github.com/syntheticgio/bioflow/issues/679)
**Date:** 2026-08-19
**Status:** Approved design, pending implementation plan

## Problem

The backend suite (6,000+ items) runs sequentially. Parallelizing it with
pytest-xdist requires that no two test processes share mutable state — and
today they do: every `beanie_models` consumer (139 test files) targets the
single hardcoded `biopipe_test` database, truncating its collections on
module entry. Two additional constraints beyond the issue's own sketch:

1. **Multiple worktrees run tests concurrently.** Agents work in parallel
   worktrees, each independently running suites. Nothing any run does may
   affect another run — including two runs against the *same* Mongo (two
   agents both using the main stack via `make test`), which per-worker
   naming alone does not protect.
2. **Host memory is a real constraint.** Full-suite runs have been
   OOM-killed (exit 137) when multiple stacks and suites ran concurrently.
   Parallelism multiplies per-run memory by worker count, so worker counts
   are capped locally and memory-heavy tests can be forced sequential.

Decision made during design: keep real Mongo for tests that need it
(mongomock supports neither Beanie 2 nor replica-set transactions), isolate
it fully by naming, and separately audit-and-migrate the tests that only
*incidentally* touch Mongo to mocks ("migrate what's cheap").

## Requirements

Each requirement is testable and has a stable ID.

- **R1** — Two pytest sessions running concurrently against the same Mongo
  instance both pass with counts identical to their sequential baselines.
- **R2** — Within one session, xdist workers never read or write another
  worker's database.
- **R3** — A session that exits green leaves zero `biopipe_test_*`
  databases behind on the Mongo it used.
- **R4** — A session that fails leaves its databases behind, discoverable
  from the pytest header, for inspection.
- **R5** — Databases abandoned by failed runs are removed automatically by
  a later session's start, once older than the sweep threshold.
- **R6** — Bare `pytest` (no flags) still runs sequentially; parallelism is
  requested explicitly by wrappers or the caller.
- **R7** — Local wrapper parallelism defaults to 4 workers, overridable via
  `PYTEST_WORKERS`; CI uses `-n auto`.
- **R8** — Tests marked `heavy` run in a strictly sequential phase after
  the parallel phase, with no parallel workers alive alongside them.
- **R9** — No test writes to a shared `/data` path from more than one
  worker concurrently.
- **R10** — The suite passes at `-n 4` for three consecutive runs with
  counts identical to the sequential baseline before any default changes.

## Design

### 1. Database naming and the run token

`beanie_models` in `backend/tests/conftest.py` derives its database name:

```
biopipe_test_{run_token}_{worker_id}
```

- **`run_token`**: 8 random hex chars, generated once per pytest invocation
  in `pytest_configure` on the controller via
  `os.environ.setdefault("BIOPIPE_TEST_RUN_TOKEN", ...)`. xdist workers are
  subprocesses of the controller and inherit the environment, so no xdist
  plumbing API is needed. `setdefault` also lets an outer wrapper pin the
  token if it ever needs to. This token is what satisfies R1.
- **`worker_id`**: the `PYTEST_XDIST_WORKER` env var (`gw0`, `gw1`, ...),
  falling back to `main` when xdist is not in use. Satisfies R2.

The existing truncate-collections-on-module-entry behavior is unchanged:
within one worker, modules run sequentially and reuse that worker's
database, exactly as the whole suite reuses `biopipe_test` today. The
fixture's `scope="module"` / `loop_scope="module"` also needs no change —
each xdist worker is its own process with its own event loop.

`pytest_report_header` prints the run's database prefix so a failed run's
data is findable (R4).

### 2. Cleanup: drop on success, sweep the stale

- **Green session end**: the controller's `pytest_sessionfinish` (exit
  status 0) drops every database matching this run's
  `biopipe_test_{run_token}_` prefix (R3).
- **Failed sessions leave their databases** (R4). To stop accumulation,
  each run database gets a `_run_meta` document with a `started_at`
  timestamp at creation; session start sweeps any `biopipe_test_*` database
  whose marker is older than **2 hours** or absent (R5). Mongo records no
  database creation time, hence the marker. A run exceeding 2 hours could
  be swept by a concurrent neighbor — accepted risk; full-suite runtime is
  minutes. The legacy bare `biopipe_test` database is never swept.

### 3. Parallelism policy

- `pytest-xdist>=3.6` joins the `dev` extras in `backend/pyproject.toml`.
- **No `-n` in `addopts`** (R6). Wrappers pass it explicitly.
- **Local wrappers**: `-n ${PYTEST_WORKERS:-4}` (R7). Not `-n auto` — auto
  spawns one worker per core, and the exit-137 history says host memory is
  the binding constraint, especially with several worktree stacks up.
- **CI**: `-n auto` — runners are isolated and small.

### 4. Memory-heavy tests: `heavy` marker, two-phase run

A `heavy` marker registered in `pyproject.toml`. Wrappers run:

```
pytest -m "not heavy" -n $WORKERS ...
pytest -m heavy ...
```

The second phase runs only after the first completes, so a heavy test runs
with nothing else alive (R8). `xdist_group` pinning was considered and
rejected for this purpose: it serializes group members against each other
while other workers keep running, which is mutual exclusion, not memory
relief. The marker starts **empty**; PR 1's verification runs watch
per-worker RSS and only demonstrably heavy tests get marked. An empty
marker makes phase two a no-op.

### 5. Non-Mongo shared state

Audited during design; remediation lands in PR 1.

- **Redis**: fakeredis everywhere (verified — the only real-looking Redis
  URLs in tests are assertion literals in `test_node_provision.py`).
  Per-instance, no cross-worker state. No change.
- **`/data` (BIOINFO_HOME)**: some tests touch it (`reap_report_dirs` and
  friends). PR 1 audits for *writes* to shared paths; findings move to
  `tmp_path`, or where the real path is the contract, get pinned to one
  worker via `xdist_group` — the correct use of it (mutual exclusion) (R9).
- **Live/port-binding tests** (`tests/mcp/test_server_live.py`,
  `tests/integration/`): audited for fixed-port binds; same `xdist_group`
  remedy where found.
- **Hypothesis**: example database is xdist-safe by design; profiles
  unchanged.

### 6. Wrapper changes

- **Makefile**: `test`, `test-fast`, `test-queue` adopt the two-phase
  parallel invocation with `-n ${PYTEST_WORKERS:-4}`.
- **`backend/run-worktree-tests.sh`**: same flags, plus a `--cpus` limit on
  the test container so several agents' parallel runs cannot saturate the
  host. Its private-Mongo-per-run pattern stays; the naming scheme is
  belt-and-suspenders there.
- **CI** (`.github/workflows/build-check.yml`, backend-full-test job):
  `-n auto`.

### 7. Verification protocol (gates PR 1)

1. Sequential full run — baseline pass count.
2. Three consecutive `-n 4` runs — counts identical to baseline (R10).
3. Two simultaneous `-n 4` runs against the same Mongo — both green (R1).
4. `run-worktree-tests.sh` with `-n 4` from a worktree — green.
5. After green runs, `biopipe_test_*` database count on the stack Mongo is
   zero (R3).

### 8. Delivery: three PRs

1. **PR 1 — isolation + xdist**: dependency, conftest naming/cleanup/sweep,
   shared-state audit fixes, verification protocol. Parallelism opt-in only.
2. **PR 2 — wrappers**: Makefile, worktree script (+ CPU cap), CI `-n
   auto`, `heavy` marker population if profiling found candidates.
3. **PR 3 — cheap migration**: audit the 139 `beanie_models` consumers for
   files that never `save`/`insert`/`find` (they need `init_beanie` only to
   *construct* Documents); convert the mechanical ones to mocks or
   `model_construct`. Deliverable is the list first, then conversions,
   possibly split across small PRs.

## Rejected alternatives

- **Per-worker name only** (the issue's sketch): two concurrent runs
  against one Mongo both use `gw0`, `gw1`, ... and collide. Fails R1.
- **Ephemeral Mongo per run everywhere**: extends the worktree script's
  private-Mongo pattern to all runs. Strongest isolation but slows and
  complicates the common in-container run; naming alone already satisfies
  R1/R2.
- **Full mock migration**: mongomock supports neither Beanie 2 nor
  replica-set transactions; rewriting 139 files is high-risk. Superseded by
  the "migrate what's cheap" audit in PR 3.
