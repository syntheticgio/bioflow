# MCP server

Design for [#31](https://github.com/syntheticgio/bioflow/issues/31).

An MCP server exposed by BioFlow itself, so an AI coding agent can drive the
platform. The user does not install a separate program: BioFlow is already
running, and connecting an agent is a matter of pasting a URL that BioFlow
hands them.

The goal is not only "reach the API from an agent." It is that an agent with no
prior knowledge of BioFlow can get useful work done -- which means the server
carries workflow guidance ("select a profile, create a project, download the
data, align it") alongside the endpoint reference, because a workflow is not an
endpoint and cannot be derived from one.

## Placement

The server mounts inside the existing FastAPI app at **`/api/v1/mcp`**, served
by the `api` container on port 8000.

`/api/v1/mcp` rather than a bare `/mcp` for a concrete reason:
`frontend/vite.config.ts` proxies `/api` to `http://api:8000`, so the versioned
path is reachable at **both** ports with no new configuration --
`localhost:5173/api/v1/mcp` and `localhost:8000/api/v1/mcp`. A bare `/mcp`
would 404 through the frontend until a proxy rule was added to *both*
`vite.config.ts` and `nginx.conf`, and the nginx half is the one that would be
forgotten, producing an endpoint that works in development and vanishes in the
packaged build. Being inside `/api/v1` is also honest about versioning: the MCP
surface will have breaking changes, exactly as the REST surface will.

New package `backend/app/mcp/`:

- `server.py` -- constructs the MCP server; mounted onto the app in `create_app()`
- `tools.py` -- hand-written tool definitions
- `resources.py` -- documentation resources
- `guides/*.md` -- hand-written workflow guides

Tool functions call the **service layer** (`project_service`,
`pipeline_service`, `suggestion_service`, `object_service`) -- the same layer
`app/api/v1/` routers call. No HTTP hop, no duplicated business logic, and no
second definition of any rule.

One dependency added to `backend/pyproject.toml`: an MCP server library
exposing a Streamable HTTP ASGI app mountable onto FastAPI.

### Why in-process

Considered and rejected:

- **Separate container consuming the REST API over HTTP.** Isolation is real
  but buys little for a single-user local tool, against the standing cost of a
  fourth service with its own image, healthcheck, and restart semantics.
- **Auto-generated from `openapi.json`.** Never drifts, but turns ~120 routes
  into ~120 tools -- flooding the agent's context with things like
  `PUT /uploads/{id}/chunks/{i}` -- and cannot express workflows at all, which
  is the main thing being asked for. The idea survives in a better place: the
  OpenAPI schema is exposed as a *resource the agent reads*, not as a tool
  manifest it must carry.

Splitting into its own container later stays cheap: the tool surface is defined
independently of how its functions reach data, so the change would be in what
those functions call, not in what the agent sees.

## Profile resolution

Every partitioned query needs an `owner`. The web UI gets one from the
`X-BioFlow-Profile` header after the startup picker; MCP has no picker.

**The profile is fixed when the user configures their agent**, via query
parameter:

```
http://localhost:5173/api/v1/mcp?profile=<profile_id>
```

Resolution reuses `deps.resolve_owner`, the same function the SSE stream calls
with a query parameter for the same reason -- a transport that cannot send
custom headers. No new resolution path.

**Absent `profile`:** fall back to the sole profile when exactly one exists;
raise a clear error naming the missing parameter when two or more do. On a
single-person install the query string can be omitted entirely; the error
appears only where guessing would actually be wrong.

**`bioflow_whoami()`** is exposed read-only so the agent can report which
profile it is acting as.

**There is deliberately no `select_profile` tool.** An agent able to switch
profiles mid-session can write a project into the wrong library, and since
profiles are not an auth boundary (`app/api/deps.py` is explicit: "Rejecting an
unknown header is *not* authentication"), nothing downstream would catch it.
Choosing a profile is the human's job, done once, at agent-config time.

### Connection panel

A section in the frontend's profile/settings area renders the ready-to-paste
config with the current profile's id filled in, plus a copy button:

```json
{ "mcpServers": { "bioflow": { "url": "http://localhost:5173/api/v1/mcp?profile=68f2a1..." } } }
```

The URL is built from `window.location.origin`, so the user is handed whichever
port they already have open and never has to learn that 8000 exists.

This panel is load-bearing, not polish: the profile id is a Mongo ObjectId, and
without BioFlow handing it over the feature's first step is "go find your id in
the database."

### What this is not

- **Not authentication.** Anything on the machine that can reach the port can
  drive the API as any profile, exactly as true today. `?profile=` prevents
  confusion, not access.
- **The profile id lands in the user's agent config in plaintext.** It is an
  ObjectId and already visible in the UI, but it is one more place it is
  written down.

## Tool surface

~16 hand-written, curated tools. Small enough that an agent reasons well over
the whole manifest.

**Orientation** -- `bioflow_whoami`, `bioflow_list_projects`,
`bioflow_get_project`

**Data** -- `bioflow_create_project`, `bioflow_list_objects`,
`bioflow_get_object`, `bioflow_search_objects`

**Guidance** -- `bioflow_suggest_next(object_id)`

**Execution** -- `bioflow_run_pipeline(kind, params)`, `bioflow_get_job`,
`bioflow_list_jobs`, `bioflow_cancel_job`

**Acquisition** -- `bioflow_search_ncbi`, `bioflow_download_reference`

**Reference** -- `bioflow_list_tools`, `bioflow_get_guide(topic)`

### `bioflow_suggest_next`

Wraps `suggestion_service.suggestions_for()`, returning each candidate pipeline
with its status (`available` / `unavailable` / `needs_install`), its launch
payload, and the honest reason it cannot run.

The highest-value tool here. It lets an agent *ask the platform* what to do
next instead of inferring it from prose, and its answers are computed from the
real object rather than guessed -- including the reasons, which is what makes a
dead end into a next step. It also means the guides can stay short: they teach
the shape of the system, and `suggest_next` handles the specifics.

### One `run_pipeline`, not one tool per pipeline

A single generic run tool keeps the manifest small. Per-pipeline parameter
schemas are reachable as resources, and `suggest_next` returns ready-made
payloads for the common path -- so the agent rarely has to construct one by
hand.

`kind` is validated against `all_handlers()` -- the same registry backing
`GET /jobs/types` -- rather than against a list written here, so a newly
registered handler is runnable without touching the MCP package and an unknown
`kind` fails with the set of valid ones rather than a generic error.

### Cancel is included

An agent that can launch a multi-hour aligner should be able to stop it. It is
the one "undo what I started" affordance in the surface.

### Not exposed

Every delete route, `uninstall_tool`, upload-chunk plumbing, profile
creation/selection, AI provider settings, share accept/decline.

Deletes are omitted because the failure modes are not comparable: launching a
wasteful job costs CPU time, and `delete_project` costs someone their library
with no undo and no auth layer to catch an agent misreading its own context.
The rest are UI mechanics with no agent use, or configuration an agent has no
business mutating.

This is a guardrail against agent error, **not a security boundary** --
everything omitted is still reachable over plain HTTP by anything on the
machine. Agent error is the realistic failure mode here; an attacker on the
loopback interface already has the whole API.

Deletes can be added later if their absence proves annoying. That direction is
easy; the reverse is not.

## Documentation resources

Split by whether the content can go stale.

### Derived -- cannot drift

- `bioflow://api/openapi` -- FastAPI's generated schema: the full endpoint reference
- `bioflow://tools/installed` -- from `TOOL_META`, already forced complete by `test_every_tool_is_documented`
- `bioflow://sources` -- from `sources.py`, which carries its own completeness test
- `bioflow://jobs/types` -- from `all_handlers()`, the real registry of runnable job types

### Hand-written workflow guides

In `backend/app/mcp/guides/`, one per path a user actually walks. A
`GuideTopic` StrEnum in `resources.py` names them; its members are the valid
arguments to `bioflow_get_guide` and each corresponds to a `<member>.md` file
in that directory:

- `getting-started` -- profiles, projects, the shape of the system
- `acquiring-data` -- NCBI/SRA search, reference download, upload
- `read-qc-and-trimming`
- `alignment-and-variants`
- `de-novo-assembly` -- assemble → polish → scaffold → QC
- `rna-quantification` -- quantify → differential expression

Each names real symbols: job-type strings, tool names, endpoint paths.

### Drift tests

Hand-written prose about code goes stale silently, and this repo has been
bitten three times: the 2026-07-31 TODO audit found three entries describing
work that had already shipped, one of them advising deletion of live code; the
`ToolMeta.runnable` comment cited cutadapt and Trimmomatic for years after
`trim_reads` grew its dispatch; and `results._SIDECAR_ROLES` silently dropped
STAR's eight index files while the whole suite stayed green.

A guide that confidently names a tool which no longer exists is worse than no
guide, because the entire purpose of the feature is telling an agent what is
true. So, in `backend/tests/mcp/test_guides.py`:

1. Every job-type string a guide names exists in `all_handlers()`
2. Every tool name a guide names exists in `TOOL_META`
3. Every endpoint path a guide names resolves against the app's route table
4. Every guide in the topic enum has a file, and every file is reachable --
   `set(GuideTopic) == set(files)`, the exhaustiveness pattern CLAUDE.md names
   as the one to copy

Consequence, and the actual cost of this choice: **guides must name symbols in
a greppable form** -- backticked literals, not paraphrase. Prose that says "the
alignment job" instead of `` `align` `` is invisible to these tests and free to
rot.

### Guides are both a resource and a tool

`bioflow_get_guide(topic)` returns the same content as the resources. Agent
support for MCP resources is uneven while tool-calling is universal; the
duplication buys reach for one small function.

## Error handling

MCP errors are what the agent reads to decide what to do next, so they must be
actionable rather than merely accurate. `app/errors.py` types map to structured
MCP errors:

- `ProfileUnresolvedError` -- names the missing `?profile=` parameter and points
  at the settings panel that supplies the id
- `NotFoundError` -- stays indistinguishable from wrong-owner, matching the
  reasoning already written on `jobs._owned_job`: answering otherwise would
  confirm an id is real
- `ValidationError` -- returns the field that failed, so the agent can retry
  rather than guess

The one that matters most: when `run_pipeline` is called for something that
cannot run, it returns `suggest_next`'s **reason** -- "no aligner installed,"
"reference has no index" -- not a generic 400. A dead end becomes a next step,
which is the premise of the whole feature.

## Testing

`backend/tests/mcp/`, run from a worktree with
`./backend/run-worktree-tests.sh tests/mcp -q`.

- **Tool tests** -- each tool called through the server with a fake owner,
  asserting it reaches the right service function and threads owner scoping.
  Follow `test_route_owner_scoping.py`: assert a *wrong* owner sees nothing,
  since that is the direction that fails when the seam breaks.
- **Guide drift tests** -- the four checks above.
- **Surface tests** -- assert the not-exposed list stays not-exposed (no tool
  name matching `delete|uninstall|remove`). Crude, but it catches the realistic
  failure: someone adding a tool later without knowing this decision was made.

## Out of scope

No auth layer. No rate limiting. No streaming job progress over MCP -- poll
`bioflow_get_job`. No deletes. No remote or non-localhost access.
