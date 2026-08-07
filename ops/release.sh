#!/usr/bin/env bash
# Cut a release: bump the version declarations, commit, tag, push.
#
# Bumping and tagging are one command on purpose. Done as two manual steps
# the failure mode is "bumped but never tagged" or "tagged but never bumped",
# and recovering from the second means deleting a pushed tag that CI has
# already acted on. One command makes both states unreachable.
#
#   ops/release.sh app 0.2.0        -> tag v0.2.0
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

# --- preflight -------------------------------------------------------------
# Every check refuses rather than warns, and names the precondition it tripped.

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || die "'$VERSION' is not semver MAJOR.MINOR.PATCH (no 'v', no -rc suffix)"

[ -z "$(git status --porcelain)" ] \
  || die "working tree is not clean -- commit or stash first"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] \
  || die "releases are cut from main, not '$BRANCH'"

git rev-parse -q --verify "refs/tags/$TAG" >/dev/null \
  && die "tag $TAG already exists locally"
if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
  die "tag $TAG already exists on origin"
fi

# The current version, read from the line's own source of truth.
if [ "$LINE" = "app" ]; then
  CURRENT="$(tr -d '[:space:]' < VERSION)"
else
  CURRENT="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' \
    launcher/src-tauri/Cargo.toml | head -n1)"
fi
[ -n "$CURRENT" ] || die "could not read the current $LINE version"

# sort -V puts the greater version last; equal versions are caught first.
[ "$VERSION" != "$CURRENT" ] || die "$VERSION is already the current version"
GREATER="$(printf '%s\n%s\n' "$CURRENT" "$VERSION" | sort -V | tail -n1)"
[ "$GREATER" = "$VERSION" ] \
  || die "$VERSION is not greater than the current version $CURRENT"

# --- bump, commit, tag, push ----------------------------------------------

echo "Releasing $LINE $CURRENT -> $VERSION (tag $TAG)"

WRITTEN=()
while IFS= read -r line; do
  WRITTEN+=("$line")
done < <(python3 "$SCRIPT_DIR/lib/bump_version.py" "$LINE" "$VERSION" --root "$REPO_ROOT")
[ "${#WRITTEN[@]}" -gt 0 ] || die "bump wrote no files"

git add -- "${WRITTEN[@]}"
git commit -m "release: $TAG"
git tag -a "$TAG" -m "$TAG"

# Commit and tag together: a pushed tag whose commit never landed is a tag CI
# cannot check out.
git push origin main "refs/tags/$TAG"

echo
echo "Pushed $TAG. CI is now building it -- watch:"
echo "  gh run list --limit 5"
