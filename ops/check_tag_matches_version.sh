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

# The version the two launcher files must agree on, suffix-stripped: CR-4
# gives tauri.conf.json the core version while Cargo.toml keeps the full one.
core_of() {
  local v="$1"
  v="${v%-alpha}"
  printf '%s\n' "${v%-beta}"
}

read_toml_version() {
  # First `version = "..."` only: [package] is the first table, so a
  # dependency's version further down must not be read instead.
  sed -n 's/^version[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' "$1" | head -n1
}

read_json_version() {
  sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -n1
}

case "$TAG" in
  launcher-v*)
    EXPECTED="${TAG#launcher-v}"
    SOURCE="launcher/src-tauri/Cargo.toml"
    ACTUAL="$(read_toml_version "$SOURCE")"
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

# TG-5: the two launcher declarations must agree with each other, whichever
# tag prefix brought us here. Tauri reads tauri.conf.json in preference to
# Cargo.toml, so a stale one there publishes mislabelled bundles while every
# other check passes -- which is exactly what shipped as launcher-v0.2.0.
# Prefix-independent, so it is checked separately from the tag comparison
# above rather than folded into it.
CARGO="launcher/src-tauri/Cargo.toml"
TAURI_CONF="launcher/src-tauri/tauri.conf.json"
[ -f "$TAURI_CONF" ] || die "missing $TAURI_CONF"

CARGO_VERSION="$(read_toml_version "$CARGO")"
TAURI_VERSION="$(read_json_version "$TAURI_CONF")"
[ -n "$TAURI_VERSION" ] || die "could not read a version from $TAURI_CONF"

CARGO_CORE="$(core_of "$CARGO_VERSION")"
if [ "$CARGO_CORE" != "$TAURI_VERSION" ]; then
  die "$TAURI_CONF says $TAURI_VERSION but $CARGO says $CARGO_VERSION (core $CARGO_CORE) -- Tauri reads tauri.conf.json, so the bundles would be named $TAURI_VERSION"
fi

echo "$TAG matches $SOURCE ($ACTUAL); launcher files agree at $TAURI_VERSION"
