# BioFlow End-to-End Test Harness

**Date:** 2026-08-13
**Status:** Draft
**Issue:** — (branch `feat/e2e-test-harness`)

## Problem

BioFlow has no way to verify, end to end, that its MCP interface and its
pipelines actually work against a running instance. The backend test suite
mocks the seams that matter — the MCP server and `asyncssh` are patched
wholesale (see #356) — so no automated test exercises the real MCP server,
real job execution, or real data flow. Confirming that "an agent can drive
BioFlow through its MCP interface and get correct results" is currently a
manual, error-prone process.

## Goal

A Hermes desktop-app plugin, developed in a new `e2e/` subfolder of the
BioFlow repo, that lets the user author end-to-end tests (ordered sequences
of MCP tool calls, data uploads, and assertions), trigger them from a
dashboard, and see per-step pass/fail, logs, and timing — with results
persisted and re-runnable.

## Decisions

Recorded choices and the reasoning behind each.

| Decision | Choice | Why |
|---|---|---|
| Surface | Hermes desktop app, full page + sidebar nav | The user already works in the desktop app; zero build step; React Query is built in for run/poll/results |
| Authoring | Hybrid: YAML for linear tests, Python escape hatch | YAML covers simple sequential tests without code; Python covers branching, retries, and complex waits |
| Data ingestion | MCP for the control plane, BioFlow's HTTP API for upload | The MCP server has no upload tool; using HTTP for upload avoids changing BioFlow |
| Fixtures | Bundled in the repo, re-uploaded automatically each run | The user never re-uploads by hand; runs stay reproducible |
| NCBI downloads | Via the existing MCP `download_reference` tool | Already implemented — downloads an assembly's genome FASTA as an async job polled with `get_job` |
| First slice | Framework + one reference smoke test + one reads-path test | Exercises every seam (MCP control plane, HTTP upload, fixtures) with the least surface |
| Result detail | Per-step status + logs + timing + history + re-run | Enough to debug a failing pipeline; a full diff/snapshot system is deferred |
| Naming | subfolder `e2e/`, plugin id `bioflow-e2e` | Concise; matches "end-to-end" |

## Architecture

```
e2e/
├── fixtures/              # bundled test data (small FASTQ/FASTA), committed
├── tests/                 # test definitions: .yaml + optional .py escape hatch
├── backend/
│   └── plugin_api.py      # FastAPI router — runner, MCP+HTTP clients, result store
├── desktop/
│   └── plugin.js          # ESM plugin — full page + sidebar nav (no build)
└── install.sh             # symlinks plugin.js + plugin_api.py into ~/.hermes/
```

Components — each has one purpose, a defined interface, and can be tested on
its own:

| Unit | Does | Depends on |
|---|---|---|
| MCP client | Speaks Streamable HTTP to `/api/v1/mcp` using the `mcp` SDK, under the test profile | BioFlow running |
| HTTP client | Uploads fixtures to BioFlow's object-upload endpoint | BioFlow running |
| Test loader | Parses YAML definitions and resolves Python escape-hatch functions | `tests/` |
| Runner | Executes steps and records per-step status, log, and timing | loader + both clients + store |
| Result store | Persists runs, steps, logs, timings, and history (SQLite in the plugin data dir) | — |
| Desktop page | Test list, run controls, per-step tree, logs, timing, history + re-run | backend via `ctx.rest` |

## Requirements

### Surface

- **ET-1** The page is reachable as a full-page view from the Hermes desktop sidebar.
- **ET-2** The page lists every test definition the harness discovers.
- **ET-3** The page provides a control to run a single selected test.
- **ET-4** The page provides a control to run all tests.
- **ET-5** The page shows, for each step of a run, its pass/fail status, elapsed time, and expandable log output.
- **ET-6** The page shows a history of past runs.
- **ET-7** The page provides a control to re-run a past run.

### Test definition (hybrid)

- **ET-8** A test is an ordered sequence of steps.
- **ET-9** A linear test can be expressed as a YAML file without writing code.
- **ET-10** A test can be expressed as a Python function that uses the same step/assert/wait primitives as a YAML test.
- **ET-11** A step is exactly one of: create a project, upload a fixture, call an MCP tool, wait for a job to reach a terminal state, or assert on a prior result.
- **ET-12** A test can reference bundled fixture files, which the runner uploads automatically when the test runs.
- **ET-13** A test can specify an NCBI accession, which the runner downloads as a reference through the MCP `download_reference` tool.
- **ET-14** A later step can reference a value produced by an earlier step (for example, a job id returned by `run_pipeline`).

### Runner & isolation

- **ET-15** Each run creates a fresh project and does not reuse an existing project.
- **ET-16** The harness targets a configurable BioFlow base URL, defaulting to `http://localhost:8000`.
- **ET-17** The harness runs under a dedicated test profile, isolated from real project data.
- **ET-18** The runner records status, log output, and elapsed time for every step.
- **ET-19** A failing step stops the remaining steps of that test and marks the run failed.
- **ET-20** When one test fails, the other tests in the same run still execute.
- **ET-21** Run results persist across desktop-app restarts.

### Backend

- **ET-22** The backend exposes routes under `/api/plugins/bioflow-e2e/` to list tests, start a run, and read a run's results.
- **ET-23** The backend drives BioFlow through its MCP server over Streamable HTTP at `/api/v1/mcp`.
- **ET-24** The backend uploads fixtures through BioFlow's HTTP object-upload endpoint.
- **ET-25** The backend resolves a fixture reference to a path inside the harness's `fixtures/` directory.

### Starter tests

- **ET-26** The harness ships a reference smoke test that creates a project, downloads a reference, builds an index, and asserts the reference and index objects are present.
- **ET-27** The harness ships a reads-path test that uploads FASTQ fixtures, runs QC, trim, and alignment, and asserts a BAM object is produced.

### Install & non-functional

- **ET-28** A single install script places the desktop plugin and the Python backend into the Hermes plugin directories so the plugin loads in the desktop app.
- **ET-29** A long-running job poll does not block the runner from executing other tests or the page from showing live results.
- **ET-30** A user can delete a stored run from the history.

## Test definition model

A linear test is a YAML file: a name, optional fixtures, and an ordered list
of steps. Values are addressable across steps through a `$.path` reference
into the accumulated step results.

```yaml
name: reference-smoke
steps:
  - create_project: { name: "smoke-{{run_id}}" }
  - mcp: { tool: download_reference, args: { accession: "GCF_000005845.2", project_id: "$.project_id" } }
  - wait: { tool: get_job, job_id: "$.download_job_id", until: complete, timeout: 600 }
  - assert: { fact: "$.job.state", equals: "succeeded" }
  - mcp: { tool: list_objects, args: { project_id: "$.project_id" } }
  - assert: { fact: "$.objects", contains_format: "fasta" }
```

A non-linear test is a Python function using the same primitives
(`create_project`, `upload`, `mcp`, `wait`, `assert_step`), registered so the
loader finds it and the runner executes it identically. The Python escape
hatch exists for branching, retries, and custom waits that YAML cannot
express.

## Data model

- **Run** — id, test name, started/ended timestamps, overall status, target profile.
- **Step** — parent run, index, verb, status (pending/running/passed/failed), elapsed time, log text, error message, and the raw result payload for later `$.path` references.

Both live in SQLite under the plugin's data directory; the desktop page reads
them through the backend's run/results routes.

## Out of scope

Scheduled or cron-triggered runs, a web-dashboard tab, rich object
diff/snapshot views, multi-profile runs, and CI integration. All are future
increments.
