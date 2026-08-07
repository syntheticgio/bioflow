#!/usr/bin/env bash
# Fail if a release tag disagrees with the version committed in the tree.
#
# ops/release.sh makes disagreement unreachable, so in normal operation this
# never fires. It exists for the tag typed by hand -- `git tag v0.9.0` against
# a tree that says 0.1.0 publishes images labelled 0.9.0 and a release page
# that lies, with nothing else in the pipeline noticing.
#
#   ops/check_tag_matches_version.sh v0.2.0
#   ops/check_tag_matches_version.sh launcher-v0.1.1

set -euo pipefail

die() { echo "::error::$*"; exit 1; }

[ $# -eq 1 ] || { echo "usage: $0 <tag>" >&2; exit 2; }
TAG="$1"

case "$TAG" in
  launcher-v*)
    EXPECTED="${TAG#launcher-v}"
    SOURCE="launcher/src-tauri/Cargo.toml"
    # First `version = "..."` only: [package] is the first table, so a
    # dependency's version further down must not be read instead.
    ACTUAL="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' "$SOURCE" | head -n1)"
    ;;
  v*)
    EXPECTED="${TAG#v}"
    SOURCE="VERSION"
    ACTUAL="$(tr -d '[:space:]' < "$SOURCE")"
    ;;
  *)
    die "tag '$TAG' has no known release prefix (expected v* or launcher-v*)"
    ;;
esac

[ -n "$ACTUAL" ] || die "could not read a version from $SOURCE"

if [ "$EXPECTED" != "$ACTUAL" ]; then
  die "tag $TAG expects version $EXPECTED but $SOURCE says $ACTUAL -- the tag was probably created by hand instead of by ops/release.sh"
fi

echo "$TAG matches $SOURCE ($ACTUAL)"
