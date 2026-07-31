# Software and sources reference help pages

Date: 2026-07-31

## Goal

Two new Help pages documenting the third-party software BioFlow runs and the
external data sources it draws from. Each entry carries a comprehensive
description, links to its homepage and repository, its citation, its license,
and -- for tools -- the version actually installed in the running container.

The version must never require a manual edit when a tool is upgraded.

## Background

Most of the tool data already exists.

`backend/app/pipelines/tools.py` is the registry of external binaries. It does
two things this feature needs:

- **Probes each binary at runtime** for its version (`_probe`, line 52),
  caching per process. The probe is honest about failure: a missing binary
  yields `available: false` with an error string, and an unrunnable one (an
  x86-64 build on arm64) is reported as unavailable rather than versioned.
- **Describes each tool statically** in `TOOL_META` (line 307) -- 17 entries
  carrying `pipelines`, `summary`, `strengths`, `one_liner`, and `runnable`.

`tool_with_meta()` (line 517) merges probe and description, and
`GET /api/v1/pipelines/tools` (`backend/app/api/v1/pipelines.py:39`) already
serves the merged result. `ToolDetailPane.tsx` renders a subset of it in the
launch dialog.

What is missing is bibliographic: homepage, repository, citation, license, and
a statement of how BioFlow uses each tool.

Data sources have no registry at all. Three real endpoints are reached today,
as bare constants:

- NCBI Datasets v2alpha -- `backend/app/metadata/assembly.py:28`
- NCBI E-utilities -- `backend/app/metadata/sra.py:28`
- The SRA record UI -- `backend/app/metadata/sra.py:372`

Help today is one page, `HelpCalculations.tsx`, routed at
`/help/calculations` (`App.tsx:67`) and listed in `HELP_ITEMS`
(`Header.tsx:17`). `App.tsx:46` already gives any `/help/` path a
single-column layout.

Broadsheet is the only theme as of the 2026-07-30 theme removal:
`frontend/index.html:2` hardcodes `class="theme-broadsheet"` on `<html>`.
`styles.css` still supplies structural CSS; `broadsheet.css` overrides
appearance. Both are imported unconditionally.

## Approach

Extend the existing backend catalog rather than building a parallel one in the
frontend, and render both pages from live API data.

The decisive reason is the failure mode a frontend-side catalog would create.
A TypeScript constant keyed by tool name is exactly the hand-maintained
parallel mapping this repo has already been burned by (see the
`suggestion_service.py` note in CLAUDE.md): adding an 18th tool to `TOOL_META`
would silently omit it from the help page, with nothing failing. Keeping one
catalog means a new tool either gets an entry or fails a test.

The version comes from the existing runtime probe, so upgrading a tool in the
image updates the help page with no edit anywhere.

## Design

### Backend: extend `ToolMeta`

Six fields added to the frozen dataclass at `tools.py:283`:

| Field | Example | Notes |
| --- | --- | --- |
| `homepage` | `https://github.com/bwa-mem2/bwa-mem2` | May equal `repository` for tools with no separate site |
| `repository` | `https://github.com/bwa-mem2/bwa-mem2` | Empty when there is no public repo |
| `citation` | `Vasimuddin et al., IPDPS 2019` | Human-readable, for a methods section |
| `citation_url` | DOI link | Empty when the tool has no paper |
| `license` | `MIT` | SPDX identifier |
| `usage` | prose | How BioFlow uses this tool |

All six default to `""`, so the dataclass stays constructible for any entry
not yet filled in.

These reach the API without a serializer change. `tool_with_meta()` builds its
metadata via `asdict(meta)`, and the docstring at line 517 already states that
a field added to `ToolMeta` reaches the API without a second edit here. The
fallback dict for tools with no metadata (line 522) gains the same six keys
with `""` values, since it enumerates keys explicitly.

No route change. No frontend type change beyond widening `PipelineTool`.

**`usage` is prose and cannot be mechanically verified.** It is the one field
that can rot when a runner changes. Mitigate by describing behaviour, not
flags -- "runs whenever you align short reads; builds the index on first use
and pipes to samtools for sorting" stays true across parameter changes in a
way that naming `-K 100000000` does not.

### Backend: completeness test

In `backend/tests/pipelines/test_tools.py`, assert every `TOOL_META` entry has
non-empty `homepage`, `citation`, `license`, and `usage`.

This is the guard that makes the single-catalog choice pay off: adding a tool
without documenting it fails the suite rather than rendering a blank entry.

`repository` and `citation_url` are deliberately excluded -- some tools
genuinely have no public repo or no paper, and a test that forced a value
there would invite a fabricated one.

### Backend: sources registry

New module `backend/app/pipelines/sources.py`. Separate from `tools.py`
because that module is about probing binaries, and a source has no binary, no
version, and no probe.

```python
@dataclass(frozen=True)
class DataSource:
    name: str
    kind: str          # "api" | "database" | "reference"
    summary: str
    usage: str
    homepage: str
    docs: str = ""
    citation: str = ""
    citation_url: str = ""
    terms: str = ""    # usage-policy link
```

Seeded with the three endpoints named in Background. A module-level
`all_sources() -> list[dict]` returns them for the API.

**Sources have no version and none is invented.** NCBI Datasets is whatever
the API returned today. The page shows no version field for sources rather
than a fabricated one -- the same honesty principle the tool probe follows.

### Backend: sources endpoint

`GET /api/v1/system/sources` in `backend/app/api/v1/system.py`, returning
`{"sources": [...]}`.

On `system` rather than `pipelines` because these are not pipeline tools. The
handler is a pure return of static data -- no probing, no I/O.

### Frontend: two sibling pages

Separate pages rather than one, because sources are expected to grow.

| Route | Component | Source |
| --- | --- | --- |
| `/help/software` | `HelpSoftware.tsx` | `GET /api/v1/pipelines/tools` |
| `/help/sources` | `HelpSources.tsx` | `GET /api/v1/system/sources` |

Both registered in `App.tsx` beside the existing help route, and added to
`HELP_ITEMS` in `Header.tsx:17`. Both inherit single-column layout from the
existing `pathname.startsWith("/help/")` check at `App.tsx:46` -- no layout
change needed.

Both fetch with `useQuery` following the app's existing pattern. Two new
methods on `api`: `pipelineTools()` if not already present, and `sources()`.

### Page structure

A single scrolling ruled index -- a printed reference, not a second tool
browser. No left rail: `PipelineToolSelector` already provides selection UI
for choosing a tool to run, and duplicating it here would build a second
navigation for a page whose job is to be read and linked into. Broadsheet's
scroll model (`broadsheet.css:200`: "Broadsheet is a page: the sheet scrolls
under the masthead") makes an internally-paneled layout the wrong shape.

Software page, top to bottom:

1. `h1` and a standfirst paragraph.
2. A note stating versions are read from the running container.
3. Tools grouped by `PipelineType`, each group under a ruled category heading:
   Alignment, Trimming, Quality control, Variant calling, Download, Utility.

Each entry:

- Tool name as an italic serif headline, with an `id` anchor for deep linking.
- Version chip (see states below).
- `summary` as body prose.
- **How BioFlow uses it** -- the `usage` field.
- `strengths` as a list.
- A facts rail: license, citation, and links to homepage, repository, paper.

A tool belonging to two pipelines (`fastp` is TRIM and QC; `samtools` is
UTILITY and QC) appears under its first category only, with a cross-reference
line in the other. Rendering it twice would duplicate a long entry and make
the page's length misleading.

Sources page: the same ruled treatment, grouped by `kind`, with lighter
entries -- no version chip and no license, since neither applies.

### Version chip states

The live probe distinguishes three states, and the page shows all three
because each answers a different user question.

| Probe result | Chip | Why it matters |
| --- | --- | --- |
| `available`, `runnable` | version, accent tint | Normal case |
| `available: false` | "not installed", error tint | The description is still useful; the absence is the news |
| `available`, `runnable: false` | version + "not yet wired up" | `cutadapt` and `trimmomatic` today |

The third state is the one a static document could not express. `TOOL_META`
already distinguishes "the binary works" from "this application calls it"
(the `runnable` comment at `tools.py:295`), and a reference page that
conflated them would tell a user a tool is ready when no handler dispatches
to it.

An entry never disappears because its probe failed. The catalog is what the
page lists; the probe only decorates it.

### Styling

New `.software-*` and `.source-*` rules in `styles.css` beside the existing
`.help-*` block (line 2205), with appearance overrides in a new Broadsheet
section of `broadsheet.css`.

Structural rules go in `styles.css` and appearance in `broadsheet.css`,
matching the split the two files already maintain -- even though Broadsheet is
now the only theme, that separation is what keeps `broadsheet.css` a coherent
override layer.

`.help-page`'s `max-width: 760px` is too narrow for a facts rail beside prose.
The new pages take a wider measure, with the prose column itself still
measure-constrained so line length stays readable -- the same technique
`.detail-headline-main` uses (noted at `broadsheet.css:784`).

The facts rail collapses under the prose at narrow widths via
`grid-template-columns: repeat(auto-fit, minmax(...))`, following the
`.detail-columns` pattern at `broadsheet.css:808`, so no media query is
needed.

Broadsheet register for the entries: italic serif headlines, uppercase
letterspaced category kickers (`--ink-62`, `0.16em`), ruled dividers at
`--color-divider`, monospace version chips. Tokens only -- no literal hex.

## Research required during implementation

License, homepage, repository, and citation for all 17 tools are external
facts. Verify each against the project's own repository or documentation
rather than asserting from memory; licenses especially, where a wrong claim is
worse than a blank field.

Any field that cannot be confirmed stays empty. The rendering must therefore
treat every link and fact as optional -- an entry with no repository shows no
repository line rather than a dead link.

The four fields under the completeness test must be filled for all 17 before
that test passes, which is the intended forcing function.

## Verification

Backend: `docker compose exec api python -m pytest tests/ -q`.

Frontend: manual testing at localhost:5173, per CLAUDE.md -- there is no
component-testing setup. Rebuild first, from the main repo root:

```bash
docker compose up -d --build api web worker
```

Check specifically:

- Both pages reachable from the Help menu, and directly by URL.
- Versions match what the container actually has:
  `docker compose exec api fastp --version`.
- A tool with `runnable: false` (cutadapt, trimmomatic) shows the
  not-wired-up state, not a bare version.
- The not-installed state renders. Verify against a real absence rather than
  by trusting the branch: patch a probe off, or check whichever tool is
  genuinely missing from the image.
- The facts rail collapses cleanly at a narrow window.
- Every external link opens the right page.

## Out of scope

- **Per-tool parameter documentation.** Which flags BioFlow passes duplicates
  the launch dialogs and is the detail most likely to drift when a runner
  changes.
- **The technology stack.** FastAPI, MongoDB, React, Vite, Docker are
  infrastructure a user never reasons about; listing them as peers of fastp
  would dilute the page from "what analyzed my data" into a dependency dump.
- **Citation export.** BibTeX or RIS download is a plausible follow-on but not
  needed to answer "what version of what did I run".
