#!/usr/bin/env bash
# Cut a release: bump the version declarations, commit, tag, push.
#
# Bumping and tagging are one command on purpose. Done as two manual steps
# the failure mode is "bumped but never tagged" or "tagged but never bumped",
# and recovering from the second means deleting a pushed tag that CI has
# already acted on. One command makes both states unreachable.
#
#   ops/release.sh app 0.2.0        -> tag v0.2.0, branch release/0.2.0
#   ops/release.sh app 0.3.0-alpha  -> tag v0.3.0-alpha, branch alpha/0.3.0
#   ops/release.sh app 0.3.0-beta   -> tag v0.3.0-beta, branch beta/0.3.0
#   ops/release.sh launcher 0.1.1   -> tag launcher-v0.1.1
#
# See VERSION.md for the operator's guide and what CI does with each tag.

set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

usage() {
  cat >&2 <<'EOF'
usage: ops/release.sh <app|launcher> <version>

  app       bump VERSION, backend/app/version.py, backend/pyproject.toml,
            frontend/package.json  -> tag v<version>
  launcher  bump launcher/src-tauri/Cargo.toml, launcher/package.json
            -> tag launcher-v<version>

<version> is bare semver: 0.2.0, not v0.2.0.
EOF
  exit 2
}

[ $# -eq 2 ] || usage
LINE="$1"
VERSION="$2"

case "$LINE" in
  app)      TAG_PREFIX="v" ;;
  launcher) TAG_PREFIX="launcher-v" ;;
  *)        usage ;;
esac

TAG="${TAG_PREFIX}${VERSION}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# --- git-cliff bootstrap ---------------------------------------------------
# git-cliff regenerates CHANGELOG.md from Conventional Commits (#106). The
# version and tarball checksums are pinned here so the changelog is
# reproducible, and the binary is cached under the user cache dir so a release
# does not depend on a brew/cargo toolchain and does not re-download on every
# cut. Bootstrap is only reachable from the `app` line below.
GIT_CLIFF_VERSION="2.13.1"
GIT_CLIFF_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/bioflow-tools/git-cliff-${GIT_CLIFF_VERSION}"
GIT_CLIFF_BIN="$GIT_CLIFF_DIR/git-cliff"

# SHA-512 of the release tarball per architecture. A download that fails the
# checksum aborts the release rather than producing a changelog nobody can
# reproduce.
GIT_CLIFF_SHA512_aarch64="b4f1957f73b0b87ca2113dc58cdf2828f7452f3e405444978089a9f6fdb3dc654e1f478eaf402831ec9350692b4e24e1d8e92e41485f3423664c8e6f8fb59f16"
GIT_CLIFF_SHA512_x86_64="1192fc8f7ef532c0ec245c5012c19594e3d8d08fe4c592f748e80a260088e1c8ee3d4ec21130fe3e2be0efb08434d283ec0e2eb972cdd1f7b6a5ada35b107337"

bootstrap_git_cliff() {
  [ -x "$GIT_CLIFF_BIN" ] && return 0
  local target sha
  case "$(uname -s)/$(uname -m)" in
    Darwin/arm64)  target="aarch64-apple-darwin"; sha="$GIT_CLIFF_SHA512_aarch64" ;;
    Darwin/x86_64) target="x86_64-apple-darwin";  sha="$GIT_CLIFF_SHA512_x86_64" ;;
    *) die "git-cliff bootstrap only supports macOS (got $(uname -s)/$(uname -m))" ;;
  esac
  mkdir -p "$GIT_CLIFF_DIR"
  local tarball="$GIT_CLIFF_DIR/git-cliff.tar.gz"
  curl -fL -o "$tarball" \
    "https://github.com/orhun/git-cliff/releases/download/v${GIT_CLIFF_VERSION}/git-cliff-${GIT_CLIFF_VERSION}-${target}.tar.gz" \
    || die "could not download git-cliff ${GIT_CLIFF_VERSION}"
  local actual
  actual="$(shasum -a 512 "$tarball" | awk '{print $1}')"
  [ "$actual" = "$sha" ] \
    || die "git-cliff download failed its checksum (got ${actual}, want ${sha})"
  tar -xzf "$tarball" -C "$GIT_CLIFF_DIR" --strip-components=1
  rm -f "$tarball"
  [ -x "$GIT_CLIFF_BIN" ] || die "git-cliff tarball did not contain the binary"
}

# --- preflight -------------------------------------------------------------
# Every check refuses rather than warns, and names the precondition it tripped.

VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+(-alpha|-beta)?$'
[[ "$VERSION" =~ $VERSION_RE ]] \
  || die "'$VERSION' is not semver MAJOR.MINOR.PATCH, optionally -alpha or -beta (no 'v', no -rc)"

[ -z "$(git status --porcelain)" ] \
  || die "working tree is not clean -- commit or stash first"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# Staged branches are an app-line mechanism (#107): the version suffix IS
# the stage, and the release lands on alpha/X.Y.Z / beta/X.Y.Z /
# release/X.Y.Z. The launcher line keeps its pre-existing behavior -- cut
# from main, push main and the launcher-v tag, no stage branch.
if [ "$LINE" = "app" ]; then
  # CORE is the bare version the stage branch is named after: `alpha/0.3.0`,
  # not `alpha/0.3.0-alpha`.
  CORE="${VERSION%-alpha}"
  CORE="${CORE%-beta}"
  case "$VERSION" in
    *-alpha)
      TARGET="alpha/$CORE"
      # Retrying a cut that died after switching (see the branch check below)
      # legitimately starts from the target branch itself.
      [ "$BRANCH" = "main" ] || [ "$BRANCH" = "$TARGET" ] \
        || die "an alpha release must be cut from main, not '$BRANCH'"
      ;;
    *-beta)
      TARGET="beta/$CORE"
      [ "$BRANCH" = "alpha/$CORE" ] || [ "$BRANCH" = "$TARGET" ] \
        || die "a beta release must be cut from alpha/$CORE, not '$BRANCH'"
      ;;
    *)
      TARGET="release/$CORE"
      [ "$BRANCH" = "main" ] || [ "$BRANCH" = "beta/$CORE" ] || [ "$BRANCH" = "$TARGET" ] \
        || die "a production release must be cut from main or beta/$CORE, not '$BRANCH'"
      ;;
  esac

  # The stage branch must be usable: absent (created below) or already at HEAD
  # (a previous cut that died between switching and pushing). One pointing at a
  # different commit is a different tree than the operator's checkout, so the
  # release must not happen -- checked here, before any bump/commit/tag.
  if git rev-parse -q --verify "refs/heads/$TARGET" >/dev/null; then
    [ "$(git rev-parse HEAD)" = "$(git rev-parse "$TARGET")" ] \
      || die "branch $TARGET exists but does not point at HEAD -- inspect it before cutting"
  fi
else
  [ "$BRANCH" = "main" ] \
    || die "launcher releases are cut from main, not '$BRANCH'"
  # The escape hatch ships a launcher fix without rebuilding images (#335).
  # It is production-only: staging a component through alpha/beta branches
  # when it has no images to be tested against is ceremony with no payoff.
  case "$VERSION" in
    *-alpha|*-beta)
      die "a launcher-only release cannot be a pre-release -- cut it as a production version, or use 'make release' to include the launcher in a staged app release" ;;
  esac
fi

git rev-parse -q --verify "refs/tags/$TAG" >/dev/null \
  && die "tag $TAG already exists locally"
if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
  die "tag $TAG already exists on origin"
fi

# Both lines compare against VERSION (#335). For the launcher that enforces
# the invariant "launcher version >= app version", which is what lets a
# combined cut overwrite the launcher version unconditionally without ever
# rewinding a number that has already shipped in a bundle filename.
CURRENT="$(tr -d '[:space:]' < VERSION)"
[ -n "$CURRENT" ] || die "could not read the current version from VERSION"

# Versions compared by a normalized key: CORE + stage rank (alpha=1, beta=2,
# production=3), so 0.3.0-alpha < 0.3.0-beta < 0.3.0 under any sort -V, not
# just GNU's. macOS's BSD sort -V orders a pre-release suffix AFTER the bare
# version (0.3.0 < 0.3.0-alpha), which would silently make a beta release
# unable to graduate to production. Equal versions are caught first.
rank_version() {
  local v="$1" core stage
  case "$v" in
    *-alpha) core="${v%-alpha}"; stage="1" ;;
    *-beta)  core="${v%-beta}";  stage="2" ;;
    *)       core="$v";           stage="3" ;;
  esac
  printf '%s.%s\n' "$core" "$stage"
}

[ "$VERSION" != "$CURRENT" ] || die "$VERSION is already the current version"
GREATER="$(printf '%s\n%s\n' "$(rank_version "$CURRENT")" "$(rank_version "$VERSION")" | sort -V | tail -n1)"
[ "$GREATER" = "$(rank_version "$VERSION")" ] \
  || die "$VERSION is not greater than the current version $CURRENT"

# --- bump, commit, tag, push ----------------------------------------------

echo "Releasing $LINE $CURRENT -> $VERSION (tag $TAG)"

# Command substitution (not process substitution) so a nonzero exit from
# bump_version.py is seen by `set -e` and aborts the script immediately --
# a `while ... done < <(cmd)` pipeline's exit status is the loop's, not
# cmd's, so `set -e` can't catch a failure inside it.
BUMP_OUTPUT="$(python3 "$SCRIPT_DIR/lib/bump_version.py" "$LINE" "$VERSION" --root "$REPO_ROOT")"
WRITTEN=()
while IFS= read -r line; do
  [ -n "$line" ] && WRITTEN+=("$line")
done <<< "$BUMP_OUTPUT"
[ "${#WRITTEN[@]}" -gt 0 ] || die "bump wrote no files"

# Sync the lockfile version after bumping package.json so npm ci does not
# fail on a version mismatch in CI (#491).
if [ "$LINE" = "app" ]; then
  cd "$REPO_ROOT/frontend"
  npm install --package-lock-only --silent
  cd "$REPO_ROOT"
  WRITTEN+=("frontend/package-lock.json")
fi

# Regenerate CHANGELOG.md so the release commit carries the section for this
# tag. The changelog tracks the app line only (#106); the launcher line keeps
# its GitHub release notes. `--unreleased --tag` renders the commits since the
# last tag under the new version before the tag exists; `--prepend` inserts
# that section and leaves the older sections untouched.
if [ "$LINE" = "app" ]; then
  bootstrap_git_cliff
  "$GIT_CLIFF_BIN" --unreleased --tag "$TAG" --prepend CHANGELOG.md
  grep -Fq "## [$VERSION]" CHANGELOG.md \
    || die "changelog generation produced no section for $TAG -- refusing to release without it"
  WRITTEN+=("CHANGELOG.md")
fi

git add -- "${WRITTEN[@]}"
git commit -m "release: $TAG"
git tag -a "$TAG" -m "$TAG"

if [ "$LINE" = "app" ]; then
  # Move the release commit onto the stage branch, then push branch and tag
  # together: a pushed tag whose commit never landed is a tag CI cannot check
  # out.
  if git rev-parse -q --verify "refs/heads/$TARGET" >/dev/null; then
    git switch "$TARGET"            # at HEAD, guaranteed by the preflight
  else
    git switch -c "$TARGET"         # from the current tip (the release commit)
  fi
  git push -u origin "refs/heads/$TARGET" "refs/tags/$TAG"
else
  # The launcher line has no stage branches: the bump commit and tag go to
  # main, and the operator stays where they were.
  git push origin main "refs/tags/$TAG"
fi

echo
echo "Pushed $TAG. CI is now building it -- watch:"
echo "  gh run list --limit 5"
