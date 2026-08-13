# BioFlow End-to-End Test Harness — Implementation Plan

> **For Hermes:** Use `subagent-driven-development` skill to implement this plan task-by-task.

**Goal:** Build a Hermes desktop-app plugin (full page + sidebar nav) with a Python backend, developed in a new `e2e/` subfolder, that authors, runs, and reports end-to-end tests of BioFlow driven through its MCP server (control plane) + HTTP API (data upload).

**Architecture:** A desktop ESM plugin (`plugin.js`) renders the dashboard and calls its own backend namespace via `ctx.rest`. The backend (`plugin_api.py`, a FastAPI `APIRouter` mounted under `/api/plugins/bioflow-e2e/`) loads test definitions, executes them against a running BioFlow using a raw-JSON-RPC MCP client and an httpx upload client, and persists per-step results to SQLite.

**Tech Stack:** Python 3.12 (FastAPI `APIRouter`, httpx, stdlib `sqlite3`, PyYAML), plain-JS ESM desktop plugin (`@hermes/plugin-sdk`, `react/jsx-runtime`), Bash install script.

**Spec:** `docs/superpowers/specs/2026-08-13-bioflow-e2e-test-harness-design.md` (ET-1 … ET-30). **Issue:** #373.

---

## 0. Pinned integration facts (do not re-derive — verified against source)

These are the hard-to-guess details. The implementer must NOT rediscover them by trial; they are verified below.

### 0.1 BioFlow MCP server (control plane)

- Mounted at **`/api/v1/mcp`** on the BioFlow API (default `http://localhost:8000`). Streamable HTTP, raw JSON-RPC.
- **Profile selection:** the `?profile=` query param, resolved by `backend/app/mcp/context.py::owner_for`. Value is a **profile id** (Mongo ObjectId string) or `"local"`. On a single-profile install the param may be omitted (falls back to the sole profile).
- **Tool names are prefixed `bioflow_`:** `bioflow_whoami`, `bioflow_create_project`, `bioflow_list_projects`, `bioflow_get_project`, `bioflow_list_objects`, `bioflow_get_object`, `bioflow_suggest_next`, `bioflow_run_pipeline`, `bioflow_get_job`, `bioflow_list_jobs`, `bioflow_cancel_job`, `bioflow_search_objects`, `bioflow_search_ncbi`, `bioflow_download_reference`, `bioflow_list_tools`, `bioflow_get_guide`.
- **Protocol sequence** (proven in `backend/tests/mcp/test_server_live.py`):
  1. `POST {base}/api/v1/mcp?profile=<id>` — `initialize` (JSON-RPC 2.0, `protocolVersion: "2024-11-05"`), read the `mcp-session-id` response header.
  2. `POST` — `notifications/initialized` (same `mcp-session-id` header) → expect 202.
  3. `POST` — `tools/call` with `{"name": "bioflow_<tool>", "arguments": {...}}`.
  4. `tools/call` returns `result.content[0].text` = a **JSON string** wrapping the tool's actual return value; `json.loads` it.
- `run_pipeline(kind, params)` validates `kind` against `all_handlers()`; on an unknown kind the error message lists all valid kinds. `download_reference(accession, project_id)` downloads an assembly genome as an async job; poll with `bioflow_get_job`.

### 0.2 BioFlow HTTP API (data upload only)

- Upload endpoint: **`POST /api/v1/projects/{project_id}/objects/upload`** (`backend/app/api/v1/projects.py:134`).
  - Body = **raw file bytes** (not multipart).
  - Filename in **`X-Filename`** header, **percent-encoded** (the API `unquote`s it).
  - Owner in **`X-BioFlow-Profile`** header (profile id) — `backend/app/api/deps.py::get_current_owner` (`OwnerDep`). Omit on a single-profile install.
  - Returns `201` + the created `ObjectOut`.
  - Capped at `MAX_SIMPLE_UPLOAD_BYTES` — fixtures must be small.

### 0.3 Hermes desktop plugin + backend contract

- **Frontend:** `~/.hermes/desktop-plugins/<id>/plugin.js` — single ESM file, no build. Only importable specifiers: `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`. Write UI with `jsx()`/`jsxs()` (NO JSX syntax). Full page = `area: ROUTES_AREA` with `data: { path }`; reachability = `area: SIDEBAR_NAV_AREA` with `data: { path, label, codicon }`.
- **Backend:** `~/.hermes/plugins/<id>/dashboard/manifest.json` = `{ "name": "<id>", "api": "plugin_api.py" }`, and `plugin_api.py` exports **`router = APIRouter()`**. Routes mount under `/api/plugins/<id>/`.
- **Calling the backend:** `ctx.rest('/path', { method, body })` → `/api/plugins/<id>/path`. `PluginRestOptions = { method?, body?, upload?, timeoutMs? }`.
- **Data layer:** `useQuery` / `useMutation` / `queryClient` from `@hermes/plugin-sdk` (the app's single React Query client) — never hand-roll a poll loop.
- **Gating:** `plugin_api.py` is imported only when the plugin id is in `plugins.enabled` in `config.yaml` (and not in `plugins.disabled`). The in-app Settings→Plugins toggle is renderer-side only.

---

## 1. Config

File `e2e/backend/config.py` loads `config.json` from the backend data dir (default `~/.hermes/plugins/bioflow-e2e/data/config.json`), overridable by env vars.

| Key | Env override | Default | Meaning |
|---|---|---|---|
| `base_url` | `BIOFLOW_BASE_URL` | `http://localhost:8000` | BioFlow API origin |
| `profile` | `BIOFLOW_PROFILE` | `""` | profile id for MCP `?profile=` + HTTP `X-BioFlow-Profile`; empty = single-profile fallback |
| `cleanup` | — | `false` | whether a run deletes its throwaway projects after finishing |

## 2. Backend API protocol (mounted under `/api/plugins/bioflow-e2e/`)

| Method | Path | Purpose | Response |
|---|---|---|---|
| GET | `/tests` | list discovered tests | `[{name, kind: yaml\|python, description}]` |
| POST | `/runs` | start a run | `{run_id}` (body `{tests?: ["name", ...]}`; omit = all) |
| GET | `/runs` | list run history (newest first) | `[{run_id, started_at, status, summary}]` |
| GET | `/runs/{run_id}` | run detail incl. steps | `{run_id, status, tests: [{name, status, steps: [{index, verb, status, elapsed_ms, log, error}]}]}` |
| DELETE | `/runs/{run_id}` | delete a stored run | `204` |
| GET | `/config` | read config | config object |
| PUT | `/config` | write config | updated config object |

Concurrency contract: starting a run creates a record immediately and runs it as a background `asyncio` task; `GET /runs/{id}` returns partial state while running. Multiple runs may be in flight simultaneously. A second `POST /runs` while one is running is allowed (independent runs).

## 3. Data model (SQLite, stdlib `sqlite3`)

DB file `~/.hermes/plugins/bioflow-e2e/data/results.db`. Two tables:

```sql
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL,           -- running | passed | failed
  request_json TEXT NOT NULL      -- which tests were requested
);
CREATE TABLE IF NOT EXISTS steps (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  test_name TEXT NOT NULL,
  step_index INTEGER NOT NULL,
  verb TEXT NOT NULL,
  status TEXT NOT NULL,           -- pending | running | passed | failed
  elapsed_ms INTEGER,
  log TEXT,
  error TEXT,
  result_json TEXT,               -- raw step payload for $.path references
  PRIMARY KEY (run_id, test_name, step_index)
);
```

All SQLite access goes through `asyncio.to_thread` (never block the event loop). Use a single connection guarded by a `threading.Lock`.

## 4. Test definition model (hybrid)

### 4.1 YAML (`e2e/tests/*.yaml`)

```yaml
name: reference-smoke          # unique, kebab-case
description: optional one-liner
cleanup: false                 # optional, overrides global config
steps:
  - create_project: { name: "smoke-{{run_id}}" }
  - mcp: { tool: download_reference, args: { accession: "GCF_000005845.2", project_id: "$.project_id" } }
  - wait: { tool: get_job, job_id: "$.download_job_id", until: complete, timeout: 600, poll: 5 }
  - assert: { fact: "$.job.state", equals: "succeeded" }
  - mcp: { tool: list_objects, args: { project_id: "$.project_id" } }
  - assert: { fact: "$.objects", contains_format: "fasta" }
```

Step verbs (exactly these five, per ET-11):

- **`create_project`** — calls `bioflow_create_project`, stores the result (provides `$.project_id`).
- **`upload`** — `{ file: "<fixture path>", format_hint?: ... }`; uploads `e2e/fixtures/<file>` to `$.project_id` via the HTTP client, stores the created object id.
- **`mcp`** — `{ tool: "<name>", args: {...} }`; calls `bioflow_<tool>`; args may contain `$.path` references.
- **`wait`** — `{ tool: get_job, job_id: "$.<ref>", until: <terminal state>, timeout: <sec>, poll: <sec> }`; polls `bioflow_get_job` until the job reaches a terminal state or timeout.
- **`assert`** — `{ fact: "$.<path>", equals: <v> | contains_format: <fmt> | exists: true }`.

**`$.path` references:** steps accumulate under a `state` dict. `$.project_id`, `$.job_id` resolve against the most recent step that produced that key (a flat namespace; later writes shadow earlier ones). `$.job.state` reads a field of a stored object.

### 4.2 Python escape hatch (`e2e/tests/*.py`)

A module that defines functions decorated `@test("name")` using the same primitives. The loader imports the module and turns each decorated function into a `Test` with a callable body:

```python
from e2e.backend.primitives import test, mcp, wait, assert_step, create_project, upload

@test("reads-custom-wait")
async def reads_custom_wait(ctx):
    pid = await create_project(ctx, name="custom")
    for attempt in range(3):          # branching/retry YAML can't express
        r = await mcp(ctx, "run_pipeline", {"kind": "qc", "params": {"object_id": ctx["object_id"]}})
        if not r.get("deduplicated"):
            break
    await wait(ctx, "get_job", {"job_id": ctx["job_id"]}, until="complete", timeout=600)
    assert_step(ctx["job_state"] == "succeeded", "job did not succeed")
```

The runner executes a callable body with a `RunContext` carrying `config`, the two clients, `state`, and a `log` sink.

## 5. Directory layout (all new, under the worktree)

```
e2e/
├── README.md
├── install.sh
├── backend/
│   ├── __init__.py
│   ├── config.py           # config load/validate (Section 1)
│   ├── store.py            # SQLite result store (Section 3)
│   ├── mcp_client.py       # raw JSON-RPC MCP client (Section 0.1)
│   ├── http_client.py      # upload client (Section 0.2)
│   ├── model.py            # dataclasses: Test, Step, RunResult, StepResult
│   ├── loader.py           # YAML + Python discovery
│   ├── primitives.py       # the step primitives + @test decorator (Section 4)
│   ├── runner.py           # orchestrates a run, records steps
│   └── plugin_api.py       # FastAPI router (Section 2); exports `router`
├── desktop/
│   └── plugin.js           # ESM full-page plugin + sidebar nav
├── fixtures/
│   ├── reads_R1.fastq.gz   # tiny paired-end reads
│   └── reads_R2.fastq.gz
└── tests/                  # harness's OWN pytest suite (unit, mocked clients)
    ├── test_loader.py
    ├── test_mcp_client.py
    ├── test_http_client.py
    └── test_runner.py
```

The starter BioFlow tests live in `e2e/tests/*.yaml` alongside the harness's own pytest suite in `e2e/tests/` (distinguished by extension: `*.py` under `tests/` is the pytest suite; the BioFlow test definitions are the `*.yaml` files plus `e2e/tests/bioflow/*.py` for the Python escape hatch). To avoid confusion, name the pytest suite directory `e2e/tests/` and put BioFlow test definitions under `e2e/tests/bioflow/`:

```
e2e/tests/
├── bioflow/               # BioFlow e2e test definitions (loader targets this)
│   ├── reference-smoke.yaml
│   └── reads-path.yaml
├── test_loader.py         # harness unit tests
├── test_mcp_client.py
├── test_http_client.py
└── test_runner.py
```

## 6. Tasks

### Phase 1 — Scaffolding

#### Task 1.1: Create the `e2e/` skeleton
**Files:** create `e2e/README.md`, `e2e/backend/__init__.py`, empty `e2e/tests/bioflow/`, empty `e2e/fixtures/`.
**Step 1:** `mkdir -p e2e/backend e2e/desktop e2e/fixtures e2e/tests/bioflow` from the worktree root.
**Step 2:** `README.md` = one paragraph: what the harness is, how to install (`./install.sh`), how to run tests (the dashboard page), where config lives.
**Step 3:** `backend/__init__.py` = empty.
**Verify:** `find e2e -type d` shows the tree. **Commit:** `feat(e2e): scaffold harness directory`.

#### Task 1.2: Config module
**Files:** create `e2e/backend/config.py`.
**Code:** a `Config` dataclass (`base_url`, `profile`, `cleanup`) with `load(data_dir) -> Config` reading `config.json` (missing keys → defaults) and applying env overrides (`BIOFLOW_BASE_URL`, `BIOFLOW_PROFILE`); `save(data_dir, cfg)`. A `data_dir()` helper returning `~/.hermes/plugins/bioflow-e2e/data` (create it).
**Verify:** a tiny `python -c` load with no file present returns defaults; with a file present returns its values.
**Commit:** `feat(e2e): add harness config loader`.

#### Task 1.3: Install script
**Files:** create `e2e/install.sh`.
**Code:** symlink `e2e/desktop/plugin.js` → `~/.hermes/desktop-plugins/bioflow-e2e/plugin.js`; write `~/.hermes/plugins/bioflow-e2e/dashboard/manifest.json` = `{"name":"bioflow-e2e","api":"plugin_api.py"}`; symlink `e2e/backend/plugin_api.py` → `~/.hermes/plugins/bioflow-e2e/dashboard/plugin_api.py`; mkdir the data dir. Make idempotent (`ln -sfn`), print what it did.
**Verify:** run it, `ls -l` the three targets, confirm symlinks resolve into the worktree.
**Commit:** `feat(e2e): add install script`.

### Phase 2 — Backend clients & store

#### Task 2.1: HTTP upload client
**Files:** create `e2e/backend/http_client.py`.
**Code:** `async def upload_object(base_url, profile, project_id, path_str) -> dict` using `httpx.AsyncClient`; `POST {base_url}/api/v1/projects/{project_id}/objects/upload` with `content=bytes`, headers `X-Filename: <quoted(name)>`, and `X-BioFlow-Profile: <profile>` only when profile is non-empty. Return the parsed JSON. Raise a clear error on non-201 with the response body in the message.
**Verify (unit):** `e2e/tests/test_http_client.py` mocks httpx, asserts the URL, headers (percent-encoded filename, conditional profile header), and body bytes.
**Commit:** `feat(e2e): add HTTP upload client`.

#### Task 2.2: MCP client (raw JSON-RPC)
**Files:** create `e2e/backend/mcp_client.py`.
**Code:** a small `McpClient` class holding `base_url`, `profile`, and an `httpx.AsyncClient`. Methods:
- `_init()` — POST `initialize`, capture `mcp-session-id`, POST `notifications/initialized` (expect 202).
- `call_tool(name, arguments) -> dict` — POST `tools/call` `{"name": f"bioflow_{name}", "arguments": arguments}`, `json.loads(result["content"][0]["text"])`, raise on `isError`.
- URL = `f"{base_url}/api/v1/mcp" + (f"?profile={profile}" if profile else "")`.
**Verify (unit):** `e2e/tests/test_mcp_client.py` mocks httpx with a canned initialize + tools/call flow; asserts the session header is sent on subsequent calls and the wrapped text is unwrapped.
**Commit:** `feat(e2e): add MCP JSON-RPC client`.

#### Task 2.3: Result store
**Files:** create `e2e/backend/store.py`.
**Code:** `ResultStore(data_dir)` opening SQLite at `data/results.db`, running the DDL from Section 3, exposing async `create_run`, `start_step`, `finish_step`, `finish_run`, `get_run`, `list_runs`, `delete_run` — each wrapping a sync `sqlite3` call in `asyncio.to_thread` behind a `threading.Lock`.
**Verify (unit):** `e2e/tests/test_store.py` round-trips a run + steps, asserts delete cascades steps.
**Commit:** `feat(e2e): add SQLite result store`.

### Phase 3 — Test model & loader

#### Task 3.1: Model dataclasses
**Files:** create `e2e/backend/model.py`.
**Code:** `Test` (`name`, `kind`, `description`, `steps: list[Step] | None`, `callable`), `Step` (`verb`, `args: dict`), and run/step result shapes matching Section 3.
**Commit:** `feat(e2e): add test model types`.

#### Task 3.2: YAML loader
**Files:** create `e2e/backend/loader.py`.
**Code:** `discover_tests(tests_dir) -> list[Test]` — glob `tests/bioflow/*.yaml`, parse each into `Test(kind="yaml", steps=[...])`; validate verb against the five allowed verbs and reject unknown keys loudly. Resolve `$.path` references at run time (in the runner), not load time.
**Verify (unit):** `e2e/tests/test_loader.py` loads a sample YAML fixture, asserts step order and verb parsing; asserts a bad verb raises.
**Commit:** `feat(e2e): add YAML test loader`.

#### Task 3.3: Python escape-hatch loader + primitives
**Files:** create `e2e/backend/primitives.py`; extend `loader.py`.
**Code:** `primitives.py` defines the `@test(name)` decorator (registers into a module-level dict) and the async primitives (`create_project`, `upload`, `mcp`, `wait`, `assert_step`) that take a `RunContext` and record state. `loader.py` imports `tests/bioflow/*.py` and turns each `@test`-decorated function into `Test(kind="python", callable=fn)`.
**Verify (unit):** `e2e/tests/test_loader.py` imports a sample `@test` module, asserts it's discovered.
**Commit:** `feat(e2e): add Python escape-hatch primitives and loader`.

### Phase 4 — Runner

#### Task 4.1: Step executor
**Files:** create `e2e/backend/runner.py`.
**Code:** `async def _execute_step(ctx, step, store, run_id, test_name, index)` dispatching on `step.verb`; for each: start the step row, run the action (resolving `$.path` refs in args), capture `result_json`/`log`, finish the step row with `passed` or `failed`+`error`. `wait` polls `bioflow_get_job` at `poll` seconds until `until` or `timeout`. `assert` evaluates its predicate and raises on mismatch.
**Commit:** `feat(e2e): add step executor`.

#### Task 4.2: Runner orchestration
**Files:** extend `runner.py`.
**Code:** `async def run_test(ctx, test) -> None` creating a fresh project via `create_project`, running steps in order, stopping at first failure (ET-19), and deleting the project at the end only when `cleanup` is set. `async def run_all(ctx, tests)` running tests sequentially but continuing after a failed test (ET-20). Each runs as a background `asyncio.create_task` in the plugin process.
**Verify (unit):** `e2e/tests/test_runner.py` uses fake clients; asserts stop-on-first-failure within a test and continue-across-tests.
**Commit:** `feat(e2e): add run orchestration`.

### Phase 5 — Backend routes

#### Task 5.1: `plugin_api.py` skeleton + config routes
**Files:** create `e2e/backend/plugin_api.py`.
**Code:** `router = APIRouter()`; `GET /config`, `PUT /config` (read/write config.json). Store the config in a module-level holder initialized lazily.
**Verify:** `python -c "import ...; router"` imports cleanly; `uvicorn`-style smoke not needed yet.
**Commit:** `feat(e2e): add backend router skeleton`.

#### Task 5.2: tests + runs routes
**Files:** extend `plugin_api.py`.
**Code:** `GET /tests` (loader), `POST /runs` (start background run task, return `run_id`), `GET /runs`, `GET /runs/{run_id}`, `DELETE /runs/{run_id}` per Section 2.
**Commit:** `feat(e2e): add run/tests API routes`.

#### Task 5.3: Wire runner into routes
**Files:** extend `plugin_api.py`.
**Code:** on `POST /runs`, build the `RunContext` (config, `McpClient`, `http_client`, store, `state`, log sink), `asyncio.create_task(run_all(...))`, return immediately. Guard concurrent `POST /runs` with an in-process lock only around run-record creation (not execution).
**Commit:** `feat(e2e): wire runner to the API`.

### Phase 6 — Desktop plugin

#### Task 6.1: `plugin.js` skeleton (page + nav)
**Files:** create `e2e/desktop/plugin.js`.
**Code:** default-export `{ id: 'bioflow-e2e', name: 'BioFlow E2E', register(ctx) {...} }`; register a full page (`ROUTES_AREA`, `data: { path: '/bioflow-e2e' }`) and a sidebar nav row (`SIDEBAR_NAV_AREA`, `data: { path: '/bioflow-e2e', label: 'E2E Tests', codicon: 'beaker' }`). Render a placeholder component using `jsx()`.
**Verify:** run `./install.sh`, reload desktop plugins (⌘K → Reload desktop plugins), confirm the sidebar row + page appear.
**Commit:** `feat(e2e): add desktop page skeleton`.

#### Task 6.2: Test list + run controls
**Files:** extend `plugin.js`.
**Code:** `useQuery(['bioflow-e2e','tests'], () => ctx.rest('/tests'))` to list tests; a "Run all" button and a per-test "Run" button via `useMutation` calling `ctx.rest('/runs', { method:'POST', body:{ tests:[name] } })`; on success invalidate the runs query.
**Commit:** `feat(e2e): add test list and run controls`.

#### Task 6.3: Run detail view (per-step tree, logs, timing)
**Files:** extend `plugin.js`.
**Code:** `useQuery(['bioflow-e2e','run', runId], () => ctx.rest('/runs/'+runId), { refetchInterval: (q) => running ? 2000 : false })`; render per-step status dot, elapsed ms, and an expandable `<details>` for logs. Use `StatusDot`/`Badge` from the UI kit; theme vars only, no hardcoded colors.
**Commit:** `feat(e2e): add run detail view`.

#### Task 6.4: History, re-run, delete, settings
**Files:** extend `plugin.js`.
**Code:** history list (`/runs`), re-run (POST `/runs` with the same test names), delete (`DELETE /runs/{id}` then invalidate), and a small settings form (GET/PUT `/config` for `base_url` and `profile`).
**Commit:** `feat(e2e): add history, re-run, delete, and settings`.

### Phase 7 — Starter tests + fixtures

#### Task 7.1: Fixtures
**Files:** create `e2e/fixtures/reads_R1.fastq.gz`, `reads_R2.fastq.gz`.
**Code:** generate a tiny paired-end set (e.g. 2×100 reads of a synthetic 1 kb reference) — a script under `e2e/fixtures/make_fixtures.sh` that emits them, committed alongside. Keep files well under `MAX_SIMPLE_UPLOAD_BYTES`.
**Commit:** `feat(e2e): add reads fixtures`.

#### Task 7.2: Discover exact pipeline kinds
**Files:** none (investigation task).
**Code:** with BioFlow running, call `bioflow_list_tools` and hit `GET /jobs/types` (or read `all_handlers()` output via the MCP unknown-kind error) to record the exact `kind` strings and params for the QC, trim, alignment, and reference-index pipelines. Record them in `e2e/tests/bioflow/README.md`.
**Verify:** the recorded kind names round-trip through `bioflow_run_pipeline` without the unknown-kind error.
**Commit:** `docs(e2e): record pipeline kind names for starter tests`.

#### Task 7.3: `reference-smoke.yaml`
**Files:** create `e2e/tests/bioflow/reference-smoke.yaml`.
**Code:** per Section 4.1 — `create_project` → `download_reference` (small accession, e.g. `GCF_000005845.2`) → `wait` → `assert` state succeeded → `list_objects` → `assert` contains `fasta`. (Index build added only if Task 7.2 shows a trivially-runnable index kind.)
**Verify:** run via the dashboard; passes against the running stack.
**Commit:** `feat(e2e): add reference smoke test`.

#### Task 7.4: `reads-path.yaml`
**Files:** create `e2e/tests/bioflow/reads-path.yaml`.
**Code:** `create_project` → `upload` both reads fixtures → `run_pipeline` QC → `wait` → `run_pipeline` trim → `wait` → `run_pipeline` align → `wait` → `assert` a BAM object is present (via `list_objects`).
**Verify:** run via the dashboard; passes and produces a BAM.
**Commit:** `feat(e2e): add reads-path test`.

### Phase 8 — Verification & docs

#### Task 8.1–8.4: Harness unit tests
Run the full harness pytest suite (`e2e/tests/`) with mocked clients — loader, MCP client, HTTP client, store, runner. Every test that produces code in Phases 2–4 already has its test written inline (TDD); this task is the aggregate gate.
**Verify:** `python -m pytest e2e/tests/ -q` → all pass, no network.
**Commit:** `test(e2e): green harness unit suite`.

#### Task 8.5: End-to-end manual verification
Run the verification checklist (Section 7) against the live stack.

---

## 7. Verification checklist (quality gates)

Resilience / failure modes:
- [ ] BioFlow down → `POST /runs` returns a run that fails fast with a clear "cannot reach BioFlow at <url>" error on its first step, not a hanging spinner.
- [ ] MCP `initialize` fails / session missing → the run records the failure with the response body in the error.
- [ ] `wait` timeout → the step is marked `failed` with "timed out after Ns", and the run ends.
- [ ] Upload exceeds `MAX_SIMPLE_UPLOAD_BYTES` → the upload step fails with the API's message surfaced, not a swallowed error.

Concurrency:
- [ ] Two runs started back-to-back produce two independent run records and don't corrupt the SQLite store.
- [ ] `GET /runs/{id}` while running returns partial step state (pending/running) without blocking.

Edge cases:
- [ ] Empty tests directory → `/tests` returns `[]`; "Run all" is a no-op with a message.
- [ ] A step references `$.missing` → the step fails with "unresolved reference" naming the key.
- [ ] `cleanup: true` deletes the throwaway project; `false` (default) leaves artifacts for inspection.

Practical budgets:
- [ ] Fixtures: keep each upload under ~1 MB (well below `MAX_SIMPLE_UPLOAD_BYTES`).
- [ ] SQLite: each run is ~N step rows; history is bounded by user deletion (ET-30), no auto-prune in v1.
- [ ] `wait` default `poll: 5s`, `timeout: 600s` — long pipelines get longer timeouts per test, not globally.

API protocol: every route in Section 2 returns the documented shape; errors return a JSON body with a human-readable `detail`.

## 8. Open items to confirm during implementation (small, verify-then-code)

1. Exact `kind` strings and `params` for QC / trim / align / build-index (Task 7.2).
2. `bioflow_create_project`'s return shape — confirm the project-id key before writing `create_project` primitive's state capture.
3. The exact export name for `useQuery`/`useMutation`/`queryClient` from `@hermes/plugin-sdk` (the skill lists them; confirm against a live plugin or the SDK reference during Task 6.2).
4. Whether `profile` should be the profile id or accept the `?profile=<id>` URL fragment — the id is correct per `owner_for`; confirm against Settings → MCP's paste-ready URL.

## 9. Out of scope (deferred — do not implement)

Cron/scheduled runs, a web-dashboard tab, object diff/snapshot views, multi-profile runs, CI wiring, auto-pruning of run history.
