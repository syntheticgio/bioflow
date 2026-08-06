#!/usr/bin/env bash
# Build, sign, and notarize the macOS launcher bundle.
#
# Two steps that are easy to conflate: *signing* proves who built the app,
# *notarization* is Apple scanning it and issuing a ticket. Gatekeeper blocks a
# signed-but-not-notarized download just as firmly as an unsigned one, so both
# have to happen. Tauri does both in one `tauri build` when it sees the
# environment below.
#
# Usage, from launcher/:
#
#   ./build-macos.sh              # sign + notarize
#   ./build-macos.sh --no-notarize  # sign only, for a fast local smoke test
#
# Prerequisites, both one-time:
#
#   1. A "Developer ID Application" certificate with its private key in a
#      keychain on the default search list. On the CI runner this is a
#      dedicated ci-signing.keychain-db, not the login keychain -- codesign
#      invoked from the runner's LaunchAgent context fails with
#      errSecInternalComponent against the login keychain even with correct
#      ACLs and an unlocked, active GUI session (see docs/macos-signing.md).
#      Check with:
#        security find-identity -v -p codesigning
#
#   2. An App Store Connect API key (.p8) plus its Key ID and Issuer ID.
#      `xcrun notarytool store-credentials` is NOT what this script reads --
#      that stores a *keychain profile* for manual `notarytool submit` calls,
#      but Tauri's own notarization step (invoked inside `tauri build`, not
#      by us calling notarytool directly) only recognizes the API key given
#      directly via APPLE_API_KEY / APPLE_API_ISSUER / APPLE_API_KEY_PATH (or
#      the older APPLE_ID / APPLE_PASSWORD / APPLE_TEAM_ID trio). A stored
#      keychain profile with none of those set produces:
#        "skipping app notarization, no APPLE_ID & APPLE_PASSWORD &
#         APPLE_TEAM_ID or APPLE_API_KEY & APPLE_API_ISSUER &
#         APPLE_API_KEY_PATH environment variables found"
#      which reads as an error and is actually a silent skip -- the build
#      finishes, signs, and produces an unnotarized .dmg with no failure.
#      Set the three variables below (once, e.g. in your shell profile):
#        export APPLE_API_KEY=YOUR_KEY_ID
#        export APPLE_API_ISSUER=YOUR_ISSUER_ID
#        export APPLE_API_KEY_PATH=~/path/to/AuthKey_XXXXXXXX.p8
#
# See docs/macos-signing.md for the full walkthrough.
set -euo pipefail

cd "$(dirname "$0")"

NOTARIZE=1
if [ "${1:-}" = "--no-notarize" ]; then
  NOTARIZE=0
fi

: "${APPLE_TEAM_ID:=GMFYKVC5VL}"

# Unlock the dedicated CI signing keychain if one is configured. A freshly
# created keychain is locked at the start of every session; codesign against
# a locked keychain fails the same way as one that isn't on the search list
# at all. CI_KEYCHAIN_PATH / CI_KEYCHAIN_PASSWORD are only set on the runner
# (see .github/workflows/release-launcher.yml) -- a local dev build with no
# CI keychain configured just falls through to whatever's already unlocked.
if [ -n "${CI_KEYCHAIN_PATH:-}" ]; then
  if [ -z "${CI_KEYCHAIN_PASSWORD:-}" ]; then
    echo "error: CI_KEYCHAIN_PATH is set but CI_KEYCHAIN_PASSWORD is not." >&2
    exit 1
  fi
  echo "Unlocking CI signing keychain: $CI_KEYCHAIN_PATH"
  security unlock-keychain -p "$CI_KEYCHAIN_PASSWORD" "$CI_KEYCHAIN_PATH"
  security set-keychain-settings -lut 21600 "$CI_KEYCHAIN_PATH"
fi

# Resolve the signing identity from the keychain rather than hardcoding the
# name, so a renewed certificate (they expire yearly) doesn't silently stop
# matching. Tauri matches this string against the keychain itself.
if [ -z "${APPLE_SIGNING_IDENTITY:-}" ]; then
  APPLE_SIGNING_IDENTITY="$(
    security find-identity -v -p codesigning \
      | grep "Developer ID Application" \
      | head -n1 \
      | sed -E 's/.*"(.*)"$/\1/'
  )"
fi

if [ -z "$APPLE_SIGNING_IDENTITY" ]; then
  cat >&2 <<'EOF'
error: no "Developer ID Application" identity found in the keychain.

An Apple Development certificate is not a substitute -- it cannot sign for
distribution outside the App Store. Create a Developer ID Application
certificate at https://developer.apple.com/account/resources/certificates
and install it, then re-run.
EOF
  exit 1
fi

echo "Signing identity: $APPLE_SIGNING_IDENTITY"
echo "Team ID:          $APPLE_TEAM_ID"

export APPLE_SIGNING_IDENTITY APPLE_TEAM_ID

if [ "$NOTARIZE" -eq 1 ]; then
  # Fail here rather than forty minutes into a release build. Without these,
  # `tauri build` does not error -- it silently skips the notarization step
  # and still produces a signed, unnotarized .dmg, which then fails much
  # later and less clearly at the spctl check below (or, worse, in a user's
  # hands if that check is ever skipped).
  if [ -z "${APPLE_API_KEY:-}" ] || [ -z "${APPLE_API_ISSUER:-}" ] || [ -z "${APPLE_API_KEY_PATH:-}" ]; then
    cat >&2 <<'EOF'
error: APPLE_API_KEY, APPLE_API_ISSUER, and APPLE_API_KEY_PATH must all be set
for Tauri to notarize the build.

  export APPLE_API_KEY=YOUR_KEY_ID
  export APPLE_API_ISSUER=YOUR_ISSUER_ID
  export APPLE_API_KEY_PATH=~/path/to/AuthKey_XXXXXXXX.p8

These are the same Key ID / Issuer ID / .p8 from creating an App Store
Connect API key -- see docs/macos-signing.md. Note this is NOT the same
credential store as `xcrun notarytool store-credentials`; that stores a
keychain profile for manual notarytool calls, which Tauri's own build-time
notarization step does not read.

Or skip notarization for a local-only test build:

  ./build-macos.sh --no-notarize
EOF
    exit 1
  fi
  if [ ! -f "$APPLE_API_KEY_PATH" ]; then
    echo "error: APPLE_API_KEY_PATH ($APPLE_API_KEY_PATH) does not exist." >&2
    exit 1
  fi
  export APPLE_API_KEY APPLE_API_ISSUER APPLE_API_KEY_PATH
  echo "Notarizing with API key: $APPLE_API_KEY"
else
  echo "Skipping notarization (--no-notarize). The result will be signed but"
  echo "Gatekeeper-blocked on any machine that downloads it."
fi

npm run tauri build

DMG="$(find src-tauri/target/release/bundle/dmg -name '*.dmg' -maxdepth 1 2>/dev/null | head -n1 || true)"
APP="$(find src-tauri/target/release/bundle/macos -name '*.app' -maxdepth 1 2>/dev/null | head -n1 || true)"

# Tauri's built-in notarization step (above, inside `tauri build`) notarizes
# and staples the .app -- but the .dmg is built *after* that, wrapping the
# already-notarized app, and Tauri does not separately notarize or staple the
# .dmg container itself. The result: `spctl` on the .app says "Notarized
# Developer ID" while `spctl` on the .dmg sitting right next to it says
# "Unnotarized Developer ID", and a real downloaded copy of that .dmg is
# blocked by Gatekeeper even though the app inside it is genuinely notarized.
# This was caught by testing with the quarantine attribute set (see
# docs/macos-signing.md) -- `codesign --verify` and an un-quarantined `open`
# both look fine and do not catch it.
#
# The fix is to notarize and staple the .dmg itself as a second, separate
# submission after the build.
if [ "$NOTARIZE" -eq 1 ] && [ -n "$DMG" ]; then
  echo
  echo "=== Notarizing the .dmg container ==="
  xcrun notarytool submit "$DMG" \
    --key "$APPLE_API_KEY_PATH" --key-id "$APPLE_API_KEY" --issuer "$APPLE_API_ISSUER" \
    --wait
  xcrun stapler staple "$DMG"
fi

echo
echo "=== Verification ==="

if [ -n "$APP" ]; then
  echo "--- codesign, $APP"
  codesign --verify --deep --strict --verbose=2 "$APP" 2>&1 || true
fi

if [ -n "$DMG" ]; then
  echo "--- spctl, $DMG"
  # This is the check that matters. `codesign --verify` passes on a signed but
  # un-notarized bundle; only spctl reports the notarization ticket. Look for
  # `source=Notarized Developer ID` -- anything else means a download of this
  # file will be blocked, however green the rest of the build looked.
  spctl -a -vvv -t install "$DMG" 2>&1 || true
  echo
  echo "Built: $DMG"
fi
