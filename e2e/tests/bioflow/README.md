# BioFlow e2e test definitions

Tests in this directory are discovered by the harness loader
(`e2e/backend/loader.py`). Linear tests are `.yaml`; a Python escape hatch is
a `.py` file that uses `@test(...)` from `e2e.backend.primitives`.

## Step reference model

Steps run in order; each step's result is stored in the run state under its
`as:` alias (or a default key — `project`, `upload`, or the tool name).
References `$.a.b.c` resolve against that state. `create_project` also sets
`$.project_id`. `{{run_id}}` in a string arg is replaced with the run id.

## Discovered BioFlow facts (verified against source, not recalled)

- **MCP endpoint:** `POST /api/v1/mcp` (Streamable HTTP, JSON-RPC), profile via
  `?profile=<id>`.
- **Pipeline handler kinds** (`run_pipeline` validates against
  `queue/registry.all_handlers()`): `run_qc`, `trim_reads`, `align_reads`,
  `build_index`, `call_variants`, `run_bam_stats`, `run_vcf_stats`,
  `download_assembly`, and ~45 others.
- **Job terminal states:** `succeeded`, `failed`, `cancelled`, `dead`.
- **Launch flow (important):** the intended agent path is `suggest_next` →
  each card's `launch` is `{"endpoint", "body"}` — a *complete* HTTP request
  body built server-side — posted to the REST API, *not* a `run_pipeline`
  `params` dict. `run_pipeline(kind, params)` passes `params` straight through
  as the raw handler payload, so hand-building it requires the exact
  `object_id`/`project_id`/`name`/`platform`/… shape the launch endpoints
  assemble. The reads-path test needs an HTTP-launch step to follow the real
  flow (Task 8.5).
- **Upload endpoint:** `POST /api/v1/projects/{id}/objects/upload` — raw bytes
  in the body, filename in `X-Filename` (percent-encoded), profile in
  `X-BioFlow-Profile`.

## Status

- `reference-smoke.yaml` — correct as written (downloads `GCF_000005845.2`,
  an ~4.6 Mb E. coli genome).
- `reads-path.yaml` — DRAFT; finalize the QC/trim/align launch flow against a
  live stack (needs an HTTP-launch step in the runner, then real params).
