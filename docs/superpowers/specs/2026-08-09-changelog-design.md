# In-repo CHANGELOG.md from Conventional Commits

Design for [#106](https://github.com/syntheticgio/bioflow/issues/106).

## Problem

The GitHub release notes fixed by #105 read only **PR titles**, categorized
by PR label. The commit data is richer than what that consumes: between
`v0.1.0` and `v0.2.6` there were 218 commits, 106 typed `feat`/`fix`, and
**152 of the 218 (70%) carry body text** written at real depth. Nothing
produces a durable, in-repo record that uses that body text, and no
`CHANGELOG.md` exists.

Two facts shape everything below:

- Only **two app tags survive**: `v0.1.0` and `v0.2.6`. (`v0.2.0`–`v0.2.5`
  were cut and deleted after failed publishes, per VERSION.md's
  "fixing a bad release" policy.) The first generation must therefore cover
  the full pre-tag era, not a tidy one-release span.
- The release cut is one atomic command: `make release VERSION=x.y.z` →
  `ops/release.sh` bumps, commits `release: vX.Y.Z`, tags, pushes. A
  changelog that describes a tag must be committed **before** the tag
  exists, or the tag's tree silently lacks its own entry.

## Decisions

Each made against a named alternative. All format claims were validated by
generating against the real history with git-cliff 2.13.1 before committing
to them — the issue's own "worth generating one against real history"
criterion.

**git-cliff is the generator, pinned to 2.13.1.** Single Rust binary, TOML
config, per-tag sections, groups by type, conventional-commit parsing built
in. Rejected: a hand-rolled script — it would reimplement parsing, grouping,
and ordering that git-cliff already does, and every bug would be ours; and a
GitHub Action at tag time — the tag already exists by then, so a commit-back
leaves the tag's tree missing its own changelog section (the same
chicken-and-egg the repo already avoids by bumping and tagging in one
command).

**Generation happens inside `ops/release.sh`, folded into the release
commit.** The script already owns bump → commit → tag → push as one
indivisible command; the changelog is one more file in that commit. The
alternative — CI generates and commits back after the tag — breaks the
invariant that the tag's tree contains the changelog for the tag, and needs
a push token with a workflow loop-guard. Rejected.

**One entry per commit: subject + first paragraph of the body, truncated
at 200 characters.** Subject-only would reproduce what #105 already gives
you (PR titles); full bodies are a wall — the first generation alone has 426
+ 109 + 185 = 720 entries. The 200-character first paragraph keeps the body
text, which is the point of the issue, without turning the file into
`git log`. Scope is shown as `*(scope)*` where present, since scopes are
meaningful here (`frontend`, `api`, `queue`…).

**The changelog tracks the app line only.** `v*` and `launcher-v*` are two
independent version lines with independent tags and independent release
pipeline; one file for both reads as one line. `launcher`-scoped commits
inside an app range still appear (there are four; they are real changes to a
product the reader uses) but the launcher's own releases are not sections.
Rejected: one combined changelog.

**`[unreleased]` is never committed.** The committed file contains released
sections only — the convention git-cliff's own project follows, and the
property that makes the release-time `--prepend` flow compose (below). A
checked-in `[unreleased]` section would go stale between releases anyway,
since the file is only regenerated at cut time.

**Format decisions validated against real history** (all measured with
git-cliff 2.13.1, pinned):

| Decision | Measurement |
|---|---|
| 720 entries, one-time wall | `v0.1.0`: 426 entries (entire pre-tag era) · `v0.2.6`: 109 · unreleased: 185 (the repo merged 336 commits in the day since `v0.2.6`) |
| Sections newest-first; entries newest-first within a group | git-cliff `sort_commits = "newest"` |
| 200-char truncation keeps entries ≈2 lines | ~2,900-line file for 720 entries; without truncation it is roughly double |
| Type-grouping, not scope-grouping | 19 distinct scopes, 64 of 218 commits unscoped — scope groups would be noise |
| Tag pattern must be **anchored** | unanchored `v[0-9]*` matches `launcher-v0.1.0` (regex finds `v0` inside it) and leaks a launcher section |

## Constraints discovered, not chosen

**git-cliff's default commit parsers cannot be overridden.** User-defined
`commit_parsers` are *appended to* the embedded defaults and first-match
wins, so a custom `skip` parser for `docs` never fires — the default
`^doc` → "Documentation" parser matches first. The embedded defaults are:

```
feat → 🚀 Features      fix → 🐛 Bug Fixes      doc → 📚 Documentation
perf → ⚡ Performance    refactor → 🚜 Refactor  style → 🎨 Styling
test → 🧪 Testing        chore|ci → ⚙️ Miscellaneous   (catch-all) → 💼 Other
```

Consequence: filtering happens **in the template** — the body template
renders only the `🚀 Features` and `🐛 Bug Fixes` groups and drops every
other group at render time. This is stable only because the git-cliff
version is pinned; a smoke guard in `release.sh` (assert the new section
heading exists after generation) catches a template that silently stops
matching. `docs`, `test`, `refactor`, `chore`, `ci`, `release`, `build`,
`polish`, `style`, `tweak`, and merge commits are therefore excluded by
"never rendered", not by a parser.

**Breaking changes render as an inline marker, not a section.** With the
defaults first in match order, a `feat!:` commit is grouped under
`🚀 Features` with `breaking = true`; the template prefixes
`**BREAKING:** `. A dedicated "Breaking changes" group would require parser
replacement, which append semantics forbid. Zero breaking commits exist in
history, so this is a dormant path — but it was validated with a scratch
repo: `feat(api)!: drop the old endpoint` with a `BREAKING CHANGE:` footer
renders `- **BREAKING:** *(api)* drop the old endpoint` plus the footer's
first paragraph. The CLAUDE.md contract (`!` keys the major bump) is
unchanged; the changelog merely flags it.

**The issue's AI-summary option stays deferred**, for the reason the issue
gives: it would run in CI, need its own API key, and add a flaky network
call to a release path whose job is to be boring. Defer until a real
changelog shows grouping alone is insufficient.

## Mechanics

### `cliff.toml` (repo root, committed)

```toml
# git-cliff configuration for BioFlow (#106).
# Pins git-cliff 2.13.1 (see ops/release.sh bootstrap). The default commit
# parsers are kept: user-defined `commit_parsers` are APPENDED to the embedded
# defaults and first-match wins, so overriding them is not possible. Instead
# this template selects the groups that reach users (Features, Bug Fixes) and
# drops the rest (Documentation, Refactor, Testing, Styling, Miscellaneous,
# Other) at render time. Breaking commits are flagged inline.

[changelog]
header = """
# Changelog

All notable changes to BioFlow, generated from Conventional Commits by
[git-cliff](https://git-cliff.org). One entry per commit: the subject plus
the first paragraph of the body where one exists; only `feat` and `fix`
reach the notes. See AGENTS.md "Release notes" for the contract.

"""
body = """
{% if version %}\
## [{{ version | trim_start_matches(pat="v") }}] - {{ timestamp | date(format="%Y-%m-%d") }}
{% else %}\
## [unreleased]
{% endif %}
{% for group, commits in commits | group_by(attribute="group") %}
{% set g = group | striptags | trim %}
{% if g == "🚀 Features" or g == "🐛 Bug Fixes" %}
### {{ g }}
{% for commit in commits %}
- {% if commit.breaking %}**BREAKING:** {% endif %}{% if commit.scope %}*({{ commit.scope }})* {% endif %}{{ commit.message | trim }}{% if commit.body %}
  {{ commit.body | split(pat="\n\n") | first | trim | truncate(length=200) }}{% endif %}
{% endfor %}
{% endif %}
{% endfor %}
"""
trim = true
render_always = true
postprocessors = []

[git]
conventional_commits = true
filter_unconventional = true
split_commits = false
protect_breaking_commits = false
filter_commits = false
fail_on_unmatched_commit = false
tag_pattern = "^v[0-9]+\\.[0-9]+\\.[0-9]+$"
use_branch_tags = true
topo_order = false
topo_order_commits = true
sort_commits = "newest"
```

Notes: `use_branch_tags` keeps worktree generations from picking up tags
not merged into the current branch. `filter_unconventional` drops the
pre-Conventional-Commit history (590 commits across the full history) —
intended. The release commit itself is typed `release:` and is excluded by
"never rendered".

### Bootstrap: a pinned binary, not a PATH dependency

`ops/release.sh` currently needs only `bash`, `git`, `python3`. Adding a
`brew`/`cargo` dependency to the release path would let the toolchain drift.
Instead the script downloads a pinned release binary once and caches it:

- Cache: `${XDG_CACHE_HOME:-$HOME/.cache}/bioflow-tools/git-cliff-2.13.1/`
  (version in the path so upgrades never collide)
- Source: `github.com/orhun/git-cliff/releases/download/v2.13.1/`
  `git-cliff-<aarch64|x86_64>-apple-darwin.tar.gz`, chosen from `uname -m`;
  any non-macOS host dies with a message rather than silently using the
  wrong binary
- Verified against the pinned SHA-512 from the release (checked with
  `shasum -a 512`); a mismatch aborts the release

### `release.sh` integration

Between the existing bump and the commit:

```bash
# after bump_version.py writes the version files
bootstrap_git_cliff                        # download + verify once, cached
"$GIT_CLIFF" --unreleased --tag "$TAG" --prepend CHANGELOG.md
grep -q "^## \\[${VERSION}\\]" CHANGELOG.md || die "changelog generation produced no section for $TAG"
```

`--unreleased --tag vX.Y.Z` renders the commits since the last tag as the
`vX.Y.Z` section without the tag existing yet (validated: `--tag v9.9.9`
renders `## [9.9.9] - <today>`). `--prepend` inserts that section before the
existing content and **leaves existing sections byte-identical** (measured:
a 2,886-line file grew by exactly the 768-line new section). Because
`[unreleased]` is never committed, no stale section accumulates. The release
commit therefore contains the bumped version files **and** the new changelog
section; the annotated tag's tree then contains the changelog that describes
it.

`CHANGELOG.md` is added to the `git add` list (the script stages files from
`bump_version.py` output; the changelog is a new member of that list). The
`release:` commit itself is excluded from future generations by the
template's group selection, so the loop closes.

### First generation (implementation step)

Landing this feature is itself a release-shaped event: `CHANGELOG.md` must
be created before the next tag, covering the 720-entry wall in one commit.

```bash
git cliff -o CHANGELOG.md   # full history: [unreleased] + [0.2.6] + [0.1.0]
# strip the leading [unreleased] section (never committed)
awk 'BEGIN{skip=0} /^## \[unreleased\]/{skip=1; next} /^## \[/{skip=0} !skip' \
  CHANGELOG.md > CHANGELOG.md.tmp && mv CHANGELOG.md.tmp CHANGELOG.md
```

commit `CHANGELOG.md` + `cliff.toml` on main via PR. From then on, every
`make release` prepends a small section.

## What this does not change

- **The GitHub release body.** The #105 label-categorized PR-title notes
  remain what `publish-images.yml`'s `release` job publishes. `CHANGELOG.md`
  is the durable in-repo record; they answer different questions and neither
  depends on the other. No workflow change.
- **The CLAUDE.md breaking-change contract.** The `!`/`BREAKING CHANGE:`
  marker still keys major bumps; the changelog only renders it.
- **`#107` (alpha/beta tags).** Compatible by construction: once `release.sh`
  can cut `-alpha`/`-beta` tags, the same `--unreleased --tag` flow renders
  `[v0.3.0-alpha]` sections and git-cliff orders them by version. Nothing in
  this spec depends on #107 landing first.

## Verification

- **Spec-time (done):** generated against real history with pinned 2.13.1 —
  sections `[unreleased]` (185), `[0.2.6]` (109), `[0.1.0]` (426);
  launcher section absent; scope shown; bodies truncated; breaking flagged
  in a scratch repo; `--prepend` idempotent on existing sections.
- **Release-time:** the `grep` guard in `release.sh` fails the cut if the
  template breaks silently; a manual check that the tag's tree contains its
  section is `git show v0.2.7:CHANGELOG.md | head`.
- **Idempotency:** regenerating the committed file from the same tree
  produces byte-identical sections (generation is a pure function of
  history + config).

## Docs to update (same PR as the implementation)

- `VERSION.md` — add `CHANGELOG.md` to the "What each line writes" table and
  one line under "Cutting a release".
- `AGENTS.md` and `CLAUDE.md` — the "Release notes come from PR titles"
  section's second known-gap bullet ("Generating a CHANGELOG.md ... is
  future work") is now a description of the shipped mechanism.
- `.github/release.yml` — its comment says the title prefix "does no work
  here" except for "any future commit-driven generator (#106)"; that future
  is now, so the comment should say where it lands.
