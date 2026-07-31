# Software and Sources Reference Help Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two Help pages -- one documenting every third-party bioinformatics tool BioFlow runs, one documenting the external data sources it draws from -- with tool versions read live from the running container so they never need a manual edit.

**Architecture:** Extend the existing `TOOL_META` catalog in `backend/app/pipelines/tools.py` with bibliographic fields; they reach the API for free through `tool_with_meta()`'s `asdict()` call. Add a parallel static registry for data sources in a new `sources.py` served from `/api/v1/system/sources`. Two React pages render both as scrolling ruled indexes, fetching live so an upgraded binary updates the page with no code change.

**Tech Stack:** Python 3.12 / FastAPI / pytest on the backend; React 18 / TypeScript / TanStack Query / react-router-dom on the frontend. CSS is hand-written in `styles.css` (structure) and `styles/broadsheet.css` (appearance).

---

## Background an engineer needs before starting

**Read the spec first:** `docs/superpowers/specs/2026-07-31-software-reference-help-design.md`.

**How tool data flows today.** `backend/app/pipelines/tools.py` does two separate things:

1. **Probes binaries at runtime** (`_probe`, line 52) for their `--version` output, cached per process with `lru_cache`. A missing binary yields `Tool(path=None, error="...")` and `available` is `False`.
2. **Describes them statically** in `TOOL_META` (line 307), a `dict[str, ToolMeta]` with 17 entries.

`tool_with_meta()` (line 517) merges the two. **Read its docstring before editing** -- it explains that metadata is built with `asdict(meta)` specifically so a new `ToolMeta` field reaches the API without a second edit. That is why this plan adds fields to the dataclass and changes no serializer.

`GET /api/v1/pipelines/tools` (`backend/app/api/v1/pipelines.py:39`) already serves the merged result, and `frontend/src/api/client.ts:313` already has `api.pipelineTools()`. Neither needs changing.

**Two different "is it usable" questions.** `available` means the binary works. `runnable` (a `ToolMeta` field, line 303) means a job handler actually dispatches to it. `cutadapt` and `trimmomatic` are `available: true, runnable: false` -- real working binaries with no code path. The help page must show both states distinctly; see Task 9.

**Theme.** Broadsheet is the only theme as of the 2026-07-30 removal. `frontend/index.html:2` hardcodes `class="theme-broadsheet"` on `<html>`. `styles.css` still provides structural CSS; `broadsheet.css` overrides appearance only. Keep that split.

**Running the app.** From the **main repo root, never a worktree** (see CLAUDE.md -- Compose resolves bind mounts relative to the invocation directory and will silently repoint the shared stack):

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

**Running backend tests:**

```bash
docker compose exec api python -m pytest tests/ -q
```

Use the container, not a host venv -- the host venv hits Mongo replica-set errors.

---

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `backend/app/pipelines/tools.py` | Binary probing + tool catalog | Modify: 6 fields on `ToolMeta`, fill 17 entries, widen fallback dict |
| `backend/app/pipelines/sources.py` | Data source catalog | Create |
| `backend/app/api/v1/system.py` | System endpoints | Modify: add `/sources` |
| `backend/tests/pipelines/test_tools.py` | Tool catalog tests | Modify: completeness test |
| `backend/tests/pipelines/test_sources.py` | Source catalog tests | Create |
| `backend/tests/api/test_sources_api.py` | Endpoint test | Create |
| `frontend/src/api/types.ts` | API types | Modify: widen `PipelineTool`, add `DataSource` |
| `frontend/src/api/client.ts` | API client | Modify: add `sources()` |
| `frontend/src/components/HelpSoftware.tsx` | Software page | Create |
| `frontend/src/components/HelpSources.tsx` | Sources page | Create |
| `frontend/src/App.tsx` | Routing | Modify: two routes |
| `frontend/src/components/Header.tsx` | Help menu | Modify: two entries |
| `frontend/src/styles.css` | Structural CSS | Modify: `.software-*` / `.source-*` |
| `frontend/src/styles/broadsheet.css` | Appearance CSS | Modify: Broadsheet register |

`sources.py` is separate from `tools.py` because that module is about probing binaries, and a source has no binary, no version, and no probe. Both pages are separate components because the spec anticipates sources growing.

---

## Task 1: Add bibliographic fields to `ToolMeta`

**Files:**
- Modify: `backend/app/pipelines/tools.py:283-305` (the `ToolMeta` dataclass), `backend/app/pipelines/tools.py:517-545` (`tool_with_meta` fallback dict)
- Test: `backend/tests/pipelines/test_tools.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/pipelines/test_tools.py`, at the end of the file:

```python
class TestBibliographicFields:
    """The help page's reference data lives on ToolMeta, so it reaches the
    API through tool_with_meta's asdict() with no serializer change."""

    def test_tool_meta_carries_bibliographic_fields(self):
        meta = tools.ToolMeta(
            pipelines=(tools.PipelineType.ALIGN,),
            summary="s",
            strengths=(),
            homepage="https://example.org",
            repository="https://github.com/example/x",
            citation="Author et al., Journal 2020",
            citation_url="https://doi.org/10.0000/x",
            license="MIT",
            usage="Runs when you do the thing.",
        )
        assert meta.homepage == "https://example.org"
        assert meta.repository == "https://github.com/example/x"
        assert meta.citation == "Author et al., Journal 2020"
        assert meta.citation_url == "https://doi.org/10.0000/x"
        assert meta.license == "MIT"
        assert meta.usage == "Runs when you do the thing."

    def test_bibliographic_fields_default_to_empty(self):
        """Constructible without them, so an entry can be filled in
        incrementally rather than all at once."""
        meta = tools.ToolMeta(
            pipelines=(tools.PipelineType.ALIGN,), summary="s", strengths=()
        )
        assert meta.homepage == ""
        assert meta.citation_url == ""
        assert meta.license == ""

    def test_fields_reach_the_api_payload(self):
        """The whole point of putting them on ToolMeta: no serializer edit."""
        tool = tools.Tool(name="fastp", path="/usr/bin/fastp", version="0.24.0")
        payload = tools.tool_with_meta(tool)
        assert payload["homepage"].startswith("http")
        assert payload["license"]
        assert payload["usage"]

    def test_undescribed_tool_gets_empty_bibliographic_fields(self):
        """A tool with no TOOL_META entry must still serialize, with blanks
        rather than a KeyError -- the fallback dict enumerates keys by hand."""
        tool = tools.Tool(name="not-a-real-tool", path="/x", version="1.0")
        payload = tools.tool_with_meta(tool)
        assert payload["homepage"] == ""
        assert payload["citation"] == ""
        assert payload["license"] == ""
        assert payload["usage"] == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools.py::TestBibliographicFields -v
```

Expected: FAIL. The first test errors with `TypeError: ToolMeta.__init__() got an unexpected keyword argument 'homepage'`.

- [ ] **Step 3: Add the fields to the dataclass**

In `backend/app/pipelines/tools.py`, add to `ToolMeta` after the `runnable` field (line 305), keeping the existing fields untouched:

```python
    # --- Reference data for the Software help page. ---
    # These are bibliographic facts about the tool rather than anything the
    # pipeline consults, but they live here because this dict is already the
    # one registry of what a tool *is*. A second catalog keyed by tool name
    # would go stale silently -- a new tool would simply be missing from the
    # help page with nothing failing, which is the trap
    # suggestion_service.py's hand-maintained mapping already set once.
    #
    # All default to "" so an entry stays constructible while it is being
    # filled in. `test_every_tool_is_documented` is what actually requires
    # them, and it deliberately exempts the two that are legitimately absent
    # for some tools (repository, citation_url).
    homepage: str = ""
    repository: str = ""
    citation: str = ""  # human-readable, for a methods section
    citation_url: str = ""
    license: str = ""  # SPDX identifier
    # How *this application* uses the tool -- the one thing here that no
    # upstream page can tell a user. Prose, so nothing can verify it
    # mechanically: describe behaviour, not flags, so it survives a
    # parameter change in the runner.
    usage: str = ""
```

- [ ] **Step 4: Widen the fallback dict**

Still in `tools.py`, in `tool_with_meta()`, the `else` branch (starting line 522) enumerates keys by hand and so does *not* pick up new fields automatically. Add the six keys:

```python
        else {
            "pipelines": (),
            "summary": "",
            "strengths": (),
            "one_liner": "",
            # Absent metadata defaults runnable to False too: a tool this
            # application does not describe is not one it has a code path for
            # either, and offering it as selectable would be worse than
            # omitting the summary text.
            "runnable": False,
            "homepage": "",
            "repository": "",
            "citation": "",
            "citation_url": "",
            "license": "",
            "usage": "",
        }
```

- [ ] **Step 5: Run the tests**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools.py::TestBibliographicFields -v
```

Expected: `test_tool_meta_carries_bibliographic_fields`, `test_bibliographic_fields_default_to_empty`, and `test_undescribed_tool_gets_empty_bibliographic_fields` PASS. `test_fields_reach_the_api_payload` still FAILS -- fastp has no `homepage` value yet. That is correct; Task 3 fills it.

- [ ] **Step 6: Run the full tool suite to check nothing regressed**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools.py tests/pipelines/test_tools_datasets.py -q
```

Expected: all pass except the one known failure above.

- [ ] **Step 7: Commit**

```bash
git add backend/app/pipelines/tools.py backend/tests/pipelines/test_tools.py
git commit -m "feat: add bibliographic fields to ToolMeta

Homepage, repository, citation, license, and a usage note, for the
Software help page. They reach the API through tool_with_meta's asdict
with no serializer change -- the fallback dict is the one place that
enumerates keys by hand and so needed widening."
```

---

## Task 2: Add the completeness test

This is the guard that makes the single-catalog decision pay off. Written before the data so it drives Task 3.

**Files:**
- Test: `backend/tests/pipelines/test_tools.py`

- [ ] **Step 1: Write the failing test**

Add to the `TestBibliographicFields` class:

```python
    def test_every_tool_is_documented(self):
        """Adding a tool without documenting it must fail here rather than
        render a blank help entry.

        repository and citation_url are exempt on purpose: some tools have no
        public repo and some have no paper, and a test that demanded a value
        would only invite a fabricated one.
        """
        required = ("homepage", "citation", "license", "usage")
        missing = {
            name: [f for f in required if not getattr(meta, f)]
            for name, meta in tools.TOOL_META.items()
        }
        missing = {k: v for k, v in missing.items() if v}
        assert not missing, f"undocumented tools: {missing}"

    def test_documented_urls_are_urls(self):
        """A citation string in the homepage field would render as a dead
        link, which is worse than a blank."""
        for name, meta in tools.TOOL_META.items():
            for field in ("homepage", "repository", "citation_url"):
                value = getattr(meta, field)
                if value:
                    assert value.startswith("https://"), (
                        f"{name}.{field} is not a URL: {value!r}"
                    )
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools.py::TestBibliographicFields::test_every_tool_is_documented -v
```

Expected: FAIL, listing all 17 tools with all four fields missing.

- [ ] **Step 3: Commit the failing test**

Commit it now, before the data, so the next task has a clear target.

```bash
git add backend/tests/pipelines/test_tools.py
git commit -m "test: require every tool to carry homepage, citation, license, usage

Fails for all 17 until Task 3 fills them in. This is the forcing
function that keeps the help page honest when a tool is added."
```

---

## Task 3: Research and fill in the 17 tool entries

**This is the largest task and the only one requiring external facts.** It is deliberately not split per-tool: the research is one sustained pass, and a half-filled `TOOL_META` fails the test from Task 2 either way.

**Files:**
- Modify: `backend/app/pipelines/tools.py:307-...` (the `TOOL_META` dict)

**The 15 tools**, verified against `TOOL_META`: `fastp`, `cutadapt`, `trimmomatic`, `fastqc`, `nanoplot`, `fasterq-dump`, `prefetch`, `datasets`, `bwa-mem2`, `minimap2`, `bowtie2`, `hisat2`, `samtools`, `bcftools`, `clair3`.

`bowtie2_build` and `hisat2_build` are probed in `reset_cache()` but have no `TOOL_META` entry. **Do not add entries for them** -- they are the same software as their aligners and would duplicate a citation. Re-confirm the count before starting, in case the catalog has grown:

```bash
docker compose exec api python -c "from app.pipelines.tools import TOOL_META; print(len(TOOL_META)); print(sorted(TOOL_META))"
```

- [ ] **Step 1: Verify each fact against the tool's own repository**

For each tool, find: homepage, repository, citation (paper, human-readable), citation DOI, and SPDX license.

**Verify, do not recall.** Licenses especially -- a wrong license claim is worse than a blank field. Use `WebFetch` or `WebSearch` against the project's own README/LICENSE, not a third-party summary. Some starting points, all of which must still be confirmed:

- Most of these are on GitHub; the repo's `LICENSE` file is authoritative.
- Many have a paper linked from their README.
- `fasterq-dump` and `prefetch` are both part of the **NCBI SRA Toolkit** -- one repository, one license, one citation, two entries. Same for `datasets` (NCBI Datasets).
- `samtools` and `bcftools` are separate repositories under the same htslib org, with different papers.

**Any field that cannot be confirmed stays `""`.** For the four required fields, that means the test from Task 2 keeps failing and you must resolve it -- but resolve it by finding the fact, never by inventing one.

- [ ] **Step 2: Write the `usage` field for each tool**

This one is not researched -- it comes from this codebase. Find how each tool is actually invoked:

```bash
grep -rn "tools\.\(fastp\|samtools\|bcftools\|minimap2\)" backend/app/pipelines/ backend/app/queue/ | head -40
```

Describe **behaviour, not flags** -- one or two sentences. Flags change when a runner is tuned and would silently make the page wrong; behaviour survives.

Good: `"Runs on every alignment job to sort and index the BAM, and produces the flagstat numbers behind the Alignment report."`

Bad: `"Invoked as samtools sort -@ 4 -m 768M."`

For `cutadapt` and `trimmomatic`, which are `runnable: False`, say so plainly: `"Installed and probed, but no trimming job dispatches to it yet -- fastp handles trimming today."`

- [ ] **Step 3: Fill in the entries**

Add the six fields to each `ToolMeta` in `TOOL_META`. Existing fields stay exactly as they are. Example of the finished shape (**the values below are illustrative -- replace every one with what Step 1 actually confirmed**):

```python
    "bwa-mem2": ToolMeta(
        pipelines=(PipelineType.ALIGN,),
        one_liner="Standard short-read aligner for DNA-seq",
        summary=(
            "The standard short-read aligner for human and model organism "
            "genomes. Optimized for Illumina paired-end reads up to ~500 bp."
        ),
        strengths=(
            "Gold standard for Illumina WGS/WES/resequencing",
            "Handles mated reads with proper insert-size modeling",
            "2x faster than original bwa-mem with the same accuracy",
            "x86-64 (prebuilt) and arm64 (sse2neon build) supported",
        ),
        homepage="<confirmed URL>",
        repository="<confirmed URL>",
        citation="<confirmed citation>",
        citation_url="<confirmed DOI>",
        license="<confirmed SPDX id>",
        usage=(
            "<how BioFlow actually uses it, from Step 2>"
        ),
    ),
```

- [ ] **Step 4: Run the completeness test**

```bash
docker compose exec api python -m pytest tests/pipelines/test_tools.py::TestBibliographicFields -v
```

Expected: all six tests PASS, including `test_fields_reach_the_api_payload` and `test_documented_urls_are_urls`.

- [ ] **Step 5: Verify the real payload against the real container**

Per CLAUDE.md, check a rule against real objects, not only its fixtures:

```bash
docker compose exec api python -c "
from app.pipelines.tools import all_tools_with_meta
for t in all_tools_with_meta():
    print(f\"{t['name']:15} {str(t['version']):10} {t['license']:12} {t['homepage'][:45]}\")
"
```

Expected: every row has a license and a homepage, and versions match what is installed.

- [ ] **Step 6: Run the full backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/app/pipelines/tools.py
git commit -m "docs: document homepage, citation, license, and usage for every tool

Each fact verified against the project's own repository rather than
recalled. usage describes behaviour rather than flags so it survives a
parameter change in a runner."
```

---

## Task 4: Create the data source registry

**Files:**
- Create: `backend/app/pipelines/sources.py`
- Test: `backend/tests/pipelines/test_sources.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/pipelines/test_sources.py`:

```python
"""The external data source catalog behind the Sources help page."""

from app.pipelines import sources


class TestSourceCatalog:
    def test_lists_the_sources_the_app_actually_uses(self):
        names = {s.name for s in sources.DATA_SOURCES}
        assert "NCBI Datasets" in names
        assert "NCBI E-utilities" in names
        assert "NCBI Sequence Read Archive" in names

    def test_every_source_is_documented(self):
        """Same forcing function as the tool catalog: a source added without
        a description fails here rather than rendering blank."""
        required = ("name", "kind", "summary", "usage", "homepage")
        missing = {
            s.name: [f for f in required if not getattr(s, f)]
            for s in sources.DATA_SOURCES
        }
        missing = {k: v for k, v in missing.items() if v}
        assert not missing, f"undocumented sources: {missing}"

    def test_urls_are_urls(self):
        for s in sources.DATA_SOURCES:
            for field in ("homepage", "docs", "citation_url", "terms"):
                value = getattr(s, field)
                if value:
                    assert value.startswith("https://"), (
                        f"{s.name}.{field} is not a URL: {value!r}"
                    )

    def test_kind_is_from_the_known_set(self):
        for s in sources.DATA_SOURCES:
            assert s.kind in {"api", "database", "reference"}

    def test_all_sources_serializes_for_the_api(self):
        payload = sources.all_sources()
        assert isinstance(payload, list)
        assert all(isinstance(item, dict) for item in payload)
        assert {"name", "kind", "summary", "usage", "homepage"} <= set(payload[0])

    def test_no_source_claims_a_version(self):
        """Sources have no version -- NCBI Datasets is whatever the API
        returned today. Showing one would be a fabrication."""
        assert all("version" not in item for item in sources.all_sources())
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/pipelines/test_sources.py -v
```

Expected: FAIL with `ImportError: cannot import name 'sources'`.

- [ ] **Step 3: Write the module**

Create `backend/app/pipelines/sources.py`:

```python
"""External data sources, for the Sources help page.

Separate from tools.py because that module is about probing binaries: it
resolves a path, runs `--version`, and caches the answer. None of that
applies here. A data source has no binary to find and no version to report --
NCBI Datasets is whatever the API returned today -- so this is a static
catalog with no probe and no cache.

That absence is deliberate rather than incomplete. The Sources page shows no
version field at all, which is the honest answer; inventing a "retrieved on"
date at render time would look like provenance without being it.

The URLs here duplicate constants in app/metadata/ (assembly.py:28 and
sra.py:28). They are not imported from there because these entries describe
the *service* for a reader, while those constants are request targets -- and
a docs link or a usage-policy link has no business being a live endpoint.
"""

from dataclasses import asdict, dataclass

# Every value the `kind` field may take. The Sources page groups by this, so
# a new value means a new group heading -- keep the set small and meaningful.
SOURCE_KINDS = ("api", "database", "reference")


@dataclass(frozen=True)
class DataSource:
    name: str
    kind: str
    summary: str
    # How BioFlow uses it. Same contract as ToolMeta.usage: behaviour, not
    # endpoints, so it survives a change to the request shape.
    usage: str
    homepage: str
    docs: str = ""
    citation: str = ""
    citation_url: str = ""
    # Usage policy or terms. NCBI asks for rate limits and attribution, and a
    # reference page that omits that is missing the obligation, not just a
    # link.
    terms: str = ""


DATA_SOURCES: tuple[DataSource, ...] = (
    DataSource(
        name="NCBI Datasets",
        kind="api",
        summary=(
            "NCBI's genome assembly delivery service. Serves reference "
            "genome FASTA, annotation, protein and CDS sequences for a "
            "GenBank (GCA) or RefSeq (GCF) accession as a single package."
        ),
        usage=(
            "Backs the reference genome download dialog. BioFlow queries it "
            "for an assembly's contents and size before fetching anything, "
            "so the download is shown before it starts."
        ),
        homepage="https://www.ncbi.nlm.nih.gov/datasets/",
        docs="https://www.ncbi.nlm.nih.gov/datasets/docs/v2/",
        terms="https://www.ncbi.nlm.nih.gov/home/about/policies/",
    ),
    DataSource(
        name="NCBI E-utilities",
        kind="api",
        summary=(
            "The Entrez programmatic interface. Resolves accessions and "
            "returns metadata records across NCBI's databases, including "
            "the SRA's experiment, run, and sample records."
        ),
        usage=(
            "Resolves an SRA accession into its run metadata -- platform, "
            "layout, read counts, and sample fields -- which is what fills "
            "in a downloaded run's metadata without the user typing it."
        ),
        homepage="https://www.ncbi.nlm.nih.gov/books/NBK25501/",
        docs="https://www.ncbi.nlm.nih.gov/books/NBK25499/",
        terms="https://www.ncbi.nlm.nih.gov/home/about/policies/",
    ),
    DataSource(
        name="NCBI Sequence Read Archive",
        kind="database",
        summary=(
            "The public archive of raw sequencing reads. Holds submissions "
            "from Illumina, PacBio, and Oxford Nanopore instruments under "
            "run accessions (SRR, ERR, DRR)."
        ),
        usage=(
            "The source of any run imported through the SRA panel. BioFlow "
            "reads its metadata through E-utilities and fetches the reads "
            "themselves with the SRA Toolkit."
        ),
        homepage="https://www.ncbi.nlm.nih.gov/sra",
        docs="https://www.ncbi.nlm.nih.gov/sra/docs/",
        terms="https://www.ncbi.nlm.nih.gov/home/about/policies/",
    ),
)


def all_sources() -> list[dict]:
    """The catalog as JSON-ready dicts, for the API.

    `asdict` rather than naming fields, matching tool_with_meta: a field
    added to DataSource reaches the API without a second edit here.
    """
    return [asdict(s) for s in DATA_SOURCES]
```

**Before committing, verify the three URLs and the `docs`/`terms` links actually resolve** -- they are the same kind of external fact as Task 3 and get the same treatment. Fix any that 404.

- [ ] **Step 4: Run the tests**

```bash
docker compose exec api python -m pytest tests/pipelines/test_sources.py -v
```

Expected: all six PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipelines/sources.py backend/tests/pipelines/test_sources.py
git commit -m "feat: add a data source catalog for the Sources help page

Static, with no version field: NCBI Datasets is whatever the API
returned today, and a fabricated version would read as provenance
without being it."
```

---

## Task 5: Serve the sources endpoint

**Files:**
- Modify: `backend/app/api/v1/system.py`
- Test: `backend/tests/api/test_sources_api.py`

- [ ] **Step 1: Check the existing API test conventions**

```bash
ls backend/tests/api/ && sed -n 1,30p backend/tests/api/test_system.py 2>/dev/null || sed -n 1,30p "$(ls backend/tests/api/*.py | head -2 | tail -1)"
```

Match whatever client fixture the existing API tests use (likely `client` from `conftest.py`). The test below assumes a `client` fixture returning a `TestClient`; **adjust to match what you find**.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/api/test_sources_api.py`:

```python
"""GET /api/v1/system/sources -- the Sources help page's data."""


class TestSourcesEndpoint:
    def test_returns_the_catalog(self, client):
        resp = client.get("/api/v1/system/sources")
        assert resp.status_code == 200
        body = resp.json()
        assert "sources" in body
        assert len(body["sources"]) >= 3

    def test_entries_carry_what_the_page_renders(self, client):
        body = client.get("/api/v1/system/sources").json()
        first = body["sources"][0]
        for field in ("name", "kind", "summary", "usage", "homepage"):
            assert first[field], f"{field} is empty"
```

- [ ] **Step 3: Run to verify it fails**

```bash
docker compose exec api python -m pytest tests/api/test_sources_api.py -v
```

Expected: FAIL with a 404.

- [ ] **Step 4: Add the endpoint**

In `backend/app/api/v1/system.py`, add the import at the top with the other `app.` imports:

```python
from app.pipelines import sources
```

and the route at the end of the file:

```python
@router.get("/sources")
async def data_sources() -> dict:
    """The external data sources behind the Sources help page.

    On `system` rather than `pipelines` because these are not pipeline
    tools -- nothing here is dispatched to by a job. Static data, so no
    probe and no I/O: unlike /pipelines/tools this cannot be slow and
    cannot fail.
    """
    return {"sources": sources.all_sources()}
```

`system.router` is already registered in `backend/app/api/v1/__init__.py`, so no wiring is needed.

- [ ] **Step 5: Run the tests**

```bash
docker compose exec api python -m pytest tests/api/test_sources_api.py -v
```

Expected: both PASS.

- [ ] **Step 6: Verify against the running API**

```bash
curl -s localhost:8000/api/v1/system/sources | python -m json.tool | head -20
```

Expected: the JSON catalog. If the api container is not running, `docker compose up -d api` first.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/v1/system.py backend/tests/api/test_sources_api.py
git commit -m "feat: serve the data source catalog at /system/sources

On system rather than pipelines: nothing here is a pipeline tool, and
the handler does no probing, so it cannot be slow or fail."
```

---

## Task 6: Frontend types and API client

**Files:**
- Modify: `frontend/src/api/types.ts:355-380`, `frontend/src/api/client.ts`

- [ ] **Step 1: Widen `PipelineTool`**

In `frontend/src/api/types.ts`, add to the `PipelineTool` interface after `runnable` (line 374), keeping the existing fields and their comments:

```typescript
  /**
   * Reference data for the Software help page, from ToolMeta. Any of these
   * may be empty: a tool with no public repository or no paper is a real
   * case, and the page renders the absence rather than a dead link.
   */
  homepage: string;
  repository: string;
  citation: string;
  citation_url: string;
  license: string;
  /** How BioFlow uses this tool -- the part no upstream page can tell you. */
  usage: string;
```

- [ ] **Step 2: Add the `DataSource` types**

Add after the `PipelineTools` interface (line 380):

```typescript
/** An external data source. Mirrors sources.DataSource.
 *
 *  No version field, deliberately: a source has nothing to probe, and
 *  NCBI Datasets is whatever the API returned today. */
export interface DataSource {
  name: string;
  kind: "api" | "database" | "reference";
  summary: string;
  usage: string;
  homepage: string;
  docs: string;
  citation: string;
  citation_url: string;
  terms: string;
}

export interface DataSources {
  sources: DataSource[];
}
```

- [ ] **Step 3: Add the client method**

In `frontend/src/api/client.ts`, beside `systemStats` (line 239):

```typescript
  sources: () => request<DataSources>("/system/sources"),
```

Add `DataSources` to the type import list at the top of the file (match the existing import style).

- [ ] **Step 4: Verify it typechecks**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors. If `tsc` is not available on the host, run in the container: `docker compose exec web npx tsc --noEmit`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat: type the tool bibliography and data source catalog"
```

---

## Task 7: Build the Software help page

**Files:**
- Create: `frontend/src/components/HelpSoftware.tsx`

There is no component-testing setup in this repo and none is expected (CLAUDE.md); verification is manual, in Task 10.

- [ ] **Step 1: Write the component**

Create `frontend/src/components/HelpSoftware.tsx`:

```tsx
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { PipelineTool, PipelineType } from "../api/types";

/**
 * Every third-party tool BioFlow runs, as a reference page.
 *
 * Reads the live catalog rather than a static list, which is the whole
 * point: the version shown is what the container will actually execute, so
 * upgrading a binary in the image updates this page with no edit. A tool
 * that failed to install shows up as a problem here instead of as a
 * confident wrong version.
 *
 * A scrolling ruled index rather than a rail-and-detail browser:
 * PipelineToolSelector already exists for *choosing* a tool to run, and this
 * page's job is to be read and linked into. Broadsheet scrolls the sheet
 * under the masthead, so an internally-paneled layout would fight the theme.
 */

/** Group headings, in reading order. A tool in two pipelines is listed under
 *  the first of these it matches and cross-referenced from the other, since
 *  rendering a long entry twice would misrepresent the page's length. */
const GROUPS: { type: PipelineType; label: string }[] = [
  { type: "qc", label: "Quality control" },
  { type: "trim", label: "Trimming" },
  { type: "align", label: "Alignment" },
  { type: "variant", label: "Variant calling" },
  { type: "download", label: "Data retrieval" },
  { type: "utility", label: "Utilities" },
];

/** The tool's own anchor, for deep links into this page. */
const anchorFor = (name: string) => `tool-${name.replace(/[^a-z0-9]/gi, "-")}`;

export function HelpSoftware() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["pipelines", "tools"],
    queryFn: api.pipelineTools,
  });

  const tools = data?.tools ?? [];

  // First matching group wins, so each tool renders once.
  const groupOf = (tool: PipelineTool) =>
    GROUPS.find((g) => tool.pipelines.includes(g.type))?.type;

  return (
    <div className="help-page software-page">
      <h1>Software</h1>
      <p className="help-intro">
        The third-party tools BioFlow runs on your data, what each one is for,
        and how to cite it.
      </p>
      <p className="software-note">
        Versions are read from the running container, so this page always
        reports what would actually execute.
      </p>

      {isLoading && <p className="software-note">Reading tool versions…</p>}
      {isError && (
        <p className="software-note">
          Could not reach the tool catalog. The descriptions below come from
          the API, so this page needs the backend running.
        </p>
      )}

      {GROUPS.map((group) => {
        const inGroup = tools.filter((t) => groupOf(t) === group.type);
        if (inGroup.length === 0) return null;
        return (
          <section className="software-group" key={group.type}>
            <h2 className="software-group-title">{group.label}</h2>
            {inGroup.map((tool) => (
              <ToolEntry key={tool.name} tool={tool} />
            ))}
          </section>
        );
      })}
    </div>
  );
}

/** One tool: prose on the left, the facts that belong in a methods section
 *  on the right. */
function ToolEntry({ tool }: { tool: PipelineTool }) {
  // Every group this tool belongs to *except* the one it is rendered under,
  // so fastp (trim + qc) says so rather than appearing twice.
  const alsoIn = GROUPS.filter(
    (g) =>
      tool.pipelines.includes(g.type) &&
      g.type !== GROUPS.find((x) => tool.pipelines.includes(x.type))?.type,
  );

  return (
    <article className="software-entry" id={anchorFor(tool.name)}>
      <div className="software-entry-head">
        <h3 className="software-name">{tool.name}</h3>
        <VersionChip tool={tool} />
      </div>

      {alsoIn.length > 0 && (
        <p className="software-also">
          Also used for {alsoIn.map((g) => g.label.toLowerCase()).join(" and ")}.
        </p>
      )}

      <div className="software-entry-body">
        <div className="software-prose">
          {tool.summary && <p>{tool.summary}</p>}

          {tool.usage && (
            <>
              <h4 className="software-label">How BioFlow uses it</h4>
              <p>{tool.usage}</p>
            </>
          )}

          {tool.strengths.length > 0 && (
            <>
              <h4 className="software-label">Strengths</h4>
              <ul className="software-strengths">
                {tool.strengths.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </>
          )}
        </div>

        <aside className="software-facts">
          {tool.license && (
            <div className="software-fact">
              <div className="software-fact-label">License</div>
              <div className="software-fact-value">{tool.license}</div>
            </div>
          )}
          {tool.citation && (
            <div className="software-fact">
              <div className="software-fact-label">Cite as</div>
              <div className="software-fact-value">
                {tool.citation_url ? (
                  <a href={tool.citation_url} target="_blank" rel="noreferrer">
                    {tool.citation}
                  </a>
                ) : (
                  tool.citation
                )}
              </div>
            </div>
          )}
          {/* Each link is conditional: a tool with no public repository is a
              real case, and a dead link is worse than a missing line. */}
          {(tool.homepage || tool.repository) && (
            <div className="software-fact">
              <div className="software-fact-label">Links</div>
              <div className="software-links">
                {tool.homepage && (
                  <a href={tool.homepage} target="_blank" rel="noreferrer">
                    Homepage
                  </a>
                )}
                {tool.repository && tool.repository !== tool.homepage && (
                  <a href={tool.repository} target="_blank" rel="noreferrer">
                    Repository
                  </a>
                )}
              </div>
            </div>
          )}
        </aside>
      </div>
    </article>
  );
}

/**
 * The three states the live probe can distinguish.
 *
 * `available` is whether the binary works; `runnable` is whether any job
 * handler dispatches to it. Conflating them would tell a reader a tool is
 * ready when nothing calls it -- which is true of cutadapt and Trimmomatic
 * today. A static page could not express this at all.
 */
function VersionChip({ tool }: { tool: PipelineTool }) {
  if (!tool.available) {
    return (
      <span className="software-version missing" title={tool.error ?? undefined}>
        not installed
      </span>
    );
  }
  return (
    <>
      <span className="software-version">{tool.version ?? "installed"}</span>
      {!tool.runnable && (
        <span className="software-version pending">not yet wired up</span>
      )}
    </>
  );
}
```

- [ ] **Step 2: Verify it typechecks**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors. If `PipelineType` is not exported from `types.ts`, check its definition and adjust the import -- it is referenced at `types.ts:362`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/HelpSoftware.tsx
git commit -m "feat: add the Software reference page

Renders the live tool catalog, so a version here is what the container
would actually run. Distinguishes not-installed from installed-but-not-
wired-up, which a static page could not express."
```

---

## Task 8: Build the Sources help page

**Files:**
- Create: `frontend/src/components/HelpSources.tsx`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/HelpSources.tsx`:

```tsx
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { DataSource } from "../api/types";

/**
 * The external services BioFlow draws data from.
 *
 * A sibling of the Software page rather than a section of it: sources are
 * expected to grow, and they carry a different set of facts -- no version,
 * no license, but a usage policy that the tools do not have.
 *
 * No version is shown anywhere on this page. NCBI Datasets is whatever the
 * API returned today; a version or a retrieval date rendered here would look
 * like provenance without being it.
 */

const GROUPS: { kind: DataSource["kind"]; label: string }[] = [
  { kind: "api", label: "Programmatic interfaces" },
  { kind: "database", label: "Archives and databases" },
  { kind: "reference", label: "Reference data" },
];

export function HelpSources() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["system", "sources"],
    queryFn: api.sources,
  });

  const sources = data?.sources ?? [];

  return (
    <div className="help-page software-page">
      <h1>Data sources</h1>
      <p className="help-intro">
        The external services BioFlow retrieves data from, what each provides,
        and the terms it is provided under.
      </p>

      {isLoading && <p className="software-note">Loading…</p>}
      {isError && (
        <p className="software-note">Could not reach the source catalog.</p>
      )}

      {GROUPS.map((group) => {
        const inGroup = sources.filter((s) => s.kind === group.kind);
        if (inGroup.length === 0) return null;
        return (
          <section className="software-group" key={group.kind}>
            <h2 className="software-group-title">{group.label}</h2>
            {inGroup.map((source) => (
              <SourceEntry key={source.name} source={source} />
            ))}
          </section>
        );
      })}
    </div>
  );
}

function SourceEntry({ source }: { source: DataSource }) {
  return (
    <article className="software-entry">
      <div className="software-entry-head">
        <h3 className="software-name">{source.name}</h3>
      </div>

      <div className="software-entry-body">
        <div className="software-prose">
          <p>{source.summary}</p>
          <h4 className="software-label">How BioFlow uses it</h4>
          <p>{source.usage}</p>
        </div>

        <aside className="software-facts">
          {source.citation && (
            <div className="software-fact">
              <div className="software-fact-label">Cite as</div>
              <div className="software-fact-value">
                {source.citation_url ? (
                  <a href={source.citation_url} target="_blank" rel="noreferrer">
                    {source.citation}
                  </a>
                ) : (
                  source.citation
                )}
              </div>
            </div>
          )}
          <div className="software-fact">
            <div className="software-fact-label">Links</div>
            <div className="software-links">
              <a href={source.homepage} target="_blank" rel="noreferrer">
                Homepage
              </a>
              {source.docs && (
                <a href={source.docs} target="_blank" rel="noreferrer">
                  Documentation
                </a>
              )}
              {source.terms && (
                <a href={source.terms} target="_blank" rel="noreferrer">
                  Usage policy
                </a>
              )}
            </div>
          </div>
        </aside>
      </div>
    </article>
  );
}
```

- [ ] **Step 2: Verify it typechecks**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/HelpSources.tsx
git commit -m "feat: add the Data sources reference page

A sibling of Software rather than a section: sources carry a usage
policy and no version, and are expected to grow."
```

---

## Task 9: Wire up routes and the Help menu

**Files:**
- Modify: `frontend/src/App.tsx:8` (imports), `frontend/src/App.tsx:67` (routes)
- Modify: `frontend/src/components/Header.tsx:17-19`

- [ ] **Step 1: Add the imports and routes**

In `frontend/src/App.tsx`, add beside the existing `HelpCalculations` import (line 8):

```typescript
import { HelpSoftware } from "./components/HelpSoftware";
import { HelpSources } from "./components/HelpSources";
```

and beside the existing help route (line 67):

```tsx
          <Route path="/help/calculations" element={<HelpCalculations />} />
          <Route path="/help/software" element={<HelpSoftware />} />
          <Route path="/help/sources" element={<HelpSources />} />
```

No layout change is needed: `App.tsx:46` already gives any `/help/` path the single-column treatment via `pathname.startsWith("/help/")`.

- [ ] **Step 2: Add the menu entries**

In `frontend/src/components/Header.tsx`, replace the `HELP_ITEMS` array (lines 16-19). The comment about "one entry today" is now stale:

```typescript
/** Help menu contents. */
const HELP_ITEMS: { to: string; label: string }[] = [
  { to: "/help/calculations", label: "BioFlow Calculations" },
  { to: "/help/software", label: "Software" },
  { to: "/help/sources", label: "Data Sources" },
];
```

- [ ] **Step 3: Verify it typechecks**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/App.tsx frontend/src/components/Header.tsx
git commit -m "feat: route the Software and Data Sources help pages"
```

---

## Task 10: Style both pages

**Files:**
- Modify: `frontend/src/styles.css` (after the `.help-table` block, around line 2270)
- Modify: `frontend/src/styles/broadsheet.css` (append a new section)

Structural rules go in `styles.css`, appearance in `broadsheet.css`. Broadsheet is the only theme now, but that split is what keeps `broadsheet.css` a coherent override layer -- do not collapse it.

- [ ] **Step 1: Add the structural CSS**

In `frontend/src/styles.css`, after the `.help-table th` rule (ends line 2271):

```css
/* Software and Sources reference pages ---------------------------------- */

/* Wider than .help-page's 760px: these carry a facts rail beside the prose,
   and 760 would squeeze both. The prose column is measure-constrained on its
   own below, so line length stays readable at this width. */
.software-page {
  max-width: 1020px;
}

.software-note {
  color: var(--text-dim);
  font-size: 13px;
  margin: 0 0 20px;
}

.software-group {
  margin-top: 32px;
}

.software-group-title {
  font-size: 15px;
  margin: 0 0 4px;
  padding-bottom: 6px;
  border-bottom: 2px solid var(--text);
}

.software-entry {
  padding: 20px 0;
  border-bottom: 1px solid var(--border);
  scroll-margin-top: 24px;
}

.software-entry-head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 6px;
}

.software-name {
  font-size: 18px;
  margin: 0;
}

.software-version {
  font-family: var(--mono);
  font-size: 11px;
  padding: 2px 7px;
  /* --radius, not --radius-sm: styles.css defines only --radius, and a
     declaration referencing an undefined variable is dropped silently --
     the same trap a nonexistent --space-5 already set in broadsheet.css.
     Broadsheet overrides this with its own --radius-sm below, which it
     does define. */
  border-radius: var(--radius);
  background: var(--bg-elevated);
  color: var(--text-dim);
}

.software-also {
  font-size: 12.5px;
  color: var(--text-dim);
  margin: 0 0 8px;
}

/* auto-fit rather than a media query, following .detail-columns: the rail
   drops under the prose when the window is too narrow to hold both. */
.software-entry-body {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 28px;
  align-items: start;
}

.software-prose {
  max-width: 62ch;
}

.software-prose p {
  line-height: 1.6;
  margin: 0 0 10px;
}

.software-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-dim);
  margin: 16px 0 6px;
}

.software-strengths {
  padding-left: 18px;
  margin: 0;
}

.software-strengths li {
  line-height: 1.55;
  margin-bottom: 4px;
}

.software-facts {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.software-fact-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-dim);
  margin-bottom: 2px;
}

.software-fact-value {
  font-size: 13.5px;
  line-height: 1.45;
}

.software-links {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 13.5px;
}
```

- [ ] **Step 2: Add the Broadsheet appearance rules**

Append to `frontend/src/styles/broadsheet.css`:

```css
/* ── Software and Sources reference pages ───────────────────────────────── */

/* These pages are the app's most literally editorial surface: a ruled index
   in a printed reference. The register follows the detail pane -- italic
   serif subject, letterspaced labels, hairline rules -- so they read as the
   same publication rather than as documentation bolted on. */

.theme-broadsheet .software-group-title {
  font-family: var(--font-heading);
  font-size: 12px;
  font-weight: 400;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-62);
  border-bottom: 2px solid var(--color-text);
  padding-bottom: var(--space-1);
  margin-bottom: var(--space-2);
}

/* Italic for the same reason .detail-title is: the subject of the entry. */
.theme-broadsheet .software-name {
  font-family: var(--font-heading);
  font-size: 22px;
  font-weight: 600;
  font-style: italic;
  letter-spacing: -0.015em;
}

.theme-broadsheet .software-entry {
  border-bottom: 1px solid var(--color-divider);
  padding: var(--space-4) 0;
}

.theme-broadsheet .software-version {
  background: var(--color-accent-200);
  color: var(--color-accent-800);
  border-radius: var(--radius-sm);
}

/* The two states that are news rather than routine. `missing` reuses the
   accent-2 tint the badge rules already give failures, so a not-installed
   tool reads the same way a failed job does. */
.theme-broadsheet .software-version.missing {
  background: var(--color-accent-2-200);
  color: var(--color-accent-2-800);
}

/* Installed but nothing dispatches to it: not a failure, so neutral ink
   rather than the failure tint -- it is a note, not a problem. */
.theme-broadsheet .software-version.pending {
  background: var(--color-neutral-200);
  color: var(--color-neutral-800);
}

.theme-broadsheet .software-label,
.theme-broadsheet .software-fact-label {
  font-family: var(--font-body);
  font-size: 11px;
  font-weight: 400;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-62);
}

.theme-broadsheet .software-prose p,
.theme-broadsheet .software-strengths li {
  color: var(--ink-78);
  text-wrap: pretty;
}

/* The rail is a sidebar in the printed sense: separated by a rule, not a
   box. A card here would be the only rounded surface on the page. */
.theme-broadsheet .software-facts {
  border-left: 1px solid var(--color-divider);
  padding-left: var(--space-3);
}

.theme-broadsheet .software-note {
  color: var(--ink-62);
  font-style: italic;
}
```

- [ ] **Step 3: Rebuild and look at it**

From the main repo root:

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker
```

Then open http://localhost:5173/help/software.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/styles.css frontend/src/styles/broadsheet.css
git commit -m "style: give the reference pages a ruled editorial register"
```

---

## Task 11: Verify against the running app

Manual testing in the browser is the actual verification step for anything UI-facing in this repo (CLAUDE.md). Nothing here is optional.

- [ ] **Step 1: Confirm the stack is serving the right source tree**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

If any path contains `.claude/worktrees/`, the shared stack is pointed at a worktree. Fix by rebuilding from the main repo root before trusting anything below.

- [ ] **Step 2: Run the full backend suite**

```bash
docker compose exec api python -m pytest tests/ -q
```

Expected: all pass. Record the actual output -- do not claim a pass without it.

- [ ] **Step 3: Check both pages load**

Open each and confirm it renders:
- http://localhost:5173/help/software
- http://localhost:5173/help/sources

Then reach both from the Help menu in the masthead, confirming the two new entries appear.

- [ ] **Step 4: Verify versions against the container**

Pick three tools from the page and check the page's number against the binary:

```bash
docker compose exec api fastp --version
docker compose exec api samtools --version | head -1
docker compose exec api minimap2 --version
```

Expected: exact match with the version chips. A mismatch means the probe or the page is wrong, not that the page needs a manual edit.

- [ ] **Step 5: Verify the not-yet-wired-up state**

`cutadapt` and `trimmomatic` are `runnable: false`. Confirm both show the "not yet wired up" chip alongside a real version, not a bare version and not "not installed".

```bash
docker compose exec api python -c "
from app.pipelines.tools import all_tools_with_meta
for t in all_tools_with_meta():
    if not t['runnable']:
        print(t['name'], t['available'], t['version'])
"
```

- [ ] **Step 6: Verify the not-installed state against a real absence**

Find whether any tool is genuinely missing from the image:

```bash
docker compose exec api python -c "
from app.pipelines.tools import all_tools_with_meta
missing = [t['name'] for t in all_tools_with_meta() if not t['available']]
print('missing:', missing or 'none')
"
```

If any are missing, confirm the page shows the "not installed" chip for exactly those. If none are missing, the image ships everything -- per the CLAUDE.md warning about tests that pass whether or not the patch worked, do not assume the branch works because nothing exercised it. Temporarily set a bogus path to force the state:

```bash
docker compose exec -e FASTP_PATH=/nonexistent api python -c "
from app.pipelines import tools
tools.reset_cache()
print(tools.tool_with_meta(tools.fastp())['available'])
"
```

Expected: `False`. Confirm the page's rendering of that state by whatever means is available -- the point is to see the branch render, not to trust it.

- [ ] **Step 7: Check the responsive collapse**

Narrow the browser window until the facts rail drops below the prose. Confirm it collapses cleanly with no overlap and no horizontal scroll.

- [ ] **Step 8: Click every external link**

Every homepage, repository, citation, documentation, and usage-policy link on both pages. A dead link here is a wrong fact, and this is the only step that catches a typo'd URL.

- [ ] **Step 9: Commit any fixes**

```bash
git add -A
git commit -m "fix: <what the manual pass turned up>"
```

If nothing needed fixing, skip this step rather than making an empty commit.

---

## Task 12: Record the maintenance contract

The one thing about this feature that a future reader cannot infer from the code: adding a tool now requires documenting it, and the test that enforces this will fail in a way that looks unrelated.

**Files:**
- Modify: `CLAUDE.md` (the "Adding a pipeline tool" section)

- [ ] **Step 1: Extend the existing section**

`CLAUDE.md` already has an "Adding a pipeline tool" section explaining that registering a tool in `tools.py` is only half the change. Add a third requirement to it:

```markdown
Third, the Software help page renders `TOOL_META` directly, and
`test_every_tool_is_documented` requires every entry to carry `homepage`,
`citation`, `license`, and `usage`. A new tool fails that test until those
are filled in -- which is the point, since the alternative is a help page
that silently omits it.

Verify the license and citation against the project's own repository rather
than recalling them. A wrong license claim on a page that reads as
authoritative is worse than a blank field, and `repository` and
`citation_url` are deliberately *not* required so that a tool with no public
repo or no paper does not invite a fabricated one.

`usage` describes how BioFlow uses the tool. Write behaviour, not flags:
flags change whenever a runner is tuned, and nothing can mechanically catch
a `usage` string that has gone stale.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: note that a new tool must be documented for the help page

The completeness test fails in a way that looks unrelated to the tool
being added, so the requirement belongs where tool-adding is described."
```

---

## Self-review notes

**Spec coverage.** Every section of the spec maps to a task: `ToolMeta` extension (1), completeness test (2), the research pass (3), sources registry (4), endpoint (5), types (6), the two pages (7, 8), routing (9), styling (10), verification (11). Task 12 is an addition — the spec implies the maintenance contract but does not say where it gets written down, and CLAUDE.md is where this repo records exactly this kind of trap.

**A count corrected in both documents.** The spec originally said "17 tools"; `TOOL_META` has 15, the extra two being `bowtie2_build` and `hisat2_build`, which are probed but have no metadata entry and should not get one — they are the same software as their aligners. Verified with `grep -c '": ToolMeta('` and corrected in the spec.

**Deliberate gaps.** `repository` and `citation_url` are excluded from the completeness test, and both pages render every link conditionally. This is the mechanism that lets an unverifiable fact stay blank instead of becoming a guess.
