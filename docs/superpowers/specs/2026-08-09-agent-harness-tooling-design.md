# Agent Harness Tooling — Design

Design for [#83](https://github.com/syntheticgio/bioflow/issues/83) — add a
curated, pre-installed set of extensions, skills, and MCP servers to the
in-app Pi agent harness.

The harness itself is already shipped ([#30](https://github.com/syntheticgio/bioflow/issues/30),
`docs/superpowers/specs/2026-08-09-ai-agent-harness-design.md`): Pi 0.84.1 and
the pi-mcp-adapter extension live in the api container, four starter skills
are vendored under `backend/pi-skills/`, and the agent connects to BioFlow's
own MCP server. Conversation persistence shipped in
[#97](https://github.com/syntheticgio/bioflow/issues/97). This design decides
what *else* the harness should ship with.

## Priorities

1. **BioFlow-internal work** — the agent is expert at driving pipelines inside
   the app (run, interpret, debug). This is the most important job.
2. **External research & data** — the agent can go outside the app: fetch
   reference data on demand, check literature.
3. **Self-extension** — the agent building its own skills/servers is
   deliberately deprioritized. It is documented as a future growth path, not
   pre-installed.

## Acceptance jobs

The curated set is judged against two end-to-end jobs a user would actually
ask the agent to do:

- **Job 1 — QC debugging:** given a project with a failed QC job, the agent
  diagnoses *why* it failed (logs, errors, resource limits) and tells the user
  exactly what to rerun.
- **Job 2 — variants in a gene:** given raw reads, the agent drives
  QC/trim → reference genome (in-app download or existing object) → align →
  variant call → extracts the gene's coordinates from the annotation → reports
  the variants in that region with consequence-based interpretation.

## Current state

What already exists and stays:

- Pi `0.84.1` + pi-mcp-adapter `2.21.1` pinned in the api image
  (`backend/Dockerfile`), with build-time assertions (`pi --version`,
  `pi list`).
- Four vendored skills: `run-qc`, `interpret-multiqc`, `suggest-next-steps`,
  `debug-failed-job` (`backend/pi-skills/`).
- BioFlow MCP server with 18 tools, including `bioflow_search_ncbi`,
  `bioflow_download_reference`, `bioflow_run_pipeline`,
  `bioflow_suggest_next`, `bioflow_get_guide` — the primary "data on demand"
  and pipeline-driving surface already.
- Per-project agent subprocess (`AgentService`), SSE streaming UI, and a
  tested `AGENT_EXTRA_MCP_SERVERS` setting whose value is merged into every
  spawned agent's `--mcp-config` (`backend/app/config.py`,
  `backend/app/services/agent_service.py`, tests in `test_config.py` and
  `test_agent_service.py`).
- Conversation persistence across sessions (#97).

Key reframing that shaped this design: the two acceptance jobs are largely
achievable with in-app MCP tools plus well-written skills. The external set is
therefore small — it closes real gaps (literature verification, assembly
browsing) instead of duplicating what the app already does.

## Extension set

| Extension | Decision | Rationale |
|---|---|---|
| pi-mcp-adapter | Keep (pinned) | Required for any MCP connectivity |
| pi-subagents | **Add** | Parallelizes independent work inside a job (e.g., interpret QC while fetching the reference for Job 2) |
| pi-hermes-memory | **Add** | Durable cross-session memory; coherent now that persistence (#97) exists |
| context-mode | Defer | Real value only in very long sessions; token-heavy; most of its surface (dashboard, browser) is useless headless. Revisit later with session-length criteria |
| ask-user-questions | Reject | The harness has no structured-question UI surface in `--mode rpc`; nothing to render on |
| todo | Reject | Duplicates pi core's own todo tool |
| bioskill-manager | Defer | Runtime on-demand installs — the self-extension path. Documented as the future growth path instead of pre-installed |

## Skill set

### Written by us, vendored under `backend/pi-skills/`

Kept and sharpened:

- `run-qc`, `interpret-multiqc`, `suggest-next-steps` — unchanged.
- `debug-failed-job` — sharpened around Job 1 (log location, resource-limit
  signals, what "rerun" means for each failure class).

New:

- **`drive-pipelines`** — the meta-skill. One skill teaches how to drive any
  BioFlow stage: `bioflow_get_guide` → check the object → `bioflow_run_pipeline`
  → poll the job → verify the output. Stage-specific knowledge lives in
  `bioflow_get_guide` and `bioflow_suggest_next`, so this skill does not need
  to enumerate stages.
- **`interpret-alignment`** — mapping rate, coverage, and the failure modes
  that matter (low mapping, uneven coverage, reference mismatch).
- **`variant-analysis`** — the Job 2 workflow end to end: obtain/verify the
  reference, align, call variants, extract gene coordinates from the
  annotation, filter to the region, interpret bcftools-csq output.
- **`bioflow-database-access`** — the scoped-down "400-database" item. One
  skill that says how to reach the databases that actually matter: in-app NCBI
  tools first (`bioflow_search_ncbi`, `bioflow_download_reference`), the
  datasets MCP server for assembly/taxonomy browsing, the fetch MCP server for
  web content, and documented REST paths for EBI/Ensembl/UniProt when needed.
  A curated skill, not a database catalog.

### Imported

- **`biorxiv-search`** — the one community skill that earns a slot
  (literature checks, cheap to vendor). Vendored into `backend/pi-skills/`
  with attribution and license noted in the README, consistent with how all
  skills are shipped (no build-time fetching).

## MCP servers

Exactly two external servers, both stdio, both installed in the api image,
both wired through the existing `AGENT_EXTRA_MCP_SERVERS` setting:

- **datasets** (NCBI) — assembly/taxonomy/sequence browsing beyond the
  single-accession in-app download tool. Distribution (npm/PyPI/binary)
  verified at plan time and pinned.
- **fetch** (`@modelcontextprotocol/server-fetch`) — the literature-
  verification primitive. Installed globally via pinned `npm install -g`, not
  fetched via `npx` at runtime (headless container, reproducible builds).

### Rejected, with reasons on record

| Candidate | Reason |
|---|---|
| memory MCP (official) | Duplicates pi-hermes-memory |
| bio-mcp seqkit / samtools | Duplicate the CLIs the image already ships; the agent can shell out to them directly |
| GEOmcp / GEO datasets | BioFlow is assembly/annotation, not expression analysis. Revisit only if expression work appears |
| cytado citations | Niche; no literature feature to serve yet |
| bio-mcp-blast | Defer — network BLAST is a plausible future add, not a current need |
| tooluniverse (5 skills) | Overlap with in-app tools + `bioflow-database-access` |
| deep-research / research stacks (4 variants) | Long-report generators, not the job; quality and pi-compatibility vary |
| skill-creator / mcp-builder | Self-extension, deprioritized |

## Delivery mechanics

No backend code changes are needed — the config → agent-service → per-project
MCP config path already handles extra servers.

1. **Dockerfile** (extends the existing pi install step):
   - `pi install npm:pi-subagents@<pin>` and `pi install npm:pi-hermes-memory@<pin>`
     as pinned ARGs alongside the adapter; the `pi list` assertion extended to
     prove all three registered.
   - MCP server binaries installed and pinned in the image (fetch via npm
     global; datasets via its verified distribution).
   - `COPY pi-skills /root/.pi/agent/skills` unchanged — the vendored dir grows.

2. **docker-compose.override.yml** — add to the `api` service environment:

   ```yaml
   AGENT_EXTRA_MCP_SERVERS: '{"mcpServers": {
     "fetch": {"command": "mcp-server-fetch"},
     "datasets": {"command": "<datasets-bin>"}}}'
   ```

   Stdio servers run inside the api container; no new containers. The override
   is the deployment file this app actually runs.

3. **Vendored skills** — all skills follow the existing conventions in
   `backend/pi-skills/README.md`: real MCP tool names, the `mcp({ tool: ... })`
   proxy syntax, job kinds read from `bioflow://jobs/types` or
   `bioflow_suggest_next` payloads, and a preference for
   `bioflow_suggest_next` over reasoning from format names. Imported skills
   carry attribution + license notes.

4. **Docs** — `backend/pi-skills/README.md` documents the full set, what each
   item is for, and the update procedure (bump pin → rebuild → run the two
   acceptance jobs).

5. **Size/build impact** — two small npm extensions plus two MCP server
   packages; modest layer additions. Pinned versions keep build time stable.

## Tests

- Config test: the default compose env carries both external servers
  (`agent_extra_mcp_servers` populated from `AGENT_EXTRA_MCP_SERVERS`).
- Skill-presence test: every `backend/pi-skills/*/SKILL.md` parses with valid
  frontmatter and references only real MCP tools, so a new skill cannot
  silently fail to load.
- `_build_mcp_config` merge test extended to the actual default servers.
- Build-time assertions already established: `pi --version` pin check and
  `pi list` showing adapter + subagents + hermes-memory.

## Acceptance verification

Precondition: an AI provider configured in BioFlow settings — the harness has
no model otherwise.

1. Backend suite green (extended as above).
2. Image builds with all pins asserted.
3. Manual e2e against the worktree-up stack (localhost:5273) with a fresh
   rebuild (`docker compose up -d --build api`):
   - Job 1 end to end against a real failed QC job.
   - Job 2 end to end against a real project (small reads + a real organism
     reference).
   - A skills-probe: the agent can find and name its skills.
   - A literature check through fetch/biorxiv.

**Failure exit:** if Job 2 cannot be completed because variant-calling-to-gene-
region is a pipeline capability gap rather than a skill gap, stop and
re-scope. A skill cannot paper over a missing capability, and that should be
known before building the skill.

## Out of scope / follow-ups

- context-mode evaluation (revisit with session-length criteria)
- bioskill-manager as the on-demand growth path (documented, not installed)
- GEO/expression support (only if expression work appears)
- bio-mcp-blast, cytado citations
- skill-creator / mcp-builder
- The literal 400-database catalog (replaced by `bioflow-database-access`)
