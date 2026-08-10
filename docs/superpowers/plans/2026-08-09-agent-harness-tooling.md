# Agent Harness Tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the curated, pinned set of extensions, skills, and MCP servers for the in-app Pi agent harness — pi-subagents + pi-hermes-memory extensions, the NCBI datasets + fetch MCP servers, four new BioFlow skills, and a sharpened debug skill — so the two acceptance jobs work end to end.

**Architecture:** Everything ships inside the existing api image (no new containers): the two extensions install via `pi install` alongside the existing pi-mcp-adapter; the two stdio MCP servers install via pinned `pip install` and are wired into every agent spawn through the existing `AGENT_EXTRA_MCP_SERVERS` → `AgentService._build_mcp_config()` path; skills are vendored under `backend/pi-skills/` and copied into the image. No backend Python code changes are needed.

**Tech Stack:** Docker (api image), pi 0.84.1 + pi-mcp-adapter 2.21.1 (existing), pi-subagents 0.45.1, pi-hermes-memory 0.9.4, `mcp-server-fetch` 2026.7.10 (PyPI), `ncbi-datasets-mcp` 0.1.4 (PyPI), pytest. Markdown SKILL.md skills (existing format).

**Spec:** `docs/superpowers/specs/2026-08-09-agent-harness-tooling-design.md` — read it before starting.

## Global Constraints

- **Version pins are exact and mandatory** (verified against registries on 2026-08-09):
  - `pi-subagents` npm package `0.45.1`
  - `pi-hermes-memory` npm package `0.9.4`
  - `mcp-server-fetch` PyPI package `2026.7.10` (console script `mcp-server-fetch`)
  - `ncbi-datasets-mcp` PyPI package `0.1.4` (console script `ncbi-datasets-mcp`)
- **No `docker compose` from a worktree** — a PreToolUse hook blocks it. Use plain `docker build` for image verification, or `./ops/worktree-up.sh` for the full stack.
- **Backend tests run via `./backend/run-worktree-tests.sh tests/ -q`** from this worktree. `docker compose exec api python -m pytest` silently tests main's code.
- **Skill conventions** (from `backend/pi-skills/README.md`): skills name the real `bioflow_*` MCP tool and its arguments; job kinds come from `bioflow://jobs/types` or `bioflow_suggest_next` payloads, never invented; prefer `bioflow_suggest_next` over reasoning from format names.
- **Real MCP tools** (from `backend/app/mcp/server.py`): `bioflow_whoami`, `bioflow_list_projects`, `bioflow_get_project`, `bioflow_create_project`, `bioflow_list_objects`, `bioflow_get_object`, `bioflow_suggest_next`, `bioflow_run_pipeline`, `bioflow_get_job`, `bioflow_list_jobs`, `bioflow_cancel_job`, `bioflow_search_objects`, `bioflow_search_ncbi`, `bioflow_download_reference`, `bioflow_list_tools`, `bioflow_get_guide`.
- **Real job kinds** (from `backend/app/queue/registry.py` handlers): `run_qc`, `trim_reads`, `build_index`, `align_reads`, `index_bam`, `call_variants`, `run_vcf_stats`, `download_sra_run`, plus any in `bioflow://jobs/types`.
- **Guide topics** (valid `bioflow_get_guide` args): `getting-started`, `acquiring-data`, `read-qc-and-trimming`, `alignment-and-variants`, `de-novo-assembly`, `rna-quantification`.

**Two plan-time deviations from the spec (both deliberate, both recorded here):**

1. **`biorxiv-search` is NOT installed.** On inspection (`yorkeccak/scientific-skills/skills/biorxiv-search`), it requires a paid third-party Valyu API key and its script paths are Claude-Code-specific (`~/.claude/plugins/cache`). The literature need is served by the `fetch` MCP server instead (free, no key). This is a change from the approved spec, which listed it; the fetch-based path in the `bioflow-database-access` skill replaces it.
2. **The "datasets" MCP server is `ncbi-datasets-mcp` 0.1.4** — the user's own PyPI package (`syntheticgio/ncbi-datasets-mcp-server`), not a third-party server. It downloads the NCBI CLI on first use (`NCBI_AUTO_INSTALL`).

---

### Task 1: Image — install the extensions and MCP servers

**Files:**
- Modify: `backend/Dockerfile` (ARG block near line 492, pi install RUN near line 534, after the skills COPY near line 546)

**Interfaces:**
- Consumes: the existing `PI_VERSION`/`PI_MCP_ADAPTER_VERSION` ARG + `pi install` pattern already in the file.
- Produces: the api image contains pi-subagents, pi-hermes-memory, `mcp-server-fetch`, and `ncbi-datasets-mcp`, all pinned; `pi list` proves the three extensions registered.

- [ ] **Step 1: Add the four new ARGs next to the existing pins**

Edit `backend/Dockerfile`, the ARG block that currently reads:

```dockerfile
ARG NODE_VERSION=22.23.2
ARG PI_VERSION=0.84.1
ARG PI_MCP_ADAPTER_VERSION=2.21.1
```

Add four lines after `PI_MCP_ADAPTER_VERSION`:

```dockerfile
ARG PI_SUBAGENTS_VERSION=0.45.1
ARG PI_HERMES_MEMORY_VERSION=0.9.4
ARG MCP_FETCH_VERSION=2026.7.10
ARG NCBI_DATASETS_MCP_VERSION=0.1.4
```

- [ ] **Step 2: Extend the pi install RUN to add the two extensions**

The RUN currently is:

```dockerfile
RUN set -e \
    && pi install npm:pi-mcp-adapter@${PI_MCP_ADAPTER_VERSION} \
    && pi list
```

Replace it with (keeps the existing comment block above it, which documents the headless-install reasoning):

```dockerfile
RUN set -e \
    && pi install npm:pi-mcp-adapter@${PI_MCP_ADAPTER_VERSION} \
    && pi install npm:pi-subagents@${PI_SUBAGENTS_VERSION} \
    && pi install npm:pi-hermes-memory@${PI_HERMES_MEMORY_VERSION} \
    && pi list
```

`pi list` must show all three `npm:...` packages. If a package's install prompts at build time, the build fails loudly here — that is the point of the assertion.

- [ ] **Step 3: Add a RUN installing the two MCP servers via pip**

Insert a new RUN between the pi install block and the `COPY pi-skills` line:

```dockerfile
# The two external stdio MCP servers. Installed as pinned Python packages so
# the agent's --mcp-config (built from AGENT_EXTRA_MCP_SERVERS) can reference
# the console scripts by name. The import assertion catches a rename that a
# silent pip install would otherwise ship.
RUN set -e \
    && pip install --no-cache-dir \
        mcp-server-fetch==${MCP_FETCH_VERSION} \
        ncbi-datasets-mcp==${NCBI_DATASETS_MCP_VERSION} \
    && python -c "import mcp_server_fetch, ncbi_datasets_mcp"

# Verify the console scripts exist (they are what the agent's config invokes).
RUN set -e \
    && command -v mcp-server-fetch \
    && command -v ncbi-datasets-mcp
```

If either console-script name is wrong, the second RUN fails and the plan's Task 2 config must be updated to match the real name.

- [ ] **Step 4: Build the image and verify the pins landed**

From the worktree root (plain `docker build` is allowed; `docker compose` is not):

```bash
docker build -f backend/Dockerfile -t biopipe-agent-tooling-check backend
```

The base layers are cached from the running stack, so only the changed layers rebuild. Expected: build succeeds; the `pi list` output in the build log shows `npm:pi-subagents@0.45.1` and `npm:pi-hermes-memory@0.9.4`; the `command -v` assertions pass.

- [ ] **Step 5: Commit**

```bash
git add backend/Dockerfile
git commit -m "feat(agent): install pi-subagents, pi-hermes-memory, and the datasets+fetch MCP servers in the api image (#83)"
```

---

### Task 2: Compose — wire the external servers into every agent spawn

**Files:**
- Modify: `docker-compose.override.yml` (api service `environment:` block near line 24, api service `volumes:` block near line 14)
- Modify: `backend/run-worktree-tests.sh` (mounts near lines 133-136)
- Create: `backend/tests/test_compose_agent_servers.py`

**Interfaces:**
- Consumes: the existing `AGENT_EXTRA_MCP_SERVERS` setting (`backend/app/config.py:268`) and `AgentService._build_mcp_config()` (`backend/app/services/agent_service.py:533`) — both already tested.
- Produces: every spawned agent's `--mcp-config` contains `fetch` and `datasets` stdio entries alongside `bioflow`; the wiring is covered by a test.

- [ ] **Step 1: Write the failing wiring test**

Create `backend/tests/test_compose_agent_servers.py`:

```python
"""The two external MCP servers must be wired into every agent spawn.

The wiring lives in docker-compose.override.yml: AGENT_EXTRA_MCP_SERVERS is
merged into every spawned agent's --mcp-config by AgentService. If someone
edits that env value (renames a server, removes one, breaks the JSON), the
agent silently loses the server -- nothing else fails. This test pins the
wiring to the two servers Task 1 installed.

The test reads the compose file from the repo checkout and skips when it is
not visible (e.g. a test container that does not mount it) -- the skip is
explicit rather than a silent pass.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERRIDE = REPO_ROOT / "docker-compose.override.yml"


@pytest.fixture(scope="module")
def override_text() -> str:
    if not OVERRIDE.exists():
        pytest.skip(f"{OVERRIDE} not mounted in this test environment")
    return OVERRIDE.read_text()


def _extra_servers(override_text: str) -> dict:
    """Extract the AGENT_EXTRA_MCP_SERVERS value from the override's api
    environment and parse it as JSON."""
    m = re.search(r"AGENT_EXTRA_MCP_SERVERS:\s*'(\{.*\})'", override_text, re.DOTALL)
    assert m, "AGENT_EXTRA_MCP_SERVERS missing from docker-compose.override.yml"
    return json.loads(m.group(1))


def test_extra_servers_are_the_two_expected(override_text):
    servers = _extra_servers(override_text)
    assert servers["mcpServers"].keys() == {"fetch", "datasets"}


def test_fetch_server_uses_the_installed_console_script(override_text):
    servers = _extra_servers(override_text)
    assert servers["mcpServers"]["fetch"]["command"] == "mcp-server-fetch"


def test_datasets_server_uses_the_installed_console_script(override_text):
    servers = _extra_servers(override_text)
    assert servers["mcpServers"]["datasets"]["command"] == "ncbi-datasets-mcp"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/test_compose_agent_servers.py -q
```

Expected: FAIL — the override has no `AGENT_EXTRA_MCP_SERVERS` yet (assertion error in `_extra_servers`), and the test file's `REPO_ROOT` resolve is unproven until the mounts below exist. A skip or an assertion failure is a pass for this step.

- [ ] **Step 3: Add the test mounts to the worktree runner**

Edit `backend/run-worktree-tests.sh`, next to the existing mounts:

```bash
  -v "$REPO_ROOT/backend/app:/srv/app" \
  -v "$REPO_ROOT/backend/tests:/srv/tests" \
  -v "$REPO_ROOT/VERSION:/VERSION:ro" \
  -v "$REPO_ROOT/docker-compose.override.yml:/docker-compose.override.yml:ro" \
  -v "$REPO_ROOT/backend/pi-skills:/backend/pi-skills:ro" \
  -v "$DATA_SOURCE:/data" \
```

(The `backend/pi-skills` mount serves Task 3's test.)

- [ ] **Step 4: Add the same mounts and the env wiring to the override**

Edit `docker-compose.override.yml`. In the `api` service `volumes:` block add:

```yaml
      - ./docker-compose.override.yml:/docker-compose.override.yml:ro
      - ./backend/pi-skills:/backend/pi-skills:ro
```

In the `api` service `environment:` block (currently `LOG_LEVEL: DEBUG`) add:

```yaml
      # External stdio MCP servers for the agent, merged into every spawn by
      # AgentService._build_mcp_config(). The commands are the console scripts
      # installed in the image (see backend/Dockerfile). Keep in sync with
      # backend/tests/test_compose_agent_servers.py.
      AGENT_EXTRA_MCP_SERVERS: '{"mcpServers": {"fetch": {"command": "mcp-server-fetch"}, "datasets": {"command": "ncbi-datasets-mcp"}}}'
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/test_compose_agent_servers.py -q
```

Expected: PASS (3 tests). If the JSON in the env string has a typo, this catches it.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.override.yml backend/run-worktree-tests.sh backend/tests/test_compose_agent_servers.py
git commit -m "feat(agent): wire datasets and fetch MCP servers into agent spawns via AGENT_EXTRA_MCP_SERVERS (#83)"
```

---

### Task 3: Skills inventory test (fails until the skills exist)

**Files:**
- Create: `backend/tests/test_pi_skills.py`

**Interfaces:**
- Consumes: `backend/pi-skills/` (mounted at `/backend/pi-skills` in test containers by Task 2) and `app.mcp.server` (import precedent: `backend/tests/mcp/test_server_live.py:65`).
- Produces: the definitive skill inventory — every skill the harness must ship. Task 4 makes it pass.

- [ ] **Step 1: Write the inventory test**

Create `backend/tests/test_pi_skills.py`:

```python
"""Every vendored pi skill must load.

A skill that fails to load is silent: pi starts fine and simply does not
have the workflow, and nothing reports it. This test makes the inventory
explicit -- the expected set below is the contract. Adding a skill means
adding it here AND writing it; the test fails until both exist.
"""

import inspect
import re
from pathlib import Path

import pytest

from app.mcp import server as mcp_server

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "backend" / "pi-skills"

# The curated inventory (spec section "Skill set"). Keep in sync with the
# README -- the test asserts each skill is documented there too.
EXPECTED_SKILLS = {
    "run-qc",
    "interpret-multiqc",
    "suggest-next-steps",
    "debug-failed-job",
    "drive-pipelines",
    "interpret-alignment",
    "variant-analysis",
    "bioflow-database-access",
}


@pytest.fixture(scope="module")
def skills_dir() -> Path:
    if not SKILLS_DIR.exists():
        pytest.skip(f"{SKILLS_DIR} not mounted in this test environment")
    return SKILLS_DIR


def _frontmatter_name(skill_md: str) -> str | None:
    m = re.search(r"^---\n(.*?)\n---", skill_md, re.DOTALL)
    if not m:
        return None
    name = re.search(r"^name:\s*(\S+)", m.group(1), re.MULTILINE)
    return name.group(1) if name else None


def _real_mcp_tools() -> set[str]:
    src = inspect.getsource(mcp_server)
    return set(re.findall(r'name="(bioflow_[a-z_]+)"', src))


def test_inventory_is_complete_and_loads(skills_dir):
    real_tools = _real_mcp_tools()
    readme = (skills_dir / "README.md").read_text()
    for name in EXPECTED_SKILLS:
        md = (skills_dir / name / "SKILL.md").read_text()
        assert _frontmatter_name(md) == name, f"{name}/SKILL.md frontmatter name mismatch"
        assert name in readme, f"{name} not documented in pi-skills/README.md"
        referenced = set(re.findall(r"bioflow_[a-z_]+", md))
        unknown = referenced - real_tools
        assert not unknown, f"{name} references unknown MCP tools: {sorted(unknown)}"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/test_pi_skills.py -q
```

Expected: FAIL — `drive-pipelines`, `interpret-alignment`, `variant-analysis`, and `bioflow-database-access` do not exist yet (FileNotFoundError). This is the red state.

- [ ] **Step 3: Commit the red test**

```bash
git add backend/tests/test_pi_skills.py
git commit -m "test(agent): define the pi-skills inventory contract (#83)"
```

---

### Task 4: Write the four new skills, sharpen debug-failed-job, update the README

**Files:**
- Create: `backend/pi-skills/drive-pipelines/SKILL.md`
- Create: `backend/pi-skills/interpret-alignment/SKILL.md`
- Create: `backend/pi-skills/variant-analysis/SKILL.md`
- Create: `backend/pi-skills/bioflow-database-access/SKILL.md`
- Modify: `backend/pi-skills/debug-failed-job/SKILL.md` (add the "say what to rerun" step)
- Modify: `backend/pi-skills/README.md` (document the full set)

**Interfaces:**
- Consumes: the real MCP tools and job kinds listed in Global Constraints; the guide topics there too.
- Produces: the four new skills + a sharpened debug skill; Task 3's test turns green.

- [ ] **Step 1: Write `drive-pipelines/SKILL.md`**

```markdown
---
name: drive-pipelines
---

# Drive Any BioFlow Pipeline

## When to Use

The user asks to run any pipeline stage -- trim, align, assemble, annotate,
variant calling, anything -- or asks "what can I do with this object?" This
is the general pattern; stage-specific interpretation lives in the
interpret-* skills, not here.

## Procedure

1. Ask BioFlow what can run: `bioflow_suggest_next(object_id)` on the input
   object returns available candidates with a ready-made launch payload.
   Take the payload from there rather than constructing it by hand -- it
   encodes installed tools, existing indexes, and what has already run.
2. Read the workflow guide for the stage: `bioflow_get_guide(topic)`.
   Topics: `getting-started`, `acquiring-data`, `read-qc-and-trimming`,
   `alignment-and-variants`, `de-novo-assembly`, `rna-quantification`.
3. Confirm the inputs: `bioflow_list_objects(project_id)` /
   `bioflow_get_object(object_id)` -- the object must exist and be the right
   kind for the job (e.g. a genome before `build_index`, reads before
   `run_qc`).
4. Launch: `bioflow_run_pipeline(kind=<kind>, params=<payload>)`. The kind
   must come from `bioflow://jobs/types` or the `bioflow_suggest_next`
   payload -- never invented.
5. Poll `bioflow_get_job(job_id)` until it reaches a terminal state. Pipeline
   jobs are asynchronous; a long runtime is normal.
6. Verify the output object appeared and, when the stage has an
   interpretation skill (`interpret-multiqc`, `interpret-alignment`,
   `variant-analysis`), offer it.

## Pitfalls

- Job kinds are ground truth from `bioflow://jobs/types`; `run_qc`,
  `trim_reads`, `build_index`, `align_reads`, `index_bam`, `call_variants`,
  `run_vcf_stats` are examples, not an exhaustive list.
- `bioflow_run_pipeline` returns immediately; the job runs in the
  background. Never report success without polling the job.
- QC (`run_qc`) does not produce a new object -- "no output file" is
  expected there; check the job outcome instead.
```

- [ ] **Step 2: Write `interpret-alignment/SKILL.md`**

```markdown
---
name: interpret-alignment
---

# Interpret an Alignment

## When to Use

After `align_reads` (or `build_index`/`index_bam`), or when the user asks
about mapping quality, coverage, or whether an alignment is good enough for
the next step.

## Procedure

1. Read the alignment results: `bioflow_get_object(object_id)` on the
   alignment/BAM object and the `align_reads` job output.
2. Evaluate mapping rate first: a low fraction of mapped reads points at a
   reference mismatch (wrong organism, contaminated reference, or reads that
   are mostly something else), not at the aligner. High mapping with even
   coverage is the healthy state.
3. Evaluate coverage: how deep and how even. What "enough" means depends on
   the downstream goal -- variant calling needs depth at the sites of
   interest; a low-depth sample changes which variants you can trust.
   Uneven coverage usually means repeats, GC bias, or multi-mapping reads.
4. Check index state: some downstream tools require a sorted and indexed
   BAM. If `index_bam` has not run, `bioflow_suggest_next` on the BAM will
   offer it.
5. Confirm the next step with `bioflow_suggest_next` on the alignment object
   -- variant calling, assembly, or re-aligning with different parameters.

## Pitfalls

- Read the numbers from the object/job output; never guess mapping rate or
  coverage.
- "Low mapping" (reference problem) and "low coverage" (sequencing problem)
  are different failures with different fixes -- name the right one.
- The reference the reads were aligned to matters: a BAM aligned to one
  genome cannot be quietly interpreted against another.
```

- [ ] **Step 3: Write `variant-analysis/SKILL.md`**

```markdown
---
name: variant-analysis
---

# Find Variants in a Gene (or Genome) vs a Reference

## When to Use

The user has reads and wants to know the variants relative to an organism's
reference genome -- especially "are there variations in this gene" or
"what's different between my reads and the reference".

## Procedure

1. Reference first. If the organism's genome is not already a project
   object, search then download it: `bioflow_search_ncbi(term)` to find the
   accession, then `bioflow_download_reference(accession, project_id)`.
   Confirm the assembly actually matches the organism before aligning.
2. Quality-gate the reads if not done: see the `run-qc` skill. QC decides
   whether trimming (`trim_reads`) is warranted before alignment.
3. Align: `build_index` on the genome, then `align_reads`, then `index_bam`
   -- the general pattern is in the `drive-pipelines` skill. Check
   `bioflow_suggest_next` on the reads/genome for the ready-made payloads.
4. Call variants: `call_variants` (kind and payload from
   `bioflow://jobs/types` / `bioflow_suggest_next`).
5. Gene coordinates: when the user named a gene, extract its coordinates
   from the reference's annotation (the GFF object that
   `bioflow_download_reference` registers). Search the annotation for the
   gene name; record chromosome, start, and end. If the gene cannot be
   found in the annotation, say so explicitly -- never invent coordinates.
6. Filter and interpret: use `run_vcf_stats` and the variant job output
   (bcftools csq consequence prediction) to report the variants that fall
   inside the gene's coordinates, with their consequence (missense,
   synonymous, frameshift, ...).
7. Report only variants in the region when the question was about a gene;
   offer the full VCF interpretation if they want the whole genome.

## Pitfalls

- Gene coordinates come from the annotation, never from memory or guessing.
- The reference must match the organism and be the same one the reads were
  aligned to; a variant call against the wrong reference is noise.
- Strand and coordinate system matter when reading a GFF -- report the
  gene's strand when describing variants.
- Reads must pass QC before alignment; skipping it produces variant calls
  nobody can trust.
```

- [ ] **Step 4: Write `bioflow-database-access/SKILL.md`**

```markdown
---
name: bioflow-database-access
---

# Reach Public Bioinformatics Databases

## When to Use

The user needs data from outside BioFlow -- a genome, a sequence, an
assembly, taxonomy, literature -- or asks how to get data into a project.

## Procedure

1. NCBI first, in-app: `bioflow_search_ncbi(term)` then
   `bioflow_download_reference(accession, project_id)`. This is the primary
   path for genomes -- it registers the genome, annotation, protein and CDS
   as project objects, which the rest of the pipeline can use directly.
2. NCBI Datasets browsing: the `datasets` MCP server
   (`ncbi-datasets-mcp`) for assembly/taxonomy discovery and downloads that
   go beyond a single known accession -- `genome_summary_by_taxon`,
   `genome_summary_by_accession`, `taxonomy_summary`, and the download
   tools. It installs the NCBI CLI on first use; the download lands outside
   BioFlow, so register or import the result into the project afterwards.
3. Literature and web content: the `fetch` MCP server for any URL -- PubMed,
   journal pages, Europe PMC, the bioRxiv search API. This is the
   literature-verification path; there is no paid-key skill installed.
4. Beyond NCBI: EBI/Ensembl/UniProt are reachable through their public REST
   APIs via the `fetch` MCP server -- Ensembl REST for genes/transcripts,
   UniProt REST for proteins, EBI search for literature. Fetch the API's
   documented endpoint and parse the JSON response.

## Pitfalls

- Prefer the in-app tools: they register results as project objects. Use
  the external servers only when the in-app path cannot answer.
- Never guess accession formats; get them from a search result.
- The `fetch` server reads web content; for structured databases use their
  documented API endpoints, not a scrape of the HTML site.
- An external download (datasets CLI) does not appear in BioFlow until it
  is imported -- tell the user when a manual import is needed.
```

- [ ] **Step 5: Sharpen `debug-failed-job/SKILL.md` for Job 1**

Edit `backend/pi-skills/debug-failed-job/SKILL.md`. Replace step 3 ("Suggest the concrete fix and offer to retry. If the job is stuck rather than failed, `bioflow_cancel_job(job_id)` may be the right move before relaunching.") with a version that ends by naming the exact rerun:

```markdown
3. Suggest the concrete fix and say exactly what to rerun:
   - **Input problems** -- the fix is on the data side (re-upload,
     re-download, correct params); rerun the same job once the input is
     fixed.
   - **Tool not installed** (`needs_install`) -- check
     `bioflow_list_tools()` for what is missing, install it, then rerun.
   - **Resource limits** (memory/OOM, disk) -- retry with fewer threads or
     free disk; the job may need the input split or the tool's resource
     settings changed.
   - **Tool version mismatch** -- pin or update the tool, then rerun.
   State the job kind and the object to rerun, not just "try again".
4. If the job is stuck rather than failed, `bioflow_cancel_job(job_id)`
   may be the right move before relaunching.
5. For a freshly failed run, check `bioflow_suggest_next` on the input
   object -- it will say whether the input is actually runnable, which
   distinguishes "the tool is broken" from "this object was never runnable".
```

Renumber the remaining steps ("4. For a freshly failed run..." becomes step 5, per the block above) and leave the Pitfalls section unchanged.

- [ ] **Step 6: Update `backend/pi-skills/README.md`**

Extend the "## Skills" list from the current four entries to the full set, with one line each:

```markdown
## Skills

- `run-qc` — assess raw read quality before anything else
- `interpret-multiqc` — explain a QC report in plain terms
- `suggest-next-steps` — what should the user run next
- `debug-failed-job` — diagnose a failed or stuck job, and say what to rerun
- `drive-pipelines` — the general pattern for running any BioFlow pipeline
- `interpret-alignment` — mapping rate, coverage, and whether an alignment is good enough
- `variant-analysis` — variants vs a reference, including a named gene region
- `bioflow-database-access` — how to reach NCBI, EBI/Ensembl/UniProt, and literature

## External servers (installed in the image, wired via AGENT_EXTRA_MCP_SERVERS)

- `fetch` — web content and literature verification (`mcp-server-fetch`)
- `datasets` — NCBI Datasets assembly/taxonomy browsing and downloads (`ncbi-datasets-mcp`)
```

- [ ] **Step 7: Run the inventory test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/test_pi_skills.py -q
```

Expected: PASS. If a skill references a `bioflow_*` name that does not exist in `server.py`, the test names it — fix the skill, not the test.

- [ ] **Step 8: Commit**

```bash
git add backend/pi-skills/
git commit -m "feat(agent): add drive-pipelines, interpret-alignment, variant-analysis, database-access skills; sharpen debug-failed-job (#83)"
```

---

### Task 5: End-to-end acceptance (manual)

**Files:** none (verification only). Runs against a rebuilt stack from this worktree: `./ops/worktree-up.sh` (UI on 5273, API on 8100).

**Interfaces:**
- Consumes: everything from Tasks 1-4 — the rebuilt image, the compose wiring, the installed skills.

- [ ] **Step 1: Bring up the worktree stack and configure a provider**

```bash
./ops/worktree-up.sh
```

In the UI at http://localhost:5273, open Settings → AI, and configure a provider + model for the agent's task slot (the harness has no model otherwise). Confirm the agent drawer connects and responds to a trivial prompt.

- [ ] **Step 2: Verify the extensions and servers are live inside the container**

```bash
docker exec biopipe-<worktree>-api-1 pi list        # all three npm extensions
docker exec biopipe-<worktree>-api-1 sh -c 'command -v mcp-server-fetch && command -v ncbi-datasets-mcp'
```

(The container name follows the worktree project name — check `docker ps` if unsure.)

Then ask the agent: *"use the fetch server to retrieve a page, e.g. https://example.com, and tell me its title"* — it should prove the `fetch` server is wired through the proxy tool.

- [ ] **Step 3: Run acceptance Job 1 — QC debugging**

Upload (or use an existing) read set, run a QC job, and make it fail (e.g. point it at a corrupt/empty FASTQ, or a payload missing a required param). Then ask the agent: *"My QC job failed — why, and what should I rerun?"*

Expected: the agent finds the failed job, reads the error, distinguishes input vs tool vs resource failure, and names the exact rerun (job kind + object), not "try again".

- [ ] **Step 4: Run acceptance Job 2 — variants in a gene**

In a project with a small real read set, ask: *"I have these reads — are there variations in [gene name] compared to the genome of [organism]?"*

Expected: the agent quality-gates/trims, obtains the organism's reference (in-app `bioflow_download_reference` or an existing object), aligns, calls variants, finds the gene's coordinates in the annotation, and reports the variants inside that region with consequences. If the agent reports it cannot complete a step that is genuinely a pipeline capability gap (not a skill gap), **stop and re-scope** — do not paper over it.

- [ ] **Step 5: Verify the skills are discoverable**

Ask the agent: *"which skills do you have available, and when would you use variant-analysis?"* — it should enumerate the eight skills from the README and describe the right trigger for each.

- [ ] **Step 6: Run the full backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: full suite green (baseline 4031 + the ~4 new tests). If the baseline is red, stop and report rather than proceeding.

- [ ] **Step 7: Commit any fixes and record results**

If the e2e run surfaced skill content fixes, amend/extend the Task 4 commit. Note in the commit or a short `docs/nextsteps.md` entry what the manual run proved, and any follow-ups.

---

## After the plan

- **Close the loop on the backlog:** this work is issue #83. When it lands on `main` and the e2e run passes, the spec's "Out of scope / follow-ups" section (context-mode evaluation, bioskill-manager growth path, GEO, bio-mcp-blast, cytado) are candidates for a `docs/TODO.md` entry — create one if any is worth tracking, per the repo's closing-out convention.
- **The `109-add-recommendations` stash is unrelated** to this plan; it stays stashed until that issue is resumed.
