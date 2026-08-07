# Version management

Design for [#53](https://github.com/syntheticgio/bioflow/issues/53).

## Problem

Five files declare a version. All five say `0.1.0`, by coincidence rather
than by construction — nothing checks them against each other, and only two
have a consumer that would notice if they drifted:

- `backend/app/main.py:93` — a hardcoded literal passed to `FastAPI(version=)`,
  rendered at `/docs`. User-visible and already the most likely to go stale,
  because nothing about editing it is load-bearing for anything else.
- `launcher/src-tauri/Cargo.toml` — Tauri reads it for the bundle filename and
  the macOS `Info.plist`.

The other three (`backend/pyproject.toml`, `frontend/package.json`,
`launcher/package.json`) are read by nothing: this repo publishes no Python
dist and no npm package.

Releases today are `git tag` and nothing else. `git tag v0.2.0 && git push
--tags` publishes GHCR images labelled `0.2.0` while every file in the tree
still says `0.1.0`, and nothing anywhere reports the mismatch.

`publish-images.yml` fires on `v*` and pushes images, but creates no GitHub
release. The only release that exists is `launcher-v0.1.0`, from
`release-launcher.yml`. So the issue's completion check — "version is
successfully used for a release" — is currently satisfiable for the launcher
and not for the app.

## Decisions

Four choices, each made against a named alternative.

**The app and the launcher version independently.** Two tag namespaces
already exist (`v*`, `launcher-v*`) and two pipelines already key off them.
A macOS build is signed and notarized, which is slow and depends on a
certificate that expires yearly; there is no reason an API change should
trigger one. The launcher pulls `:latest` images and has no version
negotiation, so there is no compatibility contract that lockstep versioning
would be protecting. Rejected: one shared version, and a launcher-declared
minimum supported app version — the latter is machinery for a problem that
does not exist yet.

**A script bumps and tags in one command.** The alternative — bump by hand,
tag by hand, let CI verify they agree — is cheaper to build but its failure
mode is the one that actually happens: bump without tagging, or tag without
bumping, then delete a pushed tag and redo it. Doing both in one command
removes the class of error rather than detecting it. Also rejected: deriving
everything from the git tag with nothing committed, which removes the same
error class but gives up being able to answer "what version is this
checkout?" from the tree — which matters more here than usual, because work
happens in worktrees where `git describe` is often not the useful answer.

**The version is visible at runtime, not only at build time.** The launcher's
update check compares image *digests*, not versions, so a user running
`:latest` has no way to discover what they have. An endpoint plus a line on
the About page closes that. Rejected: fixing the `main.py` literal and
stopping — it satisfies the letter of the issue but leaves "what am I
running?" unanswerable without shelling into a container.

**`Cargo.toml` stays the launcher's source of truth.** An earlier draft gave
the launcher its own `VERSION` file for symmetry with the app line. That
symmetry serves the reader of this document, not anyone operating the
release: it adds a file whose only job is to duplicate a value Cargo already
holds correctly, and which Tauri already reads. The app line needs a `VERSION`
file because its runtime declaration (`version.py`) is generated and there is
no other natural home; the launcher has no equivalent gap.

## Design

### Two version lines

| | App | Launcher |
|---|---|---|
| Source of truth | `VERSION` (repo root) | `launcher/src-tauri/Cargo.toml` |
| Tag | `v0.2.0` | `launcher-v0.2.0` |
| Workflow | `publish-images.yml` | `release-launcher.yml` |
| Produces | GHCR backend + web images, GitHub release | `.dmg`, `.deb`, `.rpm` on a GitHub release |

They move independently and never need to agree.

### Files the bump writes

App line, from `VERSION`:

- `VERSION` — one line, the bare version, no `v` prefix, trailing newline.
- `backend/app/version.py` — **new, generated, committed**. A single
  `__version__ = "0.2.0"` plus a header comment saying it is generated and
  naming the script. The backend needs the version at import time; this is
  the path with no Docker build-arg plumbing and no file parsing at runtime.
- `backend/pyproject.toml` — no consumer, kept consistent so the tree does
  not contradict itself.
- `frontend/package.json` — same.

Launcher line, from `Cargo.toml`:

- `launcher/src-tauri/Cargo.toml` — the source of truth; the script edits it.
- `launcher/package.json` — no consumer, kept consistent.

`main.py:93`'s literal is **deleted**, replaced by
`from app.version import __version__` and `version=__version__`. It does not
become a sixth thing the script writes: a declaration that only exists to be
kept in sync is the same trap one level down.

### The bump script

`ops/release.sh <app|launcher> <version>`, wrapped by two Makefile targets
alongside the existing ones:

```
make release VERSION=0.2.0
make release-launcher VERSION=0.1.1
```

Preflight — refuse, with a message naming the specific failure:

1. The version is valid semver (`MAJOR.MINOR.PATCH`, no pre-release or build
   metadata — see Out of scope).
2. The working tree is clean.
3. The current branch is `main`.
4. The tag does not already exist, locally or on `origin`.
5. The new version is greater than the current one.

Then, in order: write the files for that line; commit as `release: v0.2.0`
(or `release: launcher-v0.1.1`) touching only version declarations; create an
annotated tag; push the commit and the tag together, which is what fires CI.

The `main`-branch check is a hard refusal, not a warning, even though much of
this repo's work happens in worktrees. Releasing from a worktree is not a
thing that should be happening, and if this turns out to obstruct a real
workflow, loosening it is a one-line change. Starting strict and relaxing on
evidence is the cheaper direction to be wrong in.

### Runtime visibility

`GET /api/v1/version` → `{"version": "0.2.0"}`, added to `system.py` (which
is already mounted under `api_router`'s `/api/v1` prefix; `health.py` is
mounted unprefixed at the root and is therefore the wrong home for a path
stated as `/api/v1/version`). It reads `app.version.__version__`.

`HelpAbout.tsx` renders the version under the page title. `HelpAbout` rather
than `HelpSoftware`, because `HelpSoftware` renders `TOOL_META` — it is about
the bioinformatics tools BioFlow drives, not about BioFlow. If the fetch
fails the line renders nothing; a missing version must never break the page.

### CI

**Tag/VERSION agreement guard.** Both workflows gain a first step that fails
if the tag does not match the committed source of truth — `VERSION` for
`publish-images.yml`, `Cargo.toml` for `release-launcher.yml`. Redundant
behind the script by construction, which is the point: it is what catches a
hand-rolled `git tag`.

**GitHub release for `v*`.** A new job in `publish-images.yml`, gated on the
image jobs succeeding, creates the release for the tag with auto-generated
notes and a body listing the image tags that were pushed. This is what makes
the issue's completion check true for the app line.

### VERSION.md

A `VERSION.md` at the repo root, written for whoever is cutting a release
(including an agent). It covers: the two independent lines and which
artifacts each governs; the scheme and what earns a major, minor, or patch
bump; the exact commands; every file each line writes and which of them have
real consumers; what CI does on each tag and what a failed guard means; how
to verify a release landed (GHCR tags present, release page created, bundles
attached, `/api/v1/version` reporting the new number); and how to recover
from a bad release — which is to push a new patch version, not to move or
delete a published tag, since GHCR images and release assets are already
downstream of it.

It is documentation, not a source of truth: it must not restate the current
version number anywhere, or it becomes a sixth thing that goes stale.

## Testing

- The script's preflight refusals, one test per condition, against temporary
  git repositories.
- The script writes every file it claims to and produces valid TOML/JSON.
- **`VERSION` and `backend/app/version.py` agree** — a pytest that reads both.
  This is the test that catches a hand-edit of one without the other, and the
  one that would have caught the original drift.
- `/api/v1/version` returns what `app.version.__version__` holds.
- The CI guard, exercised by running its check script against a matching and
  a mismatching pair.

The About page line is verified in the browser, per the repo's convention
that UI-facing changes are checked at the running app rather than in a
component test.

## Out of scope

Pre-release, rc, and build-metadata versions. Changelog generation beyond
GitHub's auto-notes. Any launcher/app compatibility checking. Version labels
on the web image or baked into the frontend build. Conventional-commit
enforcement. A `--dry-run` flag on the script.
