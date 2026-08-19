# Parallel Pytest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the backend pytest suite in parallel with pytest-xdist, with every run and every worker fully isolated at the database level so concurrent worktree/agent runs can never clash.

**Architecture:** Test databases are named `biopipe_test_{run_token}_{worker_id}` — the run token (8 hex chars, generated once per pytest invocation, inherited by xdist workers via the environment) isolates concurrent sessions sharing one Mongo; the worker id isolates xdist workers within a session. Green sessions drop their own databases at exit; failed sessions leave theirs for inspection and a 2-hour stale sweep at the next session start removes abandoned ones. Parallelism is never in `addopts` — wrappers (Makefile, worktree script, CI) pass `-n` explicitly and run `heavy`-marked tests in a strictly sequential second phase.

**Tech Stack:** pytest 8, pytest-xdist >= 3.6, pytest-asyncio, Beanie 2 / pymongo (AsyncMongoClient in fixtures, sync MongoClient in session hooks), Docker Compose stack (`api` container is where tests run).

**Spec:** `docs/superpowers/specs/2026-08-19-parallel-pytest-design.md`

## Global Constraints

- Database name scheme: `biopipe_test_{run_token}_{worker_id}`; legacy bare `biopipe_test` is never created and never swept.
- Stale sweep threshold: 7200 seconds; timestamps are `time.time()` floats in each DB's `_run_meta` collection.
- Bare `pytest` stays sequential — no `-n`/`--dist` in `addopts`, ever.
- Local wrapper worker count: `-n ${PYTEST_WORKERS:-4}`; CI uses `-n auto`.
- Any `xdist_group` pinning requires `--dist loadgroup` on the parallel invocation; all wrappers pass it.
- Backend tests run from the **main repo root** via `docker compose exec -T api pytest ...` (a worktree would silently test main's code; use `./backend/run-worktree-tests.sh` from worktrees).
- Conventional Commits; PR titles to changelog standard; PRs labeled `type:maintenance`, `area:backend`.
- Work branch for PR 1: `test/679-parallel-pytest` (already exists, holds the spec commit). PR 2 branch: `test/679-parallel-wrappers`.
- `pyproject.toml` dependency changes require rebuilding the api image: `docker compose up -d --build api` (from main repo root).
- Never name a helper function `test_*` in importable test modules — pytest collects imported `test_*` callables as tests.
- **Inside the api container, both `python` and `python3` are `/opt/medaka/env/bin/*`** — a tool venv that shadows the app interpreter on PATH and cannot import the app or its dev dependencies. Use `pytest` directly, or `/usr/local/bin/python3.12` by absolute path when an interpreter is genuinely needed. Never `python -m pytest`, never bare `python3 -c`.

---

## PR 1 — isolation + xdist (Tasks 1–7)

### Task 1: pytest-xdist dependency and `heavy` marker registration

**Files:**
- Modify: `backend/pyproject.toml` (dev extras ~line 39; markers ~line 60)

**Interfaces:**
- Produces: `pytest -n` available inside the api image; `heavy` marker registered.

- [ ] **Step 1: Add the dependency and marker**

In `[project.optional-dependencies] dev`, after the `pytest>=8.3` line, add:

```toml
    "pytest-xdist>=3.6",
```

In `[tool.pytest.ini_options] markers`, add:

```toml
    "heavy: memory-heavy; excluded from the parallel phase and run sequentially afterwards",
```

- [ ] **Step 2: Rebuild the api image and verify xdist is importable**

```bash
docker compose up -d --build api
```

```bash
docker compose exec -T api python -c "import xdist; print(xdist.__version__)"
```

Expected: a version >= 3.6 printed.

- [ ] **Step 3: Smoke-run a tiny parallel invocation**

```bash
docker compose exec -T api pytest tests/pipelines/test_node_types.py -n 2 -q
```

Expected: PASS (this module is pure-function; it proves xdist mechanics only).

- [ ] **Step 4: Commit**

```bash
git add backend/pyproject.toml
git commit -m "test(backend): add pytest-xdist dev dependency and register heavy marker"
```

### Task 2: isolation helpers module (pure functions, TDD)

**Files:**
- Create: `backend/tests/_mongo_isolation.py`
- Test: `backend/tests/test_mongo_isolation.py`

**Interfaces:**
- Produces (used by Tasks 3 and 4):
  - `ensure_run_token() -> str` — returns `os.environ["BIOPIPE_TEST_RUN_TOKEN"]`, setting it to 8 random hex chars if absent.
  - `run_prefix() -> str` — `f"biopipe_test_{token}_"`.
  - `worker_db_name() -> str` — `run_prefix() + os.environ.get("PYTEST_XDIST_WORKER", "main")`. (Deliberately NOT named `test_db_name` — pytest would collect an imported `test_*` callable as a test.)
  - `direct_mongo_url(base_url: str) -> str` — the URL munging currently inlined in `beanie_models` (mongo→127.0.0.1 host swap, strip `replicaSet`, add `directConnection=true`), moved verbatim. Do not "improve" it.
  - `stale_test_dbs(db_names, started_at_by_db, now, active_prefix) -> list[str]` — pure drop-decision function.
  - `STALE_AFTER_SECONDS = 7200`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_mongo_isolation.py`:

```python
"""The naming/sweep decision logic behind parallel-safe test databases.

Pure functions only -- the Mongo I/O that uses them lives in conftest.py and
is exercised by every DB-touching test in the suite.
"""

import re

from tests import _mongo_isolation as iso


class TestRunToken:
    def test_token_is_stable_within_a_process(self, monkeypatch):
        monkeypatch.delenv("BIOPIPE_TEST_RUN_TOKEN", raising=False)
        first = iso.ensure_run_token()
        assert iso.ensure_run_token() == first

    def test_token_respects_an_existing_env_value(self, monkeypatch):
        monkeypatch.setenv("BIOPIPE_TEST_RUN_TOKEN", "cafef00d")
        assert iso.ensure_run_token() == "cafef00d"

    def test_token_is_short_hex(self, monkeypatch):
        monkeypatch.delenv("BIOPIPE_TEST_RUN_TOKEN", raising=False)
        assert re.fullmatch(r"[0-9a-f]{8}", iso.ensure_run_token())


class TestDbNaming:
    def test_worker_db_name_uses_xdist_worker_id(self, monkeypatch):
        monkeypatch.setenv("BIOPIPE_TEST_RUN_TOKEN", "cafef00d")
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
        assert iso.worker_db_name() == "biopipe_test_cafef00d_gw3"

    def test_worker_db_name_without_xdist_uses_main(self, monkeypatch):
        monkeypatch.setenv("BIOPIPE_TEST_RUN_TOKEN", "cafef00d")
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        assert iso.worker_db_name() == "biopipe_test_cafef00d_main"

    def test_legacy_name_is_not_a_prefix_match(self, monkeypatch):
        # "biopipe_test" must never look like one of our run databases.
        monkeypatch.setenv("BIOPIPE_TEST_RUN_TOKEN", "cafef00d")
        assert not "biopipe_test".startswith(iso.run_prefix())


class TestDirectMongoUrl:
    def test_rewrites_compose_hostname(self):
        assert "127.0.0.1" in iso.direct_mongo_url("mongodb://mongo:27017")

    def test_strips_replica_set_and_forces_direct(self):
        url = iso.direct_mongo_url("mongodb://mongo:27017/?replicaSet=rs0")
        assert "replicaSet" not in url
        assert "directConnection=true" in url

    def test_leaves_direct_connection_alone_if_present(self):
        url = iso.direct_mongo_url("mongodb://h:27017/?directConnection=true")
        assert url.count("directConnection") == 1


class TestStaleSweep:
    NOW = 1_000_000.0

    def test_old_db_is_stale(self):
        names = ["biopipe_test_dead0000_gw0"]
        metas = {"biopipe_test_dead0000_gw0": self.NOW - 7201}
        assert iso.stale_test_dbs(names, metas, self.NOW, "biopipe_test_live0000_") == names

    def test_young_db_is_kept(self):
        names = ["biopipe_test_dead0000_gw0"]
        metas = {"biopipe_test_dead0000_gw0": self.NOW - 60}
        assert iso.stale_test_dbs(names, metas, self.NOW, "biopipe_test_live0000_") == []

    def test_db_without_meta_is_stale(self):
        names = ["biopipe_test_dead0000_gw0"]
        assert iso.stale_test_dbs(names, {}, self.NOW, "biopipe_test_live0000_") == names

    def test_own_run_is_never_swept_even_if_old(self):
        names = ["biopipe_test_live0000_gw0"]
        metas = {"biopipe_test_live0000_gw0": self.NOW - 99999}
        assert iso.stale_test_dbs(names, metas, self.NOW, "biopipe_test_live0000_") == []

    def test_legacy_and_unrelated_dbs_are_never_swept(self):
        names = ["biopipe_test", "biopipe", "admin", "local"]
        assert iso.stale_test_dbs(names, {}, self.NOW, "biopipe_test_live0000_") == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec -T api pytest tests/test_mongo_isolation.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'tests._mongo_isolation'`.

- [ ] **Step 3: Implement the module**

Create `backend/tests/_mongo_isolation.py`:

```python
"""Test-database isolation for parallel (xdist) and concurrent pytest runs.

Every pytest session gets a random run token; every xdist worker within it
gets its own database, `biopipe_test_{token}_{worker}`. The token is what
keeps two *sessions* sharing one Mongo apart (two agents both running the
suite against the main stack); the worker id keeps a session's own workers
apart. conftest.py owns the Mongo I/O; this module holds the decisions so
they can be tested without a database.
"""

import os
import re
import uuid

STALE_AFTER_SECONDS = 7200
_PREFIX = "biopipe_test_"


def ensure_run_token() -> str:
    """The session's token, minted on first ask, inherited by xdist workers.

    setdefault, not assignment: workers are subprocesses of the controller
    and arrive with the controller's token already in their environment.
    """
    return os.environ.setdefault("BIOPIPE_TEST_RUN_TOKEN", uuid.uuid4().hex[:8])


def run_prefix() -> str:
    return f"{_PREFIX}{ensure_run_token()}_"


def worker_db_name() -> str:
    return run_prefix() + os.environ.get("PYTEST_XDIST_WORKER", "main")


def direct_mongo_url(base_url: str) -> str:
    """Moved verbatim from the beanie_models fixture -- see its history."""
    url = base_url
    if "://mongo:" in url:
        url = url.replace("://mongo:", "://127.0.0.1:")
    url = re.sub(r"replicaSet=[^&]*&?", "", url)
    if "directConnection=" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}directConnection=true"
    return url


def stale_test_dbs(db_names, started_at_by_db, now, active_prefix):
    """Which run databases a session start may drop.

    Never the legacy bare `biopipe_test` (no trailing underscore, so the
    prefix check excludes it), never the active run's own, and otherwise
    only those whose `_run_meta` timestamp is missing or older than
    STALE_AFTER_SECONDS -- a failed run's data stays inspectable until then.
    """
    stale = []
    for name in db_names:
        if not name.startswith(_PREFIX):
            continue
        if name.startswith(active_prefix):
            continue
        started = started_at_by_db.get(name)
        if started is None or now - started > STALE_AFTER_SECONDS:
            stale.append(name)
    return stale
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec -T api pytest tests/test_mongo_isolation.py -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/_mongo_isolation.py backend/tests/test_mongo_isolation.py
git commit -m "test(backend): add per-run/per-worker test-database naming and sweep decisions"
```

### Task 3: wire conftest — token, header, fixture DB name, sweep, drop-on-green

**Files:**
- Modify: `backend/tests/conftest.py`

**Interfaces:**
- Consumes: everything Task 2 produced.
- Produces: `beanie_models` targeting `worker_db_name()`; session hooks other tasks rely on implicitly.

- [ ] **Step 1: Add hooks and rewire the fixture**

In `backend/tests/conftest.py`:

1. Add imports near the top (keep ruff's isort happy — `sys`, `time` are stdlib; `MongoClient` joins the existing pymongo import):

```python
import sys
import time

from pymongo import AsyncMongoClient, MongoClient

from tests._mongo_isolation import (
    direct_mongo_url,
    ensure_run_token,
    run_prefix,
    stale_test_dbs,
    worker_db_name,
)
```

2. Add hooks after the hypothesis profile block:

```python
def pytest_configure(config):
    # Mint the run token on the controller; xdist workers inherit it via env.
    ensure_run_token()


def pytest_report_header(config):
    # Where a failed run's data will be, for whoever inspects it.
    return f"biopipe test databases: {run_prefix()}*"


def _is_controller(config) -> bool:
    # xdist workers carry workerinput; the controller (and non-xdist runs) do not.
    return not hasattr(config, "workerinput")


def _sync_mongo_client() -> MongoClient:
    from app.config import settings as _settings

    return MongoClient(
        direct_mongo_url(_settings.mongo_url),
        tz_aware=True,
        serverSelectionTimeoutMS=2000,
    )


def pytest_sessionstart(session):
    """Sweep run databases abandoned by failed sessions older than 2h."""
    if not _is_controller(session.config):
        return
    try:
        client = _sync_mongo_client()
        names = client.list_database_names()
        metas = {}
        for name in names:
            if name.startswith("biopipe_test_"):
                doc = client[name]["_run_meta"].find_one()
                metas[name] = doc.get("started_at") if doc else None
        for name in stale_test_dbs(names, metas, time.time(), run_prefix()):
            client.drop_database(name)
        client.close()
    except Exception as exc:  # pure-function runs may have no Mongo at all
        print(f"biopipe test-db sweep skipped: {exc}", file=sys.stderr)


def pytest_sessionfinish(session, exitstatus):
    """Green runs leave no databases behind; failures keep theirs for inspection."""
    if not _is_controller(session.config) or exitstatus != 0:
        return
    try:
        client = _sync_mongo_client()
        for name in client.list_database_names():
            if name.startswith(run_prefix()):
                client.drop_database(name)
        client.close()
    except Exception as exc:
        print(f"biopipe test-db cleanup skipped: {exc}", file=sys.stderr)
```

3. In `beanie_models`, replace the inline URL munging and DB name:

The block

```python
    mongo_url = settings.mongo_url
    if "://mongo:" in mongo_url:
        mongo_url = mongo_url.replace("://mongo:", "://127.0.0.1:")
    import re
    mongo_url = re.sub(r"replicaSet=[^&]*&?", "", mongo_url)
    if "directConnection=" not in mongo_url:
        sep = "&" if "?" in mongo_url else "?"
        mongo_url += f"{sep}directConnection=true"
    client = AsyncMongoClient(mongo_url, tz_aware=True)
    db = client["biopipe_test"]
```

becomes

```python
    client = AsyncMongoClient(direct_mongo_url(settings.mongo_url), tz_aware=True)
    db = client[worker_db_name()]
```

4. After the existing truncate loop (`for coll_name in ...`), stamp the run meta the sweep reads:

```python
    await db["_run_meta"].replace_one({}, {"started_at": time.time()}, upsert=True)
```

5. Update the fixture docstring's mention of `biopipe_test` to say the database is per-run/per-worker (`biopipe_test_{token}_{worker}`) and why (concurrent sessions and xdist workers must not share one).

- [ ] **Step 2: Run a DB-touching subset sequentially**

```bash
docker compose exec -T api pytest tests/storage tests/db -q
```

Expected: PASS, and the header line shows `biopipe test databases: biopipe_test_<hex>_*`.

- [ ] **Step 3: Verify a green run leaves nothing behind**

```bash
docker compose exec -T mongo mongosh --quiet --eval "db.adminCommand('listDatabases').databases.map(d => d.name).filter(n => n.startsWith('biopipe_test_'))"
```

Expected: `[]`.

- [ ] **Step 4: Verify a failed run keeps its database**

```bash
cat > backend/tests/test_tmp_retention_check.py <<'EOF'
async def test_fails_on_purpose(beanie_models):
    assert False, "retention check"
EOF
docker compose exec -T api pytest tests/test_tmp_retention_check.py -q || true
docker compose exec -T mongo mongosh --quiet --eval "db.adminCommand('listDatabases').databases.map(d => d.name).filter(n => n.startsWith('biopipe_test_'))"
rm backend/tests/test_tmp_retention_check.py
```

Expected: exactly one `biopipe_test_<hex>_main` database listed after the failing run.

- [ ] **Step 5: Verify the sweep drops it once stale**

Backdate the leftover's marker, run any pytest, confirm it is gone:

```bash
docker compose exec -T mongo mongosh --quiet --eval "
  const names = db.adminCommand('listDatabases').databases.map(d => d.name).filter(n => n.startsWith('biopipe_test_'));
  names.forEach(n => db.getSiblingDB(n)._run_meta.updateOne({}, { \$set: { started_at: 1 } }, { upsert: true }));
  print(names)"
docker compose exec -T api pytest tests/test_mongo_isolation.py -q
docker compose exec -T mongo mongosh --quiet --eval "db.adminCommand('listDatabases').databases.map(d => d.name).filter(n => n.startsWith('biopipe_test_'))"
```

Expected: final listing is `[]`.

- [ ] **Step 6: Commit**

```bash
git add backend/tests/conftest.py
git commit -m "test(backend): isolate test databases per run and per xdist worker

Concurrent pytest sessions against one Mongo (two agents, two worktrees)
previously shared biopipe_test and truncated each other's data mid-test.
Each session now gets a random run token, each worker its own database;
green sessions drop their databases at exit, failed ones stay inspectable
until a 2-hour stale sweep at the next session start."
```

### Task 4: point the 22 self-connecting test files at the per-worker name

**Files:**
- Modify: every file from this list *except* `conftest.py` (done in Task 3):
  `backend/tests/storage/test_memory_model.py`, `test_object_role.py`, `test_sidecars.py`, `test_read_pairing.py`; `backend/tests/queue/test_annotation_ingest_triggers.py`, `test_record_outcomes.py`, `test_cancel_cleanup.py`, `test_verify_files_recheck.py`, `test_resource_override.py`, `test_tolerated_dependencies.py`, `test_verify_files_circuit_breaker.py`, `test_mate_link.py`; `backend/tests/db/test_index_reconcile.py`; `backend/tests/metadata/test_platform_migration.py`; `backend/tests/services/test_declared_align_mem.py`, `test_workflow_job_hook.py`, `test_metrics.py`, `test_workflow_multi_port.py`, `test_suggest_mate.py`, `test_workflow_derive.py`, `test_workflow_orchestrator.py`, `test_memory_estimate.py`

**Interfaces:**
- Consumes: `worker_db_name()` from Task 2.

These files open their own `AsyncMongoClient` and hardcode `client["biopipe_test"]` — under xdist (or two concurrent runs) they would collide exactly the way the shared fixture used to.

- [ ] **Step 1: Mechanical edit in each file**

In each listed file, replace

```python
    db = client["biopipe_test"]
```

with

```python
    db = client[worker_db_name()]
```

and add the import, grouped with the other first-party test imports:

```python
from tests._mongo_isolation import worker_db_name
```

Confirm zero code references remain (comments/docstrings may still mention the legacy name; leave prose that describes history, update prose that states the current name):

```bash
grep -rn '\["biopipe_test"\]' backend/tests --include="*.py"
```

Expected: no matches.

- [ ] **Step 2: Run the affected directories**

```bash
docker compose exec -T api pytest tests/storage tests/queue tests/db tests/metadata tests/services -q
```

Expected: PASS, counts matching a pre-change run of the same paths.

- [ ] **Step 3: Commit**

```bash
git add backend/tests
git commit -m "test(backend): route self-connecting test fixtures through the per-worker database name"
```

### Task 5: shared-state audit — /data writes and fixed ports

**Files:**
- Possibly modify: files found by the audit below (cannot be enumerated until run).

**Interfaces:**
- Consumes: nothing from earlier tasks; produces `xdist_group` marks that require `--dist loadgroup` (already a Global Constraint for all wrappers).

- [ ] **Step 1: Find tests that write to shared /data**

```bash
grep -rln "bioinfo_home\|/data" backend/tests --include="*.py" | \
  xargs grep -ln "mkdir\|write_text\|write_bytes\|open(\|shutil\|unlink\|rmtree" | sort
```

For each hit, read the test and classify:
- **Writes under `tmp_path`/`tmp_path_factory` only** → safe, no change.
- **Writes a shared /data path derived from settings** → prefer redirecting to `tmp_path` via monkeypatching the setting; where the real path IS the contract (e.g. reap logic asserting on the actual layout), pin the whole module's tests to one worker instead:

```python
pytestmark = pytest.mark.xdist_group("shared-data")
```

- [ ] **Step 2: Find fixed-port binds in live tests**

```bash
grep -rnE "bind|localhost:[0-9]+|127\.0\.0\.1:[0-9]+|port=[0-9]+" backend/tests/mcp backend/tests/integration --include="*.py"
```

Any test that binds a fixed TCP port gets `pytest.mark.xdist_group("<port-purpose>")` the same way (two workers binding one port is a hard failure, unlike /data which fails softly).

- [ ] **Step 3: Verify the audited paths still pass, including grouped, under xdist**

```bash
docker compose exec -T api pytest tests/mcp tests/integration <audited-paths> -n 4 --dist loadgroup -q
```

Expected: PASS.

- [ ] **Step 4: Commit (only if the audit changed anything)**

```bash
git add backend/tests
git commit -m "test(backend): serialize shared-/data and fixed-port tests under xdist"
```

### Task 6: verification protocol (gates the PR)

No file changes — this is the spec's R1/R10 evidence. Record all counts in the PR description.

**Outcome (2026-08-19).** Baseline 6,289 passed / 3 pre-existing failures in
140s; `-n 4` reproduced that count in ~53s (2.6x) across three runs. The
two-simultaneous-suites check earned its place in this protocol — it was the
only step that failed, and it failed on three separate defects a single
parallel run cannot expose:

1. The stale sweep dropped databases with no `_run_meta` marker, but an
   unstamped database is one being *born*. A concurrent run lost the race as
   `OperationFailure` out of `init_beanie`'s index build. Fixed by leaving
   unmarked databases alone and stamping the marker before anything else
   touches the database.
2. `test_object_deletion.py` drove the real `/data` report roots, where
   `reap_report_dirs` deletes any directory it has no database row for —
   including a concurrent run's live fixtures. The `xdist_group` mark from
   Task 5 only orders workers *within* one run, so it could not help. Fixed
   with private per-module roots.
3. `test_leaves_no_temp_file_behind` asserted on a fixed filename in a shared
   staging directory, reading a concurrent run's in-flight merge.

A fourth defect surfaced at the leftover-database check: drop-on-green keyed
on `exitstatus == 0`, so a suite carrying any long-standing red test never
cleaned up — 51 databases had accumulated. Re-keyed on `session.testsfailed`.

The three pre-existing failures (`test_config.py::TestAgentSettings::test_defaults`,
`test_sra_download.py::TestDiskPreflight::test_passes_when_there_is_room`,
`test_provenance_verbs.py::test_every_registered_handler_is_classified` —
filed as #692) are unrelated to this work and fail identically on a clean
tree; they are the reason "identical to baseline" rather than "zero failures"
is the bar here.

- [ ] **Step 1: Sequential baseline**

```bash
docker compose exec -T api pytest -q
```

Record the exact `N passed, M skipped` line.

- [ ] **Step 2: Three consecutive `-n 4` runs**

```bash
docker compose exec -T api pytest -n 4 --dist loadgroup -q
```

Run three times. Expected: counts identical to the baseline every time. Any test failing under xdist but not sequentially is a real isolation bug — use superpowers:systematic-debugging, do not retry until green.

- [ ] **Step 3: Two simultaneous sessions against the same Mongo**

```bash
docker compose exec -T api pytest -n 2 --dist loadgroup -q > /tmp/run_a.log 2>&1 &
docker compose exec -T api pytest -n 2 --dist loadgroup -q > /tmp/run_b.log 2>&1 &
wait
tail -2 /tmp/run_a.log /tmp/run_b.log
```

Expected: both green with baseline counts. (Two workers each, not four, to stay inside the host-memory envelope — the point is token isolation, not load.)

- [ ] **Step 4: Worktree-runner path**

```bash
./backend/run-worktree-tests.sh tests/ -q -n 4 --dist loadgroup
```

Expected: green with baseline counts (the `-n`/`--dist` pass straight through as pytest args; the script itself is untouched until PR 2).

- [ ] **Step 5: Leftover check**

```bash
docker compose exec -T mongo mongosh --quiet --eval "db.adminCommand('listDatabases').databases.map(d => d.name).filter(n => n.startsWith('biopipe_test_'))"
```

Expected: `[]`.

### Task 7: open and merge PR 1

Follow CLAUDE.md's merge workflow exactly (rebase on origin/main, survival diff check, push, `gh pr create`, poll `gh pr checks` to completion, `gh pr merge --rebase --delete-branch`).

- [ ] **Step 1: Rebase and verify survival**

```bash
git fetch origin main && git rebase origin/main && git diff origin/main...HEAD --stat
```

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin HEAD
gh pr create --base main \
  --title "test(backend): parallel-safe test databases per run and per xdist worker" \
  --body "$(cat <<'EOF'
Part of #679 (isolation half; wrapper/CI defaults follow in a second PR).

Why: concurrent pytest sessions -- two agents in two worktrees, or xdist
workers within one run -- shared the single hardcoded `biopipe_test`
database and truncated each other's data mid-test. Databases are now named
`biopipe_test_{run_token}_{worker_id}`; green sessions drop theirs at exit,
failed ones stay inspectable until a 2-hour sweep.

Verification (spec R1/R10): sequential baseline, 3x `-n 4` identical
counts, two simultaneous sessions against one Mongo both green, worktree
runner green, zero leftover databases. Counts below.

<paste Task 6 counts here>

Spec: docs/superpowers/specs/2026-08-19-parallel-pytest-design.md
EOF
)"
gh pr edit --add-label "type:maintenance" --add-label "area:backend"
```

- [ ] **Step 3: Poll checks until every one passes, fix reds, then merge**

```bash
gh pr checks <N> --watch
gh pr merge <N> --rebase --delete-branch
```

- [ ] **Step 4: Update issue #679 with progress** (isolation merged; wrappers next).

---

## PR 2 — wrapper and CI wiring (Tasks 8–12)

Start from a fresh branch off the merged main:

```bash
git fetch origin main && git checkout -b test/679-parallel-wrappers origin/main
```

### Task 8: Makefile parallel defaults with sequential heavy phase

**Files:**
- Modify: `Makefile` (targets `test`, `test-fast`, `test-queue`, ~lines 34–41)

**Interfaces:**
- Produces: `PYTEST_WORKERS` make/env variable, default 4.

- [ ] **Step 1: Rewrite the three targets**

Near the top (after `COMPOSE := docker compose`):

```make
PYTEST_WORKERS ?= 4
```

Replace the targets (`|| [ $$? -eq 5 ]` tolerates pytest's exit 5, "no tests collected", while the `heavy` marker is empty):

```make
test: ## Run the backend test suite (parallel, then heavy tests sequentially)
	$(COMPOSE) exec -T api pytest -m "not heavy" -n $(PYTEST_WORKERS) --dist loadgroup -v
	$(COMPOSE) exec -T api pytest -m heavy -v || [ $$? -eq 5 ]

test-fast: ## Run fast unit tests (skipping slow tests)
	$(COMPOSE) exec -T api pytest -m "not slow and not heavy" -n $(PYTEST_WORKERS) --dist loadgroup --tb=short
	$(COMPOSE) exec -T api pytest -m "heavy and not slow" --tb=short || [ $$? -eq 5 ]

test-queue: ## Run only the queue tests
	$(COMPOSE) exec -T api pytest tests/queue -m "not heavy" -n $(PYTEST_WORKERS) --dist loadgroup -v
	$(COMPOSE) exec -T api pytest tests/queue -m heavy -v || [ $$? -eq 5 ]
```

- [ ] **Step 2: Run each target**

```bash
make test-queue
make test-fast
make test
```

Expected: all green; the heavy phase reports "no tests ran" and the target still succeeds.

- [ ] **Step 3: Commit**

```bash
git add Makefile
git commit -m "test(backend): run make test targets in parallel with a sequential heavy phase"
```

### Task 9: run-worktree-tests.sh — parallel default, CPU cap

**Files:**
- Modify: `backend/run-worktree-tests.sh` (final `docker run` block, ~lines 244–257)

- [ ] **Step 1: Replace the final docker run with a two-phase function**

After the existing verbosity-defaulting block (`[ -n "$has_verbosity" ] || PYTEST_ARGS+=(-q)`), add:

```bash
# Parallel by default, sequential heavy phase after -- unless the caller is
# steering workers or markers themselves, in which case run exactly what
# they asked for (their -m could deliberately select heavy tests).
WORKERS="${PYTEST_WORKERS:-4}"
CALLER_CONTROLS=
for arg in "${PYTEST_ARGS[@]}"; do
  case "$arg" in
    -n | --numprocesses | --numprocesses=* | -m | --dist | --dist=*) CALLER_CONTROLS=1 ;;
  esac
done
```

Then replace the trailing `docker run ... python -m pytest "${PYTEST_ARGS[@]}"` with:

```bash
run_pytest() {
  docker run --rm \
    --network biopipe_default \
    --cpus "$WORKERS" \
    -v "$REPO_ROOT/backend/app:/srv/app" \
    -v "$REPO_ROOT/backend/tests:/srv/tests" \
    -v "$REPO_ROOT/VERSION:/VERSION:ro" \
    -v "$REPO_ROOT/docker-compose.override.yml:/docker-compose.override.yml:ro" \
    -v "$REPO_ROOT/backend/pi-skills:/backend/pi-skills:ro" \
    -v "$DATA_SOURCE:/data" \
    -w /srv \
    -e MONGO_URL="mongodb://$MONGO_NAME:27017/?replicaSet=rs0" \
    -e REDIS_URL="redis://redis:6379/0" \
    ${BIOFLOW_TEST_LIVE_DATA:+-e BIOFLOW_TEST_LIVE_DATA="$BIOFLOW_TEST_LIVE_DATA"} \
    "${SSHD_ENV[@]+"${SSHD_ENV[@]}"}" \
    "$IMAGE" python -m pytest "$@"
}

if [ -n "$CALLER_CONTROLS" ]; then
  run_pytest "${PYTEST_ARGS[@]}"
else
  run_pytest -m "not heavy" -n "$WORKERS" --dist loadgroup "${PYTEST_ARGS[@]}"
  run_pytest -m heavy "${PYTEST_ARGS[@]}" || [ $? -eq 5 ]
fi
```

(The mount/env lines are the existing ones moved into the function unchanged; the only new flag is `--cpus`.)

- [ ] **Step 2: Verify both paths from the main checkout**

```bash
./backend/run-worktree-tests.sh tests/models -q          # default: two-phase parallel
./backend/run-worktree-tests.sh tests/models -q -n 2     # caller-controlled: single phase
```

Expected: both green.

- [ ] **Step 3: Commit**

```bash
git add backend/run-worktree-tests.sh
git commit -m "test(backend): parallelize worktree test runs with a CPU cap and heavy-phase split"
```

### Task 10: CI parallelism

**Files:**
- Modify: `.github/workflows/build-check.yml` (the backend-full-test job's pytest step, ~line 359)

- [ ] **Step 1: Split the pytest step into the two phases**

Find the step running `pytest backend/tests/ -q --tb=no` and change its `run:` to:

```yaml
        run: |
          pytest backend/tests/ -q --tb=no -m "not heavy" -n auto --dist loadgroup
          pytest backend/tests/ -q --tb=no -m heavy || [ $? -eq 5 ]
```

(`-n auto` is right for CI: the runner is isolated and small. pytest-xdist is already installed there via the `dev` extras.)

- [ ] **Step 2: Commit** (verification is this PR's own CI run, watched in Task 12)

```bash
git add .github/workflows/build-check.yml
git commit -m "ci: run the backend suite in parallel with a sequential heavy phase"
```

### Task 11: memory observation and heavy-marker population

**Outcome (2026-08-19): nothing marked, and that is the answer.** Peak
api-container memory during a full `-n 8` run was **2.6 GiB against 12.4 GB
available** — no pressure, no swap, no exit 137. The `heavy` marker stays
empty and its phase collects nothing, exactly as the plan allows for.

The worker-count decision changed on measurement. The plan proposed 4 on the
assumption that memory was the binding constraint; the Docker VM turns out to
report 24 CPUs against 12.4 GB of RAM, so `-n auto` would size the run by the
resource that is *not* scarce, while 4 left most of the machine idle. Full
suite (6,299 tests): **4 → 56s, 8 → 39s, 12 → 36s, 16 → 35s.** 8 takes ~90% of
the available speedup, and past it every extra worker buys seconds while
multiplying what a second agent's concurrent run has to fit alongside.

**Files:**
- Possibly modify: test files found to spike memory (cannot be enumerated until observed).

- [ ] **Step 1: Watch container memory during a full parallel run**

In one terminal:

```bash
while true; do docker stats biopipe-api-1 --no-stream --format '{{.MemUsage}}'; sleep 5; done
```

In another: `make test`.

- [ ] **Step 2: Decide**

- **No sustained spike** (container stays well under host pressure, no swap growth, no exit 137): mark nothing. The heavy phase stays an empty no-op. Done.
- **A spike**: bisect by running test directories individually with `-n 4` while watching stats; when the offending module is found, mark the specific tests:

```python
@pytest.mark.heavy
```

then re-run `make test` and confirm the spike moved to the sequential phase.

- [ ] **Step 3: Commit (only if anything was marked)**

```bash
git add backend/tests
git commit -m "test(backend): mark memory-heavy tests for the sequential phase"
```

### Task 12: open and merge PR 2

- [ ] **Step 1: Rebase, survival check, push, PR**

```bash
git fetch origin main && git rebase origin/main && git diff origin/main...HEAD --stat
git push -u origin HEAD
gh pr create --base main \
  --title "test(backend): default make/worktree/CI test runs to parallel execution" \
  --body "$(cat <<'EOF'
Closes #679.

Why: PR <PR1-number> made concurrent and parallel runs safe; this flips the
defaults. make targets and run-worktree-tests.sh run at -n ${PYTEST_WORKERS:-4}
capped (host memory is the binding constraint -- see the exit-137 history),
CI at -n auto. Heavy-marked tests run in a strictly sequential second phase;
callers passing their own -n/-m/--dist get exactly what they asked for.

Spec: docs/superpowers/specs/2026-08-19-parallel-pytest-design.md
EOF
)"
gh pr edit --add-label "type:maintenance" --add-label "area:backend"
```

- [ ] **Step 2: Watch CI — this PR is itself the CI verification.** The parallel CI run must be green with the same counts the job reported before this PR. Poll `gh pr checks <N> --watch`, fix reds, then:

```bash
gh pr merge <N> --rebase --delete-branch
```

- [ ] **Step 3: Close out** — comment on #679 that both halves are merged; if a TODO.md entry references this, follow the CLAUDE.md close-out procedure.

---

## PR 3 — cheap-migration audit (Task 13)

### Task 13: identify beanie_models consumers that never touch the database

**Files:**
- Create: nothing in-repo; the deliverable is a filed GitHub issue with the candidate list.

The conversion work itself is deliberately NOT planned here — which files qualify and what each conversion looks like depends on the audit output, and pretending otherwise would produce placeholder tasks. The audit issue becomes the spec for that follow-up.

- [ ] **Step 1: Run the audit**

```bash
grep -rln "beanie_models" backend/tests --include="*.py" | while read -r f; do
  if ! grep -qE '\.(save|insert|insert_one|insert_many|find|find_one|find_many|get|delete|delete_many|replace|update|update_one|aggregate|count|distinct)\(' "$f"; then
    echo "$f"
  fi
done | sort
```

Files listed request `beanie_models` but never issue a DB verb — they need `init_beanie` only so Documents can be *constructed*, and are candidates for dropping the fixture (fastest check: delete the fixture argument and run the file; if it fails only on `CollectionWasNotInitialized`, it needs a construction-time shim, which is the follow-up's design question).

- [ ] **Step 2: Spot-check three candidates** by reading them — confirm the grep isn't fooled (e.g. DB verbs behind a helper). Adjust the list.

- [ ] **Step 3: File the issue**

```bash
gh issue create \
  --title "test(backend): migrate beanie_models consumers that never touch the database to mocks" \
  --label "area:backend" --label "type:maintenance" --label "status:specification document" --label "priority:low" \
  --body "$(cat <<'EOF'
Follow-up to #679 (spec: docs/superpowers/specs/2026-08-19-parallel-pytest-design.md,
PR 3). These test files request the real-Mongo `beanie_models` fixture but
never issue a database verb -- they need init_beanie only to construct
Documents. Converting them to a construction-only shim shrinks the DB-bound
test set and speeds both sequential and parallel runs.

Candidates (audited <date>, spot-checked):

<paste audited list here>

Design question for the spec: what replaces init_beanie for
construction-only tests (model_construct? a lightweight init against a
never-connected client?) -- must not reintroduce a shared resource.
EOF
)"
```

- [ ] **Step 4: Comment on #679** linking the new issue, completing the three-part delivery.
