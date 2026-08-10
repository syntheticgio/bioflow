# Staged alpha/beta releases

Design for [#107](https://github.com/syntheticgio/bioflow/issues/107).

## Problem

The four-stage release methodology adopted 2026-08-09
(`assets/BioFlowReleasePath.svg`, `assets/BioFlowReleaseLifecycle.svg`) calls
for alpha and beta stages with their own branches and tags:

| Stage | Branch | Tag |
|---|---|---|
| Alpha | `alpha/X.Y.Z` | `vX.Y.Z-alpha` |
| Beta | `beta/X.Y.Z` | `vX.Y.Z-beta` |
| Production | `release/X.Y.Z` | `vX.Y.Z` |

**The tooling cannot do this today.** `ops/release.sh` hard-refuses anything
that is not bare `MAJOR.MINOR.PATCH`:

```bash
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || die "'$VERSION' is not semver MAJOR.MINOR.PATCH (no 'v', no -rc suffix)"
```

`ops/lib/bump_version.py` carries the identical regex, and `VERSION.md` states
pre-release versions are unsupported. Both predate the methodology.

The consequence is a methodology that cannot be executed with the tooling. The
only route to an `vX.Y.Z-alpha` tag is typing it by hand, which
`publish-images.yml`'s version-guard exists to catch — a hand-rolled tag
publishes images labelled with a version the tree does not have. The gap is
real and documented in three places: `VERSION.md`'s header note, AGENTS.md /
CLAUDE.md's methodology section, and `docs/TODO.md`.

## Decisions

Six choices, each made against a named alternative.

**The version suffix is the stage; the script stays one command.**
`make release VERSION=0.3.0-alpha` is the whole invocation. The `-alpha` /
`-beta` suffix alone drives branch selection, `--prerelease`, and the
changelog boundary. Rejected: stage flags threaded through the Makefile
(`make release-alpha ...`). A flag is a second source of truth — a version
that says `-beta` while the flag says `alpha` becomes representable, and the
mapping from version to stage is exactly the thing that must never disagree.

**Stage branches are auto-created from the current stage's tip.** Cutting
`0.3.0-alpha` from `main` creates `alpha/0.3.0`; cutting `0.3.0-beta` from
`alpha/0.3.0` creates `beta/0.3.0`; cutting `0.3.0` from `main` or
`beta/0.3.0` creates `release/0.3.0`. One command performs the whole cut
including branch creation, matching the methodology's "cut from" language
("alpha is cut from main once"). Rejected: requiring the stage branch to
pre-exist and be checked out. That makes the cut two manual steps instead of
one and re-introduces the "bumped but never tagged"-class of error this
script exists to make unreachable. The preflight asserts the *source* branch
per stage; it does not drop the guard.

**Production can be cut from `main` or `beta/X.Y.Z`.** The full
alpha→beta→release gauntlet is the *tested* path, but a one-line patch is not
a release worth staging. Cutting bare `0.2.7` from `main` preserves today's
quick-patch flow verbatim (it just lands on `release/0.2.7` instead of on
`main`). Rejected: requiring `beta/X.Y.Z` for every production cut. Forcing a
single-user, non-critical tool through a gauntlet of staging branches for
every patch is process the repo's own charter ("optimize for the person using
it can see their change") argues against.

**Pre-release tags publish images, and never move `:latest`.**
`publish-images.yml` already triggers on `tags: [v*]`, so `v0.3.0-alpha`
pushes build and publish under `0.3.0-alpha` plus the `sha-…` handle. An
alpha tester pulls the exact build under test — that is what the alpha stage
is for. `:latest` must not move, or an alpha ships to every launcher on the
Update button. Both current `latest` mechanisms are already safe by
construction (`type=raw,value=latest` is gated on `is_default_branch`, and
metadata-action's `latest=auto` flavor never selects a pre-release version),
and the spec verifies the latter empirically rather than trusting the claim.
Rejected: gating the build jobs off for pre-release tags. The alpha tag would
then carry no images, and testing the alpha would require a local build —
which is precisely the failure the alpha stage exists to catch.

**Each stage tag gets its own CHANGELOG.md section.** The `cliff.toml`
`tag_pattern` widens to accept the suffix, so `v0.3.0-alpha` and
`v0.3.0-beta` become changelog boundaries. `## [0.3.0-alpha]` lists
everything since the last production tag; `## [0.3.0-beta]` lists only the
beta delta; `## [0.3.0]` lists only the production delta. Main's changelog
accumulates the full three-section story for a version via the existing
merge-back flow. Rejected: production-only sections, keeping the narrow
`tag_pattern`. The beta cut would then re-render the alpha's commits into the
beta section (git-cliff would see no boundary at the alpha tag), or the
stage cuts would have to skip changelog generation entirely — which breaks
#106's invariant that the tag's tree contains the section for that tag.

**The pre-release suffix is written verbatim into all four declarations.**
VERSION, `backend/app/version.py`, `backend/pyproject.toml`, and
`frontend/package.json` all say `0.3.0-alpha` during alpha. This is forced,
not chosen: `check_tag_matches_version.sh` compares the tag against the tree
exactly, and a tag `v0.3.0-alpha` against a tree that says `0.3.0` would
fail the version-guard. The About page and version endpoint honestly report
`0.3.0-alpha`. All three declaration formats accept it: PEP 440 (`0.3.0a0`
normalized), npm semver, and Cargo semver.

**The launcher line is untouched.** It versions independently
(`launcher-v*` tags, its own workflow), and the methodology's staging is
about the app line.

## Mechanics

### `ops/release.sh`

- Version regex becomes `^[0-9]+\.[0-9]+\.[0-9]+(-alpha|-beta)?$`. Anything
  else (`-rc`, `-ALPHA`, `v` prefix, `.1` build metadata) refuses.
- Stage derivation from the suffix, in the preflight:

  | New version | Must be on | Creates / uses | Pushes |
  |---|---|---|---|
  | `X.Y.Z-alpha` | `main` | `alpha/X.Y.Z` | `alpha/X.Y.Z` + tag |
  | `X.Y.Z-beta` | `alpha/X.Y.Z` | `beta/X.Y.Z` | `beta/X.Y.Z` + tag |
  | `X.Y.Z` | `main` or `beta/X.Y.Z` | `release/X.Y.Z` | `release/X.Y.Z` + tag |

- Branch creation: `git switch -c <target>`. Recovery: if the target branch
  already exists and points at the current `HEAD` (a previous cut that died
  before the push), check it out and proceed; if it points elsewhere, die
  naming the branch. The tag-exists checks are unchanged and catch a
  re-cut of an already-tagged version.
- The ordering check is unchanged: `sort -V` already orders
  `0.2.6 < 0.3.0-alpha < 0.3.0-beta < 0.3.0`, and the "not equal / must be
  greater" logic uses it. This also blocks regressions (cutting `0.3.0`
  while VERSION says `0.3.0-alpha` refuses: `0.3.0-alpha` sorts lower).
- The push line becomes `git push -u origin refs/heads/<target>
  refs/tags/<tag>` instead of the hardcoded `git push origin main ...`.
- The changelog step (app line only, #106) is unchanged in shape:
  `--unreleased --tag "$TAG" --prepend CHANGELOG.md` plus the
  `grep -Fq "## [$VERSION]"` guard. It works for stage tags once `cliff.toml`
  accepts the suffix (below).

### `ops/lib/bump_version.py`

The `SEMVER` regex becomes `^\d+\.\d+\.\d+(-alpha|-beta)?$`, matching
`release.sh`. Nothing else changes: the textual substitution logic is
suffix-agnostic.

### `cliff.toml`

Two lines widen, identically:

```toml
tag_pattern = "^v[0-9]+\.[0-9]+\.[0-9]+(-alpha|-beta)?$"
# and the annotated-tag-message skip parser:
{ message = "^v[0-9]+\.[0-9]+\.[0-9]+(-alpha|-beta)?$", skip = true },
```

The first makes each stage tag a changelog boundary (so a beta section lists
only the beta delta). The second keeps the annotated tag message
(`"v0.3.0-alpha"`, which is not a real commit) out of the entries — without
the widened skip parser the stage tag messages would leak in exactly the way
the bare `"v0.2.6"` messages did before #106.

### `.github/workflows/publish-images.yml`

- The `release` job passes `--prerelease` to `gh release create` /
  `gh release edit` when the tag contains `-alpha` or `-beta`. Title stays
  `BioFlow 0.3.0-alpha`.
- The comment above the metadata-action step ("...moves it for semver tags")
  is corrected to state the pre-release exception. No step change: the
  `latest` raw tag is gated on `is_default_branch`, and `latest=auto` skips
  pre-release versions.
- The version-guard and build/manifest jobs need no change: the guard is
  prefix-agnostic and `v0.3.0-alpha` ↔ VERSION `0.3.0-alpha` matches; the
  metadata-action `type=semver,pattern={{version}}` produces `0.3.0-alpha`
  for the tag.

### `Makefile`

No functional change (`test -n "$(VERSION)"` passes a suffixed version
through). The usage comment gains the stage example.

### Docs

- `VERSION.md`: the header note ("documents the tooling as it exists today,
  which predates the methodology") goes away; "What it refuses" describes the
  staged branch rules instead of `main`-only; "Cutting a release" gains the
  stage commands and the note that the operator is left on the stage branch;
  "Fixing a bad release" covers pre-release tags (a bad alpha is fixed
  forward with a new alpha or beta — the tag, images, and release stay, per
  the existing "do not move or delete a published tag" policy).
- `AGENTS.md` / `CLAUDE.md`: the methodology section's "script cannot cut
  -alpha/-beta" note becomes a description of the shipped capability.
- `docs/TODO.md`: the entry that names this gap (if present) moves to
  `docs/TODO-done.md` with the ` — FIXED` convention.

## Verification

- **Ordering.** Explicit assertions that `sort -V` orders
  `0.2.6 < 0.3.0-alpha < 0.3.0-beta < 0.3.0 < 0.3.1-alpha`, that an equal
  version refuses, and that a lower version refuses.
- **Preflight refusals** (`ops/tests/test_release_preflight.py`, all
  exit-before-bootstrap so the suite stays network-free):
  - `-alpha` cut from a non-`main` branch refuses;
  - `-beta` cut from anything but `alpha/X.Y.Z` refuses;
  - bare cut from `alpha/X.Y.Z` refuses (neither `main` nor `beta/X.Y.Z`);
  - `0.3.0-rc`, `v0.3.0`, `0.3.0-ALPHA` refuse on the regex;
  - a `-beta` cut from `main` refuses — the source branch is not
    `alpha/X.Y.Z`, whether or not that branch exists.
- **Bump tests** (`ops/tests/test_bump_version.py`): `-alpha`/`-beta`
  accepted and written to all four declarations; the refusals above rejected.
- **Tag guard** (`ops/tests/test_tag_guard.py`): `v0.3.0-alpha` passes
  against VERSION `0.3.0-alpha`; a mismatch dies.
- **`:latest` immobility.** Run docker/metadata-action's standalone test
  harness against a `v0.3.0-alpha` ref and assert the generated tag set
  contains `0.3.0-alpha` and `sha-…` but not `latest`.
- **Changelog boundaries.** Against the repo's real history (or a scratch
  repo with staged tags), generate with the widened `cliff.toml` and confirm
  a `vX.Y.Z-alpha` tag acts as a boundary: the section for the next stage
  lists only the delta, and no `v0.3.0-alpha` tag-message junk appears.
- **Happy-path cut.** Manual: a scratch remote, cut `0.3.0-alpha` from
  `main`, `0.3.0-beta` from `alpha/0.3.0`, `0.3.0` from `beta/0.3.0`;
  confirm branches, tags, `--prerelease` release, and changelog sections at
  each step.

## Out of scope

- The launcher version line (`launcher-v*`).
- Suffixes other than `-alpha` / `-beta` (`-rc`, numeric build metadata).
- Moving `:latest` for pre-release tags — this spec asserts it must not and
  verifies it does not.
- AI-written release summaries (deferred by #106's design for the same
  reasons: a flaky network call in a release path whose job is to be boring).

## Docs to update (same PR as the implementation)

- `VERSION.md` — header note, "What it refuses", "Cutting a release",
  "Fixing a bad release".
- `AGENTS.md` and `CLAUDE.md` — methodology section's gap note.
- `Makefile` — usage comment.
