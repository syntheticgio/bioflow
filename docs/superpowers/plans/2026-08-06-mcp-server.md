# MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose an MCP server from the running BioFlow API at `/api/v1/mcp` so an AI coding agent can drive the platform, with curated tools and drift-tested workflow guides.

**Architecture:** The MCP server mounts in-process on the existing FastAPI app. Tool functions call the service layer (`project_service`, `object_service`, `suggestion_service`, `queue.enqueue`) directly — the same layer `app/api/v1/` routers call. The profile is supplied as a `?profile=` query parameter and resolved through the existing `deps.resolve_owner`. Documentation splits into derived resources (OpenAPI, `TOOL_META`, `sources.py`, `all_handlers()`) that cannot drift, and hand-written guides that are drift-tested against real symbols.

**Tech Stack:** Python 3.12, FastAPI, `mcp` 2.0.0 (Streamable HTTP ASGI transport), Beanie/Motor, pytest, React 18 + Zustand (frontend panel).

**Spec:** `docs/superpowers/specs/2026-08-06-mcp-server-design.md`

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `backend/app/mcp/__init__.py` | Package marker; re-exports `build_mcp_app` |
| `backend/app/mcp/context.py` | Profile→owner resolution for MCP requests; the single seam every tool goes through |
| `backend/app/mcp/server.py` | Constructs the MCP server, registers tools/resources, returns the mountable ASGI app |
| `backend/app/mcp/tools.py` | The 16 tool functions |
| `backend/app/mcp/resources.py` | `GuideTopic` enum, guide loading, derived resource builders |
| `backend/app/mcp/guides/*.md` | Six hand-written workflow guides |
| `backend/tests/mcp/__init__.py` | Test package marker |
| `backend/tests/mcp/conftest.py` | Fixtures: owner, mounted app |
| `backend/tests/mcp/test_context.py` | Profile resolution and fallback |
| `backend/tests/mcp/test_tools.py` | Per-tool behaviour and owner scoping |
| `backend/tests/mcp/test_guides.py` | The four drift tests |
| `backend/tests/mcp/test_surface.py` | Not-exposed list stays not-exposed |
| `frontend/src/components/SettingsMcp.tsx` | The copy-paste connection panel |

**Modified:**

| Path | Change |
|---|---|
| `backend/pyproject.toml` | Add `mcp>=2.0,<3` dependency |
| `backend/app/main.py` | Mount the MCP app in `create_app()` |
| `frontend/src/components/SettingsNav.tsx` | Add third nav item |
| `frontend/src/App.tsx` | Add `/settings/mcp` route |

**Why `context.py` is separate from `tools.py`:** owner resolution is the one thing every tool must not get wrong, and the surface test in Task 12 asserts against it. Keeping it in its own module means a tool that forgets to resolve an owner is a visible import omission rather than a missing line buried in a 400-line file.

---

## Task 1: Add the MCP dependency

**Files:**
- Modify: `backend/pyproject.toml:6-31`

- [ ] **Step 1: Add the dependency**

In `backend/pyproject.toml`, in the `dependencies` list, after the `"packaging>=24",` line, add:

```toml
    # MCP server, mounted at /api/v1/mcp. Pinned below 3 because the
    # Streamable HTTP transport's ASGI surface is what app/mcp/server.py
    # mounts, and a major bump is where that would change.
    "mcp>=2.0,<3",
```

- [ ] **Step 2: Rebuild the API image so the dependency is present**

Run from the **main checkout root** (not this worktree):

```bash
docker compose up -d --build api worker
```

Expected: both containers rebuild and come up healthy.

- [ ] **Step 3: Verify the import works**

```bash
docker compose exec api python -c "import mcp; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add backend/pyproject.toml
git commit -m "build: add mcp dependency for the in-process MCP server (#31)"
```

---

## Task 2: Profile resolution context

**Files:**
- Create: `backend/app/mcp/__init__.py`
- Create: `backend/app/mcp/context.py`
- Create: `backend/tests/mcp/__init__.py`
- Create: `backend/tests/mcp/conftest.py`
- Test: `backend/tests/mcp/test_context.py`

- [ ] **Step 1: Create the package markers**

`backend/app/mcp/__init__.py`:

```python
"""The MCP server BioFlow exposes at /api/v1/mcp.

Mounted in-process on the existing FastAPI app rather than run as its own
service: see docs/superpowers/specs/2026-08-06-mcp-server-design.md. Tool
functions call the service layer directly, which is what keeps a future split
into a separate container a change to those calls rather than to the tool
surface an agent sees.
"""
```

`backend/tests/mcp/__init__.py`: (empty file)

- [ ] **Step 2: Write the failing test**

`backend/tests/mcp/test_context.py`:

```python
"""Profile resolution for MCP requests.

The MCP transport has no startup picker and cannot send the X-BioFlow-Profile
header the web UI uses, so the profile arrives as a query parameter -- the
same accommodation `deps.resolve_owner` already makes for the SSE stream.
"""

import pytest

from app.errors import ProfileUnresolvedError
from app.mcp import context
from app.services import profile_service


async def test_explicit_profile_resolves_to_its_owner():
    profile = await profile_service.create_profile(username="mcp-explicit")

    owner = await context.owner_for(str(profile.id))

    assert owner == profile.owner_id()


async def test_absent_profile_falls_back_to_the_only_profile():
    """A single-person install should not need the query string at all.

    This cannot guess wrong: there is nothing to guess between.
    """
    profile = await profile_service.create_profile(username="mcp-sole")

    owner = await context.owner_for(None)

    assert owner == profile.owner_id()


async def test_absent_profile_with_two_profiles_names_the_parameter():
    await profile_service.create_profile(username="mcp-ambiguous-a")
    await profile_service.create_profile(username="mcp-ambiguous-b")

    with pytest.raises(ProfileUnresolvedError) as exc:
        await context.owner_for(None)

    # The message has to say what to add and where to get it: an agent reads
    # this string to decide what to do next, and "no profile" alone tells it
    # nothing actionable.
    assert "?profile=" in str(exc.value)


async def test_unknown_profile_is_rejected():
    with pytest.raises(ProfileUnresolvedError):
        await context.owner_for("507f1f77bcf86cd799439011")
```

`backend/tests/mcp/conftest.py`:

```python
"""Fixtures for the MCP tests.

Profiles are created per-test by the tests that need them rather than by a
shared fixture: `tests/api/conftest.py`'s `two_profiles` docstring records why
a fixture that deletes broadly is hazardous here, and the MCP tests need
different profile counts per case (zero, one, two) which a single fixture
cannot express.
"""

import pytest_asyncio

from app.models import Profile


@pytest_asyncio.fixture(autouse=True)
async def clean_profiles():
    """Each MCP test starts with no profiles.

    Required rather than convenient: `owner_for(None)` branches on how many
    profiles exist, so a row left behind by a neighbouring test changes what
    this module's fallback tests assert.
    """
    await Profile.find_all().delete()
    yield
    await Profile.find_all().delete()
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_context.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.mcp.context'`

- [ ] **Step 4: Write the implementation**

`backend/app/mcp/context.py`:

```python
"""Turning an MCP request's `?profile=` into an `owner` string.

Every tool goes through `owner_for`. It is deliberately the only way an owner
enters the MCP package: a tool that forgets to call it cannot silently read
another profile's library, because it has no other source for the value.

The fallback to a sole profile is not a convenience shortcut. On a
single-person install -- the common case for this application -- there is
exactly one possible answer, and requiring the query string there would mean
the paste-ready URL in the settings panel is the only way to connect at all.
Where the answer is genuinely ambiguous, this raises instead of picking.
"""

from app.api.deps import resolve_owner
from app.errors import ProfileUnresolvedError
from app.models import Profile


async def owner_for(profile_param: str | None) -> str:
    """The `owner` this MCP request acts as.

    `profile_param` is the raw `?profile=` value, or None when absent.
    """
    if profile_param:
        # resolve_owner already handles "local", malformed ids and unknown
        # ids, raising ProfileUnresolvedError for each. Reusing it is what
        # keeps one definition of what a profile id means.
        return await resolve_owner(profile_param)

    profiles = await Profile.find_all().limit(2).to_list()

    if len(profiles) == 1:
        return profiles[0].owner_id()

    if not profiles:
        raise ProfileUnresolvedError(
            "No profiles exist yet. Create one in BioFlow first, then copy the "
            "MCP connection URL from Settings > MCP."
        )

    raise ProfileUnresolvedError(
        "More than one profile exists, so the MCP connection must say which "
        "one to use. Add ?profile=<id> to the server URL -- Settings > MCP in "
        "BioFlow shows the ready-to-paste URL for the current profile."
    )
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_context.py -q
```

Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/mcp/ backend/tests/mcp/
git commit -m "feat(mcp): resolve the acting profile from ?profile= (#31)"
```

---

## Task 3: Guide topics and loading

**Files:**
- Create: `backend/app/mcp/resources.py`
- Create: `backend/app/mcp/guides/getting-started.md`
- Test: `backend/tests/mcp/test_guides.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/mcp/test_guides.py`:

```python
"""The guides, and the tests that keep them true.

Hand-written prose about code goes stale silently. This repo has been bitten
three times -- the 2026-07-31 TODO audit, the `ToolMeta.runnable` comment
citing cutadapt years after `trim_reads` grew its dispatch, and
`results._SIDECAR_ROLES` dropping STAR's index files with the suite green
throughout. A guide that confidently names a tool which no longer exists is
worse than no guide, because the entire point of the feature is telling an
agent what is true.

These tests are why guides must name symbols as backticked literals rather
than paraphrase: prose saying "the alignment job" instead of `align` is
invisible here and free to rot.
"""

from app.mcp import resources


def test_every_topic_has_a_file():
    for topic in resources.GuideTopic:
        assert resources.load_guide(topic).strip(), f"{topic} is empty"


def test_every_file_has_a_topic():
    """The other direction: a stray .md is a guide nothing can reach.

    `set(enum) == set(files)` is the exhaustiveness pattern CLAUDE.md names as
    the one to copy, and this is the half that catches a file added without a
    topic to serve it.
    """
    on_disk = {p.stem for p in resources.GUIDES_DIR.glob("*.md")}
    declared = {t.value for t in resources.GuideTopic}

    assert on_disk == declared
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_guides.py -q
```

Expected: FAIL with `ImportError: cannot import name 'resources'` or `AttributeError: GuideTopic`

- [ ] **Step 3: Write the implementation**

`backend/app/mcp/resources.py`:

```python
"""Documentation the MCP server serves.

Split by whether the content can go stale. The derived resources (OpenAPI,
TOOL_META, sources, job types) are generated from the code they describe and
cannot drift. The guides are hand-written, and `tests/mcp/test_guides.py` is
what keeps them honest.
"""

from enum import StrEnum
from pathlib import Path

GUIDES_DIR = Path(__file__).parent / "guides"


class GuideTopic(StrEnum):
    """The workflow guides, one per path a user actually walks.

    Members are the valid arguments to `bioflow_get_guide`, and each must have
    a matching `<value>.md` in GUIDES_DIR -- asserted both directions in
    tests/mcp/test_guides.py.
    """

    GETTING_STARTED = "getting-started"


def load_guide(topic: GuideTopic) -> str:
    return (GUIDES_DIR / f"{topic.value}.md").read_text()
```

`backend/app/mcp/guides/getting-started.md`:

```markdown
# Getting started with BioFlow

BioFlow manages bioinformatics data and runs pipelines over it. This guide
covers the shape of the system; the other guides cover specific workflows.

## Profiles

Every piece of data belongs to a **profile**. The MCP connection is already
acting as one -- call `bioflow_whoami` to see which. You cannot switch
profiles from here; that is chosen by the human when they configure this
server.

## Projects

A **project** holds data objects and can nest inside another project. Create
one with `bioflow_create_project` before adding data. List them with
`bioflow_list_projects`.

## Objects

An **object** is a file BioFlow knows something about -- reads, a reference
genome, an alignment, a variant call set. Its `format` and `role` are detected
on ingest and drive what can be run against it.

Find them with `bioflow_list_objects` (by project) or `bioflow_search_objects`
(across the library).

## The most useful call

`bioflow_suggest_next(object_id)` asks BioFlow itself what can be run against
an object right now. It returns each candidate with a status -- `available`,
`unavailable`, or `needs_install` -- a ready-made launch payload, and the
honest reason anything unavailable cannot run.

Prefer it over reasoning from these guides. It is computed from the actual
object, so it accounts for what is installed on this machine, whether a
reference has an index, and what has already been run.

## Running something

`bioflow_run_pipeline(kind, params)` starts a job. The `kind` values are the
registered job types -- read them from the `bioflow://jobs/types` resource, or
take the payload straight from `bioflow_suggest_next`.

Jobs are asynchronous. `bioflow_run_pipeline` returns immediately with a job
id; poll `bioflow_get_job` for progress. Long pipelines can run for hours.
`bioflow_cancel_job` stops one.

## What this server will not do

There are no delete tools. Removing a project or an object is done by the
human in the BioFlow UI.
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_guides.py -q
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/mcp/resources.py backend/app/mcp/guides/ backend/tests/mcp/test_guides.py
git commit -m "feat(mcp): guide topics with exhaustiveness tests (#31)"
```

---

## Task 4: Symbol drift tests

**Files:**
- Modify: `backend/tests/mcp/test_guides.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/mcp/test_guides.py`:

```python
import re

import pytest

from app.main import app
from app.pipelines.tools import TOOL_META
from app.queue.handlers import all_handlers


def _backticked(text: str) -> set[str]:
    """Every `literal` in the guide.

    Backticks are the marker that says "this is a real symbol, check it".
    Anything a guide wants to say without being checked it simply writes
    without them.
    """
    return set(re.findall(r"`([^`\n]+)`", text))


def _all_guide_symbols() -> set[str]:
    symbols: set[str] = set()
    for topic in resources.GuideTopic:
        symbols |= _backticked(resources.load_guide(topic))
    return symbols


def test_job_type_names_are_real():
    """A guide naming a job type that isn't registered would send an agent to
    `bioflow_run_pipeline` with a kind that can never run."""
    registered = set(all_handlers())
    # Only check symbols that look like job types -- lowercase words with
    # underscores. A guide also backticks tool names, paths and parameters,
    # and those are checked by their own tests below.
    candidates = {s for s in _all_guide_symbols() if re.fullmatch(r"[a-z][a-z0-9_]+", s)}

    unknown = {c for c in candidates if c not in registered and c not in TOOL_META}
    # Names that are neither a job type nor a tool are allowed only if they
    # are on this list, which exists so a guide can say `format` or `role`
    # without inventing a checkable symbol for it.
    allowed_prose = {
        "format",
        "role",
        "available",
        "unavailable",
        "needs_install",
        "kind",
        "params",
        "object_id",
    }

    assert unknown <= allowed_prose, f"Guides name unknown symbols: {unknown - allowed_prose}"


def test_tool_names_are_real():
    """Every bioinformatics tool a guide names must be in TOOL_META.

    This is the `runnable`-comment failure made loud: that comment cited
    cutadapt and Trimmomatic for years after they stopped being what
    `trim_reads` dispatched to, and nothing failed because a comment cannot.

    Checked from a fixed list of names this project's guides are allowed to
    mention rather than by pattern-matching every backticked token: a tool
    name has no shape that distinguishes it from a job type or a field name,
    so a pattern would either miss real drift or reject prose.
    """
    documented = {k.lower() for k in TOOL_META}

    # Tools the guides are expected to name. Grown as guides are written; a
    # name added here that TOOL_META does not have fails immediately, which
    # is the point.
    named_in_guides = {
        "fastp",
        "minimap2",
        "samtools",
        "bcftools",
    }

    unknown = {n for n in named_in_guides if n not in documented}
    assert not unknown, f"Guides name tools absent from TOOL_META: {unknown}"

    # And every one of those must actually appear in some guide -- a name
    # left here after the guide stopped mentioning it makes this test look
    # like it is checking more than it is.
    all_text = " ".join(resources.load_guide(t).lower() for t in resources.GuideTopic)
    unused = {n for n in named_in_guides if f"`{n}`" not in all_text}
    assert not unused, f"Listed as named in guides but absent from all of them: {unused}"


def test_endpoint_paths_are_real():
    """A guide naming a REST path must name one the app actually serves."""
    routes = {getattr(r, "path", None) for r in app.routes}
    routes.discard(None)

    named = {s for s in _all_guide_symbols() if s.startswith("/")}

    for path in named:
        # Path params are written as {id} in guides and {object_id} in routes,
        # so compare with params normalised away.
        normalised = re.sub(r"\{[^}]+\}", "{}", path)
        known = {re.sub(r"\{[^}]+\}", "{}", r) for r in routes}
        assert normalised in known, f"Guide names unknown endpoint: {path}"


def test_mcp_tool_names_in_guides_exist():
    """A guide telling an agent to call `bioflow_foo` when no such tool is
    registered is the most direct way this feature can mislead."""
    from app.mcp import tools

    registered = set(tools.TOOL_NAMES)
    named = {s for s in _all_guide_symbols() if s.startswith("bioflow_")}
    # Guides write calls as `bioflow_get_job` or `bioflow_run_pipeline(kind, params)`;
    # strip any argument list before comparing.
    bare = {n.split("(")[0] for n in named}

    assert bare <= registered, f"Guides name unknown MCP tools: {bare - registered}"
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_guides.py -q
```

Expected: two failures, both intended:

- `test_mcp_tool_names_in_guides_exist` — `ModuleNotFoundError: No module named 'app.mcp.tools'`. Task 6 creates it.
- `test_tool_names_are_real` — the four names in `named_in_guides` do not yet appear in `getting-started.md`, which mentions no bioinformatics tools. Task 14 writes the guides that name them.

Leave both red. They are guards for content that does not exist yet, and a test that only goes green once its subject is written is doing exactly what it should.

The other two (`test_job_type_names_are_real`, `test_endpoint_paths_are_real`) should pass against the getting-started guide.

- [ ] **Step 3: Verify the import path for handlers is right**

The test imports `all_handlers` from `app.queue.handlers`. Confirm where it actually lives:

```bash
docker compose exec api python -c "from app.queue.handlers import all_handlers; print(len(all_handlers()))"
```

If that import fails, find it and correct the test's import line:

```bash
grep -rn "def all_handlers" backend/app/
```

Expected: a count of registered handlers (a number > 10).

- [ ] **Step 4: Commit the tests that pass**

The `test_mcp_tool_names_in_guides_exist` test stays red until Task 6 creates `tools.py`. That is intended — it is the guard for a module that does not exist yet. Commit now so the drift tests exist before the content they check grows:

```bash
git add backend/tests/mcp/test_guides.py
git commit -m "test(mcp): drift tests tying guides to real symbols (#31)"
```

---

## Task 5: Derived resources

**Files:**
- Modify: `backend/app/mcp/resources.py`
- Test: `backend/tests/mcp/test_resources.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/mcp/test_resources.py`:

```python
"""The derived resources.

Each is generated from the code it describes, so these tests assert the
derivation reaches the real registry rather than a copy -- a resource built
from a hand-written list would pass a shallow test and drift exactly like the
prose it was meant to replace.
"""

from app.mcp import resources
from app.pipelines.tools import TOOL_META


def test_installed_tools_resource_covers_every_documented_tool():
    payload = resources.installed_tools()

    assert set(payload["tools"]) == set(TOOL_META)


def test_installed_tools_resource_carries_the_documentation_fields():
    """`/help/software` requires homepage, citation, license and usage for
    every tool. An agent deserves the same, not a bare name list."""
    payload = resources.installed_tools()
    sample = next(iter(payload["tools"].values()))

    assert {"homepage", "citation", "license", "usage"} <= set(sample)


def test_job_types_resource_matches_the_handler_registry():
    from app.queue.handlers import all_handlers

    payload = resources.job_types()

    assert set(payload["job_types"]) == set(all_handlers())


def test_sources_resource_is_not_empty():
    payload = resources.data_sources()

    assert payload["sources"]
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_resources.py -q
```

Expected: FAIL with `AttributeError: module 'app.mcp.resources' has no attribute 'installed_tools'`

- [ ] **Step 3: Write the implementation**

Append to `backend/app/mcp/resources.py`:

```python
from dataclasses import asdict, is_dataclass


def installed_tools() -> dict:
    """Every tool BioFlow documents, with the fields /help/software renders.

    Derived from TOOL_META rather than listed here, so a tool added to the
    registry reaches the agent without anyone remembering this file.
    `test_every_tool_is_documented` already forces the four fields to be
    populated, which is what makes them safe to promise.
    """
    from app.pipelines.tools import TOOL_META

    return {
        "tools": {
            name: (asdict(meta) if is_dataclass(meta) else dict(meta))
            for name, meta in TOOL_META.items()
        }
    }


def job_types() -> dict:
    """The registered job types -- the valid `kind` values for
    `bioflow_run_pipeline`.

    Read from `all_handlers()`, the same registry `GET /jobs/types` serves, so
    a newly registered handler is runnable from MCP with no change here.
    """
    from app.queue.handlers import all_handlers

    return {
        "job_types": {
            name: {
                "mode": spec.mode.value,
                "default_class": spec.default_class.value,
            }
            for name, spec in all_handlers().items()
        }
    }


def data_sources() -> dict:
    """External data sources, from the catalog behind /help/sources."""
    from app.pipelines.sources import SOURCES

    return {
        "sources": [asdict(s) if is_dataclass(s) else dict(s) for s in SOURCES]
    }
```

- [ ] **Step 4: Confirm the `SOURCES` symbol name**

`sources.py` may export a different name. Check:

```bash
grep -n "^SOURCES\|^[A-Z_]* = \[" backend/app/pipelines/sources.py
```

If the exported name differs, correct the import in `data_sources()` to match.

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_resources.py -q
```

Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/mcp/resources.py backend/tests/mcp/test_resources.py
git commit -m "feat(mcp): derived resources for tools, job types and sources (#31)"
```

---

## Task 6: Orientation and data tools

**Files:**
- Create: `backend/app/mcp/tools.py`
- Test: `backend/tests/mcp/test_tools.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/mcp/test_tools.py`:

```python
"""Tool behaviour and owner scoping.

Every assertion about scoping is B asking for A's data, following
`tests/api/test_route_owner_scoping.py`. A single profile's request for its
own data succeeds whether or not the tool ever applied a filter, so a test
written that way proves nothing -- which is the direction that fails when the
seam breaks.
"""

import pytest

from app.errors import NotFoundError
from app.mcp import tools
from app.services import profile_service, project_service


async def test_list_projects_returns_this_owners_projects():
    profile = await profile_service.create_profile(username="tools-list")
    owner = profile.owner_id()
    await project_service.create_project(name="Mine", owner=owner)

    result = await tools.list_projects(owner=owner)

    assert [p["name"] for p in result["projects"]] == ["Mine"]


async def test_list_projects_does_not_see_another_owners_projects():
    a = await profile_service.create_profile(username="tools-a")
    b = await profile_service.create_profile(username="tools-b")
    await project_service.create_project(name="A's project", owner=a.owner_id())

    result = await tools.list_projects(owner=b.owner_id())

    assert result["projects"] == []


async def test_get_project_treats_another_owners_project_as_missing():
    """Not a 403: answering differently would confirm the id is real, which
    is the reasoning already written on `jobs._owned_job`."""
    a = await profile_service.create_profile(username="tools-get-a")
    b = await profile_service.create_profile(username="tools-get-b")
    project = await project_service.create_project(name="A's", owner=a.owner_id())

    with pytest.raises(NotFoundError):
        await tools.get_project(str(project.id), owner=b.owner_id())


async def test_create_project_assigns_the_acting_owner():
    profile = await profile_service.create_profile(username="tools-create")
    owner = profile.owner_id()

    result = await tools.create_project("New project", owner=owner)

    stored = await project_service.get_project(result["id"], owner=owner)
    assert stored.name == "New project"


async def test_whoami_reports_the_acting_profile():
    profile = await profile_service.create_profile(username="tools-whoami")

    result = await tools.whoami(owner=profile.owner_id())

    assert result["username"] == "tools-whoami"
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_tools.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.mcp.tools'`

- [ ] **Step 3: Write the implementation**

`backend/app/mcp/tools.py`:

```python
"""The MCP tool surface.

Sixteen curated tools rather than one per REST route. Auto-generating from
`openapi.json` was considered and rejected: ~120 routes becomes ~120 tools,
floods the agent's context with things like the upload-chunk plumbing, and
cannot express a workflow. The OpenAPI schema is served as a *resource* the
agent reads instead.

Every function takes `owner` as an explicit keyword argument, resolved by
`context.owner_for` at the transport edge. That is the same explicit-parameter
choice `app/api/deps.py` records for the REST routes, and it is what makes the
scoping testable without a request in flight.

No tool deletes anything. That is a guardrail against agent error, not a
security boundary -- everything omitted here is still reachable over plain
HTTP by anything on this machine.
"""

from beanie import PydanticObjectId

from app.models import Profile
from app.services import object_service, project_service


def _project_summary(project) -> dict:
    return {
        "id": str(project.id),
        "name": project.name,
        "description": project.description,
        "parent_id": str(project.parent_id) if project.parent_id else None,
        "tags": project.tags,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
    }


def _object_summary(obj) -> dict:
    return {
        "id": str(obj.id),
        "name": obj.name,
        "status": obj.status.value,
        "format": obj.format.kind.value if obj.format else None,
        "role": obj.role.value if obj.role else None,
        "size_bytes": obj.size_bytes,
    }


async def whoami(*, owner: str) -> dict:
    """Which profile this MCP connection is acting as."""
    if owner == "local":
        profile = await Profile.find_one({"adopted_legacy_owner": True})
    else:
        profile = await Profile.get(PydanticObjectId(owner))

    if profile is None:
        return {"owner": owner, "username": None}

    return {
        "owner": owner,
        "profile_id": str(profile.id),
        "username": profile.username,
    }


async def list_projects(*, owner: str, parent_id: str | None = None) -> dict:
    """Projects at the top level, or inside `parent_id` when given."""
    projects = await project_service.list_projects(
        owner=owner,
        parent_id=PydanticObjectId(parent_id) if parent_id else None,
    )
    return {"projects": [_project_summary(p) for p in projects]}


async def get_project(project_id: str, *, owner: str) -> dict:
    """One project. Another profile's project is reported as not found."""
    project = await project_service.get_project(PydanticObjectId(project_id), owner=owner)
    return _project_summary(project)


async def create_project(
    name: str,
    *,
    owner: str,
    description: str = "",
    parent_id: str | None = None,
) -> dict:
    """Create a project, optionally nested inside another."""
    project = await project_service.create_project(
        name=name,
        owner=owner,
        description=description,
        parent_id=PydanticObjectId(parent_id) if parent_id else None,
    )
    return _project_summary(project)


async def list_objects(project_id: str, *, owner: str) -> dict:
    """Data objects in a project."""
    objects = await object_service.list_objects(
        owner=owner, project_id=PydanticObjectId(project_id)
    )
    return {"objects": [_object_summary(o) for o in objects]}


async def get_object(object_id: str, *, owner: str) -> dict:
    """One data object, with the facts detected on ingest."""
    obj = await object_service.get_object(PydanticObjectId(object_id), owner=owner)
    summary = _object_summary(obj)
    summary["metadata"] = obj.metadata
    return summary


# Every tool name the server registers. `tests/mcp/test_guides.py` checks
# guides against this, and `tests/mcp/test_surface.py` checks it for anything
# destructive that should not be here.
TOOL_NAMES: set[str] = {
    "bioflow_whoami",
    "bioflow_list_projects",
    "bioflow_get_project",
    "bioflow_create_project",
    "bioflow_list_objects",
    "bioflow_get_object",
}
```

- [ ] **Step 4: Check `list_objects`' real signature**

`object_service.list_objects` is at `backend/app/services/object_service.py:52`. Confirm its keyword arguments match the call above:

```bash
sed -n '52,80p' backend/app/services/object_service.py
```

Adjust the call in `list_objects` to match the real parameter names.

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_tools.py -q
```

Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/mcp/tools.py backend/tests/mcp/test_tools.py
git commit -m "feat(mcp): orientation and data tools (#31)"
```

---

## Task 7: The suggest_next tool

**Files:**
- Modify: `backend/app/mcp/tools.py`
- Modify: `backend/tests/mcp/test_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/mcp/test_tools.py`:

```python
async def test_suggest_next_returns_cards_with_their_reasons(monkeypatch):
    """The reasons are the point.

    An agent that learns "no aligner is installed" can act; one that gets a
    bare "unavailable" is stuck. This asserts the reason survives the trip
    through the tool rather than being flattened into a status.
    """
    profile = await profile_service.create_profile(username="tools-suggest")
    owner = profile.owner_id()
    project = await project_service.create_project(name="P", owner=owner)

    from app.mcp import tools as mcp_tools

    async def fake_suggestions_for(obj):
        return [
            {"kind": "align", "status": "unavailable", "reason": "No aligner is installed"},
            {"kind": "qc", "status": "available", "payload": {"object_id": "x"}},
        ]

    class FakeObject:
        id = "507f1f77bcf86cd799439011"
        name = "reads.fastq.gz"

    async def fake_get_object(object_id, *, owner):
        return FakeObject()

    monkeypatch.setattr(
        "app.services.suggestion_service.suggestions_for", fake_suggestions_for
    )
    monkeypatch.setattr("app.services.object_service.get_object", fake_get_object)

    result = await mcp_tools.suggest_next("507f1f77bcf86cd799439011", owner=owner)

    unavailable = [s for s in result["suggestions"] if s["status"] == "unavailable"]
    assert unavailable[0]["reason"] == "No aligner is installed"
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_tools.py::test_suggest_next_returns_cards_with_their_reasons -q
```

Expected: FAIL with `AttributeError: module 'app.mcp.tools' has no attribute 'suggest_next'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/mcp/tools.py`, before the `TOOL_NAMES` definition:

```python
async def suggest_next(object_id: str, *, owner: str) -> dict:
    """What can be run against this object right now, and why not otherwise.

    The highest-value tool here. It lets an agent ask the platform what to do
    instead of inferring it from the guides, and the answers are computed from
    the real object -- so they account for what is installed on this machine,
    whether a reference has an index, and what has already been run.

    Cards carry `payload` when they are runnable: hand that straight to
    `run_pipeline` rather than constructing one.
    """
    from app.services import suggestion_service

    obj = await object_service.get_object(PydanticObjectId(object_id), owner=owner)
    return {"suggestions": await suggestion_service.suggestions_for(obj)}
```

And add to `TOOL_NAMES`:

```python
    "bioflow_suggest_next",
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_tools.py -q
```

Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/mcp/tools.py backend/tests/mcp/test_tools.py
git commit -m "feat(mcp): suggest_next, so an agent can ask what to run (#31)"
```

---

## Task 8: Execution tools

**Files:**
- Modify: `backend/app/mcp/tools.py`
- Modify: `backend/tests/mcp/test_tools.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/mcp/test_tools.py`:

```python
async def test_run_pipeline_rejects_an_unknown_kind():
    """The error names the valid kinds.

    An agent that gets "unknown kind: algn" and a list can correct itself; one
    that gets a bare 400 retries the same thing.
    """
    profile = await profile_service.create_profile(username="tools-run-bad")

    from app.errors import ValidationError

    with pytest.raises(ValidationError) as exc:
        await tools.run_pipeline("not_a_real_kind", {}, owner=profile.owner_id())

    assert "not_a_real_kind" in str(exc.value)


async def test_run_pipeline_enqueues_a_known_kind(monkeypatch):
    profile = await profile_service.create_profile(username="tools-run-ok")
    owner = profile.owner_id()

    captured = {}

    async def fake_enqueue(job_type, *, owner, payload=None, **kwargs):
        captured["job_type"] = job_type
        captured["owner"] = owner
        captured["payload"] = payload

        class FakeJob:
            id = "507f1f77bcf86cd799439099"
            type = job_type
            state = type("S", (), {"value": "queued"})()

        return FakeJob()

    monkeypatch.setattr("app.queue.queue.enqueue", fake_enqueue)

    from app.queue.handlers import all_handlers

    kind = next(iter(all_handlers()))

    result = await tools.run_pipeline(kind, {"object_id": "abc"}, owner=owner)

    assert captured["job_type"] == kind
    assert captured["owner"] == owner
    assert result["job_id"] == "507f1f77bcf86cd799439099"


async def test_list_jobs_does_not_see_another_owners_jobs():
    a = await profile_service.create_profile(username="tools-jobs-a")
    b = await profile_service.create_profile(username="tools-jobs-b")

    result = await tools.list_jobs(owner=b.owner_id())

    assert all(j["owner"] != a.owner_id() for j in result.get("jobs", []))
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_tools.py -q
```

Expected: FAIL with `AttributeError: module 'app.mcp.tools' has no attribute 'run_pipeline'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/mcp/tools.py`, before `TOOL_NAMES`:

```python
async def run_pipeline(kind: str, params: dict, *, owner: str) -> dict:
    """Start a pipeline job. Returns immediately with a job id.

    `kind` is validated against `all_handlers()` -- the same registry backing
    `GET /jobs/types` -- rather than a list written here, so a newly
    registered handler is runnable without touching this module.

    The unknown-kind error names the valid values on purpose: this is the
    message an agent reads to correct itself.
    """
    from app.errors import ValidationError
    from app.queue import queue
    from app.queue.handlers import all_handlers

    known = all_handlers()
    if kind not in known:
        raise ValidationError(
            f"Unknown pipeline kind: {kind!r}. Valid kinds: {sorted(known)}",
            details={"kind": kind, "valid": sorted(known)},
        )

    job = await queue.enqueue(kind, owner=owner, payload=params)

    if job is None:
        # enqueue returns None when a matching non-terminal job already
        # exists. That is a successful outcome, not a failure -- saying so
        # stops an agent retrying into the same dedup guard.
        return {"job_id": None, "deduplicated": True, "kind": kind}

    return {"job_id": str(job.id), "kind": kind, "state": job.state.value}


async def get_job(job_id: str, *, owner: str) -> dict:
    """A job's current state. Poll this for progress; jobs are asynchronous."""
    from app.errors import NotFoundError
    from app.models import Job

    job = await Job.get(PydanticObjectId(job_id))
    if job is None or job.owner != owner:
        raise NotFoundError(f"Job not found: {job_id}")

    return {
        "job_id": str(job.id),
        "type": job.type,
        "state": job.state.value,
        "attempts": job.attempts,
        "error": job.error,
    }


async def list_jobs(*, owner: str, limit: int = 50) -> dict:
    """Recent jobs for this profile, newest first."""
    from app.models import Job

    jobs = await Job.find({"owner": owner}).sort("-created_at").limit(limit).to_list()
    return {
        "jobs": [
            {
                "job_id": str(j.id),
                "type": j.type,
                "state": j.state.value,
                "owner": j.owner,
            }
            for j in jobs
        ]
    }


async def cancel_job(job_id: str, *, owner: str) -> dict:
    """Stop a running or queued job.

    The one "undo what I started" affordance in this surface: an agent that
    can launch a multi-hour aligner should be able to halt it.
    """
    from app.errors import NotFoundError
    from app.models import Job
    from app.queue import queue

    job = await Job.get(PydanticObjectId(job_id))
    if job is None or job.owner != owner:
        raise NotFoundError(f"Job not found: {job_id}")

    await queue.cancel(job.id)
    return {"job_id": job_id, "cancelled": True}
```

And add to `TOOL_NAMES`:

```python
    "bioflow_run_pipeline",
    "bioflow_get_job",
    "bioflow_list_jobs",
    "bioflow_cancel_job",
```

- [ ] **Step 4: Confirm `queue.cancel`'s real name and signature**

```bash
grep -n "async def cancel" backend/app/queue/queue.py backend/app/services/*.py
```

If cancellation lives elsewhere (for example in a service), correct the call in `cancel_job` to match. Check how `POST /jobs/{job_id}/cancel` does it at `backend/app/api/v1/jobs.py:283` and follow that.

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_tools.py -q
```

Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/mcp/tools.py backend/tests/mcp/test_tools.py
git commit -m "feat(mcp): run, poll and cancel pipeline jobs (#31)"
```

---

## Task 9: Search, acquisition and reference tools

**Files:**
- Modify: `backend/app/mcp/tools.py`
- Modify: `backend/tests/mcp/test_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/mcp/test_tools.py`:

```python
async def test_search_objects_is_scoped_to_the_owner():
    a = await profile_service.create_profile(username="tools-search-a")
    b = await profile_service.create_profile(username="tools-search-b")

    result = await tools.search_objects("anything", owner=b.owner_id())

    assert all(o.get("owner") != a.owner_id() for o in result["objects"])


async def test_list_tools_reports_installation_state():
    """An agent needs to know what is installed, not just what exists.

    `needs_install` is a real first-run state for ON_DEMAND_IMAGE tools, and
    an agent that cannot see it will read a pullable tool as permanently
    broken -- the same wrong reading `CardStatus.NEEDS_INSTALL` exists to
    prevent in the UI.
    """
    profile = await profile_service.create_profile(username="tools-listtools")

    result = await tools.list_tools(owner=profile.owner_id())

    assert result["tools"]
    sample = next(iter(result["tools"].values()))
    assert "installed" in sample


async def test_get_guide_returns_content():
    profile = await profile_service.create_profile(username="tools-guide")

    result = await tools.get_guide("getting-started", owner=profile.owner_id())

    assert "bioflow_suggest_next" in result["content"]


async def test_get_guide_rejects_an_unknown_topic():
    profile = await profile_service.create_profile(username="tools-guide-bad")

    from app.errors import ValidationError

    with pytest.raises(ValidationError) as exc:
        await tools.get_guide("no-such-guide", owner=profile.owner_id())

    assert "getting-started" in str(exc.value)
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_tools.py -q
```

Expected: FAIL with `AttributeError: module 'app.mcp.tools' has no attribute 'search_objects'`

- [ ] **Step 3: Write the implementation**

Add to `backend/app/mcp/tools.py`, before `TOOL_NAMES`:

```python
async def search_objects(query: str, *, owner: str, limit: int = 50) -> dict:
    """Find objects across the whole library by name and metadata."""
    from app.services import search_service

    results = await search_service.search_objects(query=query, owner=owner, limit=limit)
    objects = results.get("objects", results) if isinstance(results, dict) else results
    return {"objects": [_object_summary(o) for o in objects]}


async def search_ncbi(term: str, *, owner: str) -> dict:
    """Search NCBI for assemblies matching a term.

    Acquisition is a two-step: search here, then `bioflow_download_reference`
    with the accession you want.
    """
    from app.services import ncbi_assembly_service

    return await ncbi_assembly_service.search(term)


async def download_reference(
    accession: str, project_id: str, *, owner: str
) -> dict:
    """Download an NCBI assembly into a project. Returns a job id.

    Like every pipeline, this is asynchronous -- poll `bioflow_get_job`.
    """
    from app.queue import queue

    job = await queue.enqueue(
        "download_assembly",
        owner=owner,
        payload={"accession": accession, "project_id": project_id},
        project_id=PydanticObjectId(project_id),
    )
    return {"job_id": str(job.id) if job else None, "accession": accession}


async def list_tools(*, owner: str) -> dict:
    """The bioinformatics tools BioFlow knows about, and whether each is
    installed on this machine."""
    from app.pipelines import tools as pipeline_tools
    from app.pipelines.tools import TOOL_META

    out = {}
    for name, meta in TOOL_META.items():
        probe = getattr(pipeline_tools, name, None)
        installed = bool(probe and probe().available) if callable(probe) else None
        out[name] = {
            "installed": installed,
            "usage": getattr(meta, "usage", None),
            "homepage": getattr(meta, "homepage", None),
        }
    return {"tools": out}


async def get_guide(topic: str, *, owner: str) -> dict:
    """A workflow guide.

    Duplicated as a tool as well as a resource because agent support for MCP
    resources is uneven while tool-calling is universal -- same content, two
    doors.
    """
    from app.errors import ValidationError

    from app.mcp.resources import GuideTopic, load_guide

    try:
        parsed = GuideTopic(topic)
    except ValueError as e:
        valid = sorted(t.value for t in GuideTopic)
        raise ValidationError(
            f"Unknown guide topic: {topic!r}. Valid topics: {valid}",
            details={"topic": topic, "valid": valid},
        ) from e

    return {"topic": parsed.value, "content": load_guide(parsed)}
```

And add to `TOOL_NAMES`:

```python
    "bioflow_search_objects",
    "bioflow_search_ncbi",
    "bioflow_download_reference",
    "bioflow_list_tools",
    "bioflow_get_guide",
```

- [ ] **Step 4: Confirm the service signatures used above**

Three calls here were written from route-level reading and must be checked against the real functions:

```bash
grep -n "async def search_objects" -A 12 backend/app/services/search_service.py
grep -n "async def search\|^async def " backend/app/services/ncbi_assembly_service.py | head
grep -n "download_assembly\|def probe\|\.available" backend/app/pipelines/tools.py | head -20
```

Correct `search_objects`, `search_ncbi` and `list_tools` to match what those modules actually expose. For `download_reference`, confirm the job type name by checking `all_handlers()` for the assembly-download handler:

```bash
docker compose exec api python -c "from app.queue.handlers import all_handlers; print([k for k in all_handlers() if 'assembl' in k or 'download' in k])"
```

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_tools.py -q
```

Expected: 13 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/mcp/tools.py backend/tests/mcp/test_tools.py
git commit -m "feat(mcp): search, acquisition and reference tools (#31)"
```

---

## Task 10: The surface test

**Files:**
- Test: `backend/tests/mcp/test_surface.py`

- [ ] **Step 1: Write the test**

`backend/tests/mcp/test_surface.py`:

```python
"""What the MCP surface must never grow.

The spec's decision was read + create + launch + cancel, no deletes: launching
a wasteful job costs CPU time, while `delete_project` costs someone their
library with no undo and no auth layer to catch an agent misreading its own
context.

That decision lives in a design document nobody re-reads. This test is what
makes it survive contact with the next person adding a tool.
"""

import re

from app.mcp import tools


DESTRUCTIVE = re.compile(r"delete|destroy|remove|uninstall|purge|wipe", re.I)


def test_no_destructive_tools_are_exposed():
    offenders = {n for n in tools.TOOL_NAMES if DESTRUCTIVE.search(n)}

    assert not offenders, (
        f"Destructive tools in the MCP surface: {offenders}. "
        "See docs/superpowers/specs/2026-08-06-mcp-server-design.md -- deletes "
        "were deliberately excluded. If that decision has changed, change it "
        "there first."
    )


def test_tool_names_match_the_registered_functions():
    """TOOL_NAMES is hand-written and could drift from what is registered.

    Every name must have a matching function in the module, with the
    `bioflow_` prefix stripped -- otherwise the guide drift test in
    test_guides.py validates against a list that no longer describes reality.
    """
    for name in tools.TOOL_NAMES:
        func_name = name.removeprefix("bioflow_")
        assert hasattr(tools, func_name), f"{name} has no function {func_name}"


def test_every_public_tool_function_is_declared():
    """The other direction: a function added without a TOOL_NAMES entry.

    `set(functions) == set(names)` is the exhaustiveness shape CLAUDE.md names
    as the pattern to copy, and this half catches the tool that silently never
    gets registered.
    """
    import inspect

    public = {
        name
        for name, obj in inspect.getmembers(tools, inspect.iscoroutinefunction)
        if not name.startswith("_") and obj.__module__ == tools.__name__
    }
    declared = {n.removeprefix("bioflow_") for n in tools.TOOL_NAMES}

    assert public == declared
```

- [ ] **Step 2: Run the test**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_surface.py -q
```

Expected: 3 passed. If `test_every_public_tool_function_is_declared` fails, a tool function from Tasks 6-9 is missing its `TOOL_NAMES` entry — add it.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/mcp/test_surface.py
git commit -m "test(mcp): keep deletes out of the tool surface (#31)"
```

---

## Task 11: Mount the server

**Files:**
- Create: `backend/app/mcp/server.py`
- Modify: `backend/app/main.py:88-98`
- Test: `backend/tests/mcp/test_mount.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/mcp/test_mount.py`:

```python
"""The server is reachable at the path the settings panel hands out.

`/api/v1/mcp` rather than a bare `/mcp` because `vite.config.ts` proxies
`/api` -- the versioned path is reachable from both 5173 and 8000 with no new
proxy rule in either vite.config.ts or nginx.conf. This test is what catches
someone "tidying" the path later and silently breaking every configured agent.
"""

from app.main import app


def test_mcp_is_mounted_under_the_versioned_api_path():
    paths = [getattr(r, "path", "") for r in app.routes]

    assert any(p.startswith("/api/v1/mcp") for p in paths), (
        f"No /api/v1/mcp route. Found: {sorted(p for p in paths if 'mcp' in p)}"
    )
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_mount.py -q
```

Expected: FAIL with the assertion message showing no mcp routes.

- [ ] **Step 3: Write the server module**

`backend/app/mcp/server.py`:

```python
"""Constructing the MCP server and its mountable ASGI app.

The profile arrives as a query parameter on the connection URL, so it is read
from the ASGI scope at request time rather than from a tool argument. That is
what keeps `?profile=` invisible to the agent: there is no profile parameter
on any tool for it to get wrong, and no `select_profile` tool for it to call.
"""

from urllib.parse import parse_qs

from mcp.server.fastmcp import FastMCP

from app.logging import get_logger
from app.mcp import resources, tools
from app.mcp.context import owner_for

log = get_logger(__name__)


def _profile_from_scope(scope: dict) -> str | None:
    query = parse_qs(scope.get("query_string", b"").decode())
    values = query.get("profile")
    return values[0] if values else None


def build_mcp_app():
    """The ASGI app to mount at /api/v1/mcp."""
    mcp = FastMCP("bioflow")

    def _owner_getter():
        """Resolve the acting owner for the request in flight.

        Wrapped in a closure so each tool body reads it at call time rather
        than capturing a value at registration time -- one server instance
        serves every connection, and connections carry different profiles.
        """
        request = mcp.get_context().request_context.request
        return owner_for(_profile_from_scope(request.scope))

    @mcp.tool(name="bioflow_whoami")
    async def whoami() -> dict:
        """Which BioFlow profile this connection is acting as."""
        return await tools.whoami(owner=await _owner_getter())

    @mcp.tool(name="bioflow_list_projects")
    async def list_projects(parent_id: str | None = None) -> dict:
        """List projects. Omit parent_id for the top level."""
        return await tools.list_projects(owner=await _owner_getter(), parent_id=parent_id)

    @mcp.tool(name="bioflow_get_project")
    async def get_project(project_id: str) -> dict:
        """Get one project by id."""
        return await tools.get_project(project_id, owner=await _owner_getter())

    @mcp.tool(name="bioflow_create_project")
    async def create_project(
        name: str, description: str = "", parent_id: str | None = None
    ) -> dict:
        """Create a project, optionally nested inside another."""
        return await tools.create_project(
            name, owner=await _owner_getter(), description=description, parent_id=parent_id
        )

    @mcp.tool(name="bioflow_list_objects")
    async def list_objects(project_id: str) -> dict:
        """List the data objects in a project."""
        return await tools.list_objects(project_id, owner=await _owner_getter())

    @mcp.tool(name="bioflow_get_object")
    async def get_object(object_id: str) -> dict:
        """Get one data object, with the facts detected on ingest."""
        return await tools.get_object(object_id, owner=await _owner_getter())

    @mcp.tool(name="bioflow_suggest_next")
    async def suggest_next(object_id: str) -> dict:
        """What can be run against this object right now, and why not otherwise.

        Prefer this over reasoning from the guides: it is computed from the
        real object, so it accounts for what is installed, whether a reference
        has an index, and what has already been run. Runnable cards carry a
        `payload` to hand straight to bioflow_run_pipeline.
        """
        return await tools.suggest_next(object_id, owner=await _owner_getter())

    @mcp.tool(name="bioflow_run_pipeline")
    async def run_pipeline(kind: str, params: dict) -> dict:
        """Start a pipeline job. Returns a job id immediately; poll bioflow_get_job.

        Valid `kind` values are in the bioflow://jobs/types resource, or take
        a ready-made payload from bioflow_suggest_next.
        """
        return await tools.run_pipeline(kind, params, owner=await _owner_getter())

    @mcp.tool(name="bioflow_get_job")
    async def get_job(job_id: str) -> dict:
        """A job's current state. Jobs are asynchronous and can run for hours."""
        return await tools.get_job(job_id, owner=await _owner_getter())

    @mcp.tool(name="bioflow_list_jobs")
    async def list_jobs(limit: int = 50) -> dict:
        """Recent jobs for this profile, newest first."""
        return await tools.list_jobs(owner=await _owner_getter(), limit=limit)

    @mcp.tool(name="bioflow_cancel_job")
    async def cancel_job(job_id: str) -> dict:
        """Stop a queued or running job."""
        return await tools.cancel_job(job_id, owner=await _owner_getter())

    @mcp.tool(name="bioflow_search_objects")
    async def search_objects(query: str, limit: int = 50) -> dict:
        """Find objects across the whole library by name and metadata."""
        return await tools.search_objects(query, owner=await _owner_getter(), limit=limit)

    @mcp.tool(name="bioflow_search_ncbi")
    async def search_ncbi(term: str) -> dict:
        """Search NCBI for assemblies. Then use bioflow_download_reference."""
        return await tools.search_ncbi(term, owner=await _owner_getter())

    @mcp.tool(name="bioflow_download_reference")
    async def download_reference(accession: str, project_id: str) -> dict:
        """Download an NCBI assembly into a project. Returns a job id."""
        return await tools.download_reference(
            accession, project_id, owner=await _owner_getter()
        )

    @mcp.tool(name="bioflow_list_tools")
    async def list_tools() -> dict:
        """The bioinformatics tools BioFlow knows about, and what is installed."""
        return await tools.list_tools(owner=await _owner_getter())

    @mcp.tool(name="bioflow_get_guide")
    async def get_guide(topic: str) -> dict:
        """Read a workflow guide. Start with 'getting-started'."""
        return await tools.get_guide(topic, owner=await _owner_getter())

    @mcp.resource("bioflow://jobs/types")
    def job_types_resource() -> dict:
        """The registered job types -- valid `kind` values for run_pipeline."""
        return resources.job_types()

    @mcp.resource("bioflow://tools/installed")
    def tools_resource() -> dict:
        """Every documented tool, with homepage, citation, license and usage."""
        return resources.installed_tools()

    @mcp.resource("bioflow://sources")
    def sources_resource() -> dict:
        """External data sources BioFlow draws on."""
        return resources.data_sources()

    for topic in resources.GuideTopic:

        @mcp.resource(f"bioflow://guides/{topic.value}")
        def guide_resource(topic=topic) -> str:
            return resources.load_guide(topic)

    log.info("mcp server built", tools=len(tools.TOOL_NAMES))
    return mcp.streamable_http_app()
```

- [ ] **Step 4: Mount it in main.py**

In `backend/app/main.py`, in `create_app()`, after `app.include_router(api_router)` and before `return app`:

```python
    # Mounted rather than routed: the MCP server is its own ASGI app with its
    # own session handling. /api/v1/mcp rather than /mcp because
    # frontend/vite.config.ts already proxies /api -- the versioned path is
    # reachable from both 5173 and 8000 with no new proxy rule in
    # vite.config.ts or nginx.conf.
    from app.mcp.server import build_mcp_app

    app.mount("/api/v1/mcp", build_mcp_app())
```

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_mount.py -q
```

Expected: 1 passed

If `FastMCP` or `streamable_http_app()` does not exist under those names in mcp 2.0.0, check the installed package and adjust:

```bash
docker compose exec api python -c "import mcp.server.fastmcp as m; print([n for n in dir(m) if not n.startswith('_')])"
```

- [ ] **Step 6: Run the whole MCP suite**

```bash
./backend/run-worktree-tests.sh tests/mcp -q
```

Expected: all passing, including `test_mcp_tool_names_in_guides_exist` from Task 4, which required `tools.TOOL_NAMES` to exist.

- [ ] **Step 7: Commit**

```bash
git add backend/app/mcp/server.py backend/app/main.py backend/tests/mcp/test_mount.py
git commit -m "feat(mcp): mount the MCP server at /api/v1/mcp (#31)"
```

---

## Task 12: Verify against the running stack

**Files:** none — this is the "check it against reality, not only its unit tests" step CLAUDE.md requires.

- [ ] **Step 1: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

Expected: UI on 5273, API on 8100.

- [ ] **Step 2: Confirm the endpoint answers**

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8100/api/v1/mcp
```

Expected: a non-404 status. A 404 means the mount did not take; a 406 or 400 is fine — it means the route exists and is rejecting a non-MCP request.

- [ ] **Step 3: Confirm the profile fallback against real data**

```bash
docker compose -p $(basename $PWD) exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.mcp.context import owner_for
async def main():
    await connect_to_mongo()
    print('resolved owner:', await owner_for(None))
asyncio.run(main())
"
```

Expected: either a real owner string (one profile) or a `ProfileUnresolvedError` naming `?profile=` (several). Both are correct — note which, because it tells you what the panel in Task 13 must hand out.

- [ ] **Step 4: Confirm suggest_next against a real object**

The suggestion rules have been wrong before against real data while green in tests — `protein.faa` counted as an alignable reference, one assembly stored twice counted as two. Run it against something real:

```bash
docker compose -p $(basename $PWD) exec api python -c "
import asyncio
from app.db.client import connect_to_mongo
from app.models import DataObject
from app.mcp import tools
async def main():
    await connect_to_mongo()
    obj = await DataObject.find_one({})
    if obj is None:
        print('no objects in this database'); return
    out = await tools.suggest_next(str(obj.id), owner=obj.owner)
    print(obj.name)
    for s in out['suggestions']:
        print(' ', s.get('kind'), s.get('status'), s.get('reason', ''))
asyncio.run(main())
"
```

Expected: a list of cards with statuses and reasons. Read them — a card whose reason is nonsense for that file is a real bug, and this is the step designed to catch it.

- [ ] **Step 5: Tear down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 6: Commit nothing, but record what you saw**

If Steps 3-4 turned up anything wrong, fix it and commit that fix before moving on. If everything was correct, there is nothing to commit here.

---

## Task 13: The connection panel

**Files:**
- Create: `frontend/src/components/SettingsMcp.tsx`
- Modify: `frontend/src/components/SettingsNav.tsx:16-31`
- Modify: `frontend/src/App.tsx:108-110`

- [ ] **Step 1: Create the panel**

`frontend/src/components/SettingsMcp.tsx`:

```tsx
import { useState } from "react";
import { SettingsNav } from "./SettingsNav";
import { useProfileStore } from "../stores/profileStore";

/**
 * The paste-ready MCP connection config.
 *
 * Load-bearing rather than polish: the profile id is a Mongo ObjectId, and
 * without this panel the feature's first step is "go find your id in the
 * database". The URL is built from `window.location.origin` so the user is
 * handed whichever port they already have open -- nobody has to learn that
 * 8000 exists alongside 5173.
 */
export function SettingsMcp() {
  const profile = useProfileStore((s) => s.current);
  const [copied, setCopied] = useState(false);

  const url = profile
    ? `${window.location.origin}/api/v1/mcp?profile=${profile.id}`
    : "";

  const config = JSON.stringify(
    { mcpServers: { bioflow: { url } } },
    null,
    2,
  );

  const copy = async () => {
    await navigator.clipboard.writeText(config);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="settings-view">
      <SettingsNav />
      <div className="settings-section">
        <h2>MCP server</h2>
        <p>
          BioFlow exposes an MCP server so an AI coding agent can browse your
          projects, ask what to run next, and launch pipelines. Paste this into
          your agent's MCP configuration.
        </p>

        {profile ? (
          <>
            <pre className="mcp-config">{config}</pre>
            <button className="btn" onClick={copy}>
              {copied ? "Copied" : "Copy configuration"}
            </button>
            <p className="hint">
              Acting as <strong>{profile.username}</strong>. An agent connected
              with this URL sees only this profile's data and cannot switch
              profiles or delete anything.
            </p>
          </>
        ) : (
          <p className="hint">Select a profile to see its connection URL.</p>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Add the nav item**

In `frontend/src/components/SettingsNav.tsx`, replace the body of the component. The existing two-item version branches on a single boolean, which does not extend to three:

```tsx
export function SettingsNav() {
  const { pathname } = useLocation();

  const items = [
    { to: "/settings/ai", label: "AI" },
    { to: "/settings/tools", label: "Tools" },
    { to: "/settings/mcp", label: "MCP" },
  ];

  // `/settings` with no section renders the AI page, so it counts as AI being
  // active -- otherwise landing on the bare path shows no item selected.
  const active =
    items.find((i) => pathname.startsWith(i.to))?.to ?? "/settings/ai";

  return (
    <nav className="settings-section-nav">
      {items.map((item) => (
        <Link
          key={item.to}
          to={item.to}
          className={`settings-section-nav-item${
            active === item.to ? " active" : ""
          }`}
        >
          {item.label}
        </Link>
      ))}
    </nav>
  );
}
```

- [ ] **Step 3: Add the route**

In `frontend/src/App.tsx`, after the `/settings/tools` route at line 110:

```tsx
          <Route path="/settings/mcp" element={<SettingsMcp />} />
```

And add the import alongside the other settings imports:

```tsx
import { SettingsMcp } from "./components/SettingsMcp";
```

- [ ] **Step 4: Verify in the browser**

This is the actual verification step for UI work — there is no headless component-testing setup in this repo.

```bash
./ops/worktree-up.sh
```

Open http://localhost:5273/settings/mcp and confirm:
- The nav shows three items and MCP is highlighted
- The JSON block shows a URL with a real profile id
- "Copy configuration" copies it (paste somewhere to check)
- The URL's origin matches the port you are browsing on

- [ ] **Step 5: Verify the copied config actually works**

Paste the copied block into a real agent's MCP configuration and confirm it connects and lists tools. This is the end-to-end check the whole feature exists for: everything up to here proves the parts work, and only this proves the thing a user does works.

- [ ] **Step 6: Tear down and commit**

```bash
./ops/worktree-up.sh --down
git add frontend/src/components/SettingsMcp.tsx frontend/src/components/SettingsNav.tsx frontend/src/App.tsx
git commit -m "feat(mcp): settings panel with the paste-ready connection config (#31)"
```

---

## Task 14: Write the remaining guides

**Files:**
- Create: `backend/app/mcp/guides/acquiring-data.md`
- Create: `backend/app/mcp/guides/read-qc-and-trimming.md`
- Create: `backend/app/mcp/guides/alignment-and-variants.md`
- Create: `backend/app/mcp/guides/de-novo-assembly.md`
- Create: `backend/app/mcp/guides/rna-quantification.md`
- Modify: `backend/app/mcp/resources.py` (`GuideTopic`)

- [ ] **Step 1: Add the topics**

In `backend/app/mcp/resources.py`, extend `GuideTopic`:

```python
class GuideTopic(StrEnum):
    GETTING_STARTED = "getting-started"
    ACQUIRING_DATA = "acquiring-data"
    READ_QC_AND_TRIMMING = "read-qc-and-trimming"
    ALIGNMENT_AND_VARIANTS = "alignment-and-variants"
    DE_NOVO_ASSEMBLY = "de-novo-assembly"
    RNA_QUANTIFICATION = "rna-quantification"
```

- [ ] **Step 2: Run the tests to see them fail**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_guides.py -q
```

Expected: FAIL — `test_every_topic_has_a_file` errors with `FileNotFoundError` for the five missing guides. This is the exhaustiveness test doing its job.

- [ ] **Step 3: Find the real job-type names before writing a word**

Every backticked symbol in a guide is checked. Get the real names first rather than writing from memory:

```bash
docker compose exec api python -c "from app.queue.handlers import all_handlers; print(sorted(all_handlers()))"
docker compose exec api python -c "from app.pipelines.tools import TOOL_META; print(sorted(TOOL_META))"
```

Write the output down. Guides must use these exact strings in backticks.

- [ ] **Step 4: Write the five guides**

Each follows the same shape as `getting-started.md`: what the workflow is for, the order of operations, the job types involved, and what to check between steps. Rules that the drift tests enforce:

- Backtick real symbols only: job types from `all_handlers()`, tools from `TOOL_META`, MCP tools from `TOOL_NAMES`, endpoint paths that resolve
- Write anything unchecked without backticks
- Point at `bioflow_suggest_next` for specifics rather than enumerating parameters — it is computed from the real object and the guide is not

Content per guide:

**`acquiring-data.md`** — the three ways data enters BioFlow: uploading local files, `bioflow_search_ncbi` → `bioflow_download_reference` for assemblies, and SRA accessions. Note that downloads are jobs and must be polled.

**`read-qc-and-trimming.md`** — run QC on raw reads first, read the report, then trim if warranted. Name the real QC and trim job types. Note that paired reads are detected and paired automatically, and that trimming produces a new object rather than replacing the input.

**`alignment-and-variants.md`** — the chain: a reference must be indexed before it can be aligned against; align produces a BAM; variant calling runs on the BAM. Name the real job types for index, align and variant calling. Note that `bioflow_suggest_next` on a reference will say whether an index exists.

**`de-novo-assembly.md`** — assemble → polish → scaffold → QC, naming the real job types at each step, and noting that each stage is optional and produces a new object that the next stage consumes.

**`rna-quantification.md`** — quantify against an annotated reference, then differential expression across samples. Name the real job types. Note that DE needs multiple quantified samples in one project.

- [ ] **Step 5: Run the drift tests**

```bash
./backend/run-worktree-tests.sh tests/mcp/test_guides.py -q
```

Expected: all passing. A failure here names the exact symbol that does not exist — fix the guide, not the test.

- [ ] **Step 6: Run the whole MCP suite**

```bash
./backend/run-worktree-tests.sh tests/mcp -q
```

Expected: all passing.

- [ ] **Step 7: Commit**

```bash
git add backend/app/mcp/guides/ backend/app/mcp/resources.py
git commit -m "docs(mcp): workflow guides for the five main paths (#31)"
```

---

## Task 15: Full suite, merge and close out

- [ ] **Step 1: Run the entire backend suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: everything passes. **Read the count**, not just the exit code — CLAUDE.md is explicit that green means the number, and the mount in Task 11 touches `create_app()`, which every API test builds on.

- [ ] **Step 2: Check main is clean, then merge**

```bash
git -C /Users/syntheticgio/Programming/local-bio-pipeliner status --short
git -C /Users/syntheticgio/Programming/local-bio-pipeliner log --oneline -1
```

If main has moved, merge it into this branch and re-run the suite before continuing — a green from before someone else's merge is not a green.

```bash
git -C /Users/syntheticgio/Programming/local-bio-pipeliner merge --no-ff claude/bioflow-api-endpoints-40f7eb
```

- [ ] **Step 3: Restore the main stack**

Task 12 and 13 ran a worktree stack. Confirm 5173 is serving the main checkout, not this worktree:

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

If any path is under `.claude/worktrees/`, fix it from the main checkout root:

```bash
docker compose up -d --build api web worker
```

- [ ] **Step 4: Push**

```bash
git -C /Users/syntheticgio/Programming/local-bio-pipeliner push origin main
```

- [ ] **Step 5: Update the issue**

```bash
gh issue comment 31 --body "Implemented and merged. MCP server mounted at /api/v1/mcp, 16 curated tools, drift-tested workflow guides, and a settings panel with the paste-ready connection config."
gh issue edit 31 --remove-label "status: implementation plan"
gh issue close 31
```

Note the label name has a space after the colon — `status: implementation plan`, unlike its siblings.

---

## Self-Review Notes

**Spec coverage:** Every section of the spec maps to a task — placement (11), profile resolution (2), connection panel (13), tool surface (6-9), derived resources (5), guides (3, 14), drift tests (4), error handling (throughout, with the actionable-message assertions in 2, 8 and 9), testing (2-10), out-of-scope items (excluded by construction, guarded by 10).

**Known verification points:** Tasks 5, 8, 9 and 11 each include an explicit step to check a symbol name against the installed code rather than trusting this plan's reading. Those are the places where the plan was written from route-level inspection rather than from running the function, and they are marked as such instead of being presented as certain.
