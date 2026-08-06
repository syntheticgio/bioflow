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
#   ./build-macos.sh              # sign + notarize (needs the keychain profile)
#   ./build-macos.sh --no-notarize  # sign only, for a fast local smoke test
#
# Prerequisites, both one-time:
#
#   1. A "Developer ID Application" certificate with its private key in the
#      login keychain. Check with:
#        security find-identity -v -p codesigning
#
#   2. A stored notarytool credential profile named by APPLE_KEYCHAIN_PROFILE:
#        xcrun notarytool store-credentials "bioflow" \
#          --key ~/AuthKey_XXXXXXXX.p8 --key-id KEY_ID --issuer ISSUER_ID
#
# See docs/macos-signing.md for the full walkthrough.
set -euo pipefail

cd "$(dirname "$0")"

NOTARIZE=1
if [ "${1:-}" = "--no-notarize" ]; then
  NOTARIZE=0
fi

: "${APPLE_TEAM_ID:=GMFYKVC5VL}"
: "${APPLE_KEYCHAIN_PROFILE:=bioflow}"

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
  # Fail here rather than forty minutes into a release build. `tauri build`
  # signs first and notarizes last, so a missing profile is not discovered
  # until everything else has already succeeded.
  if ! xcrun notarytool history --keychain-profile "$APPLE_KEYCHAIN_PROFILE" >/dev/null 2>&1; then
    cat >&2 <<EOF
error: no notarytool credential profile named "$APPLE_KEYCHAIN_PROFILE".

Create one (one time, interactive):

  xcrun notarytool store-credentials "$APPLE_KEYCHAIN_PROFILE" \\
    --key ~/AuthKey_XXXXXXXX.p8 --key-id YOUR_KEY_ID --issuer YOUR_ISSUER_ID

Or skip notarization for a local-only test build:

  ./build-macos.sh --no-notarize
EOF
    exit 1
  fi
  export APPLE_KEYCHAIN_PROFILE
  echo "Notarizing via keychain profile: $APPLE_KEYCHAIN_PROFILE"
else
  echo "Skipping notarization (--no-notarize). The result will be signed but"
  echo "Gatekeeper-blocked on any machine that downloads it."
fi

npm run tauri build

DMG="$(find src-tauri/target/release/bundle/dmg -name '*.dmg' -maxdepth 1 2>/dev/null | head -n1 || true)"
APP="$(find src-tauri/target/release/bundle/macos -name '*.app' -maxdepth 1 2>/dev/null | head -n1 || true)"

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
