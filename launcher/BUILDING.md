# Building the launcher

This covers building a distributable, signed, notarized macOS bundle and a
Linux bundle. For day-to-day development (`npm run tauri dev`, unsigned test
builds), see [`README.md`](README.md).

This file is the generic instructions — anyone, any machine. If you're
setting up *this* machine to sign macOS builds, also see `BUILDING.local.md`
(gitignored, not in this repo — it holds the actual Key/Issuer IDs and file
paths for one specific developer's setup once they've done the one-time
setup below).

## macOS: signed and notarized

### One-time setup

1. **A Developer ID Application certificate**, with its private key, in the
   signing machine's login keychain. "Apple Development" is not a
   substitute — it cannot sign for distribution outside the App Store.
   Create one at
   [developer.apple.com/account/resources/certificates](https://developer.apple.com/account/resources/certificates)
   if none exists. Confirm:

   ```bash
   security find-identity -v -p codesigning
   ```

2. **An App Store Connect API key.** Create one with the **Developer** role
   at
   [appstoreconnect.apple.com/access/integrations/api](https://appstoreconnect.apple.com/access/integrations/api).
   Download the `.p8` — Apple allows exactly one download — and note the Key
   ID (also the filename) and Issuer ID (a UUID shown above the key list,
   not in the file). Restrict its permissions:

   ```bash
   chmod 600 ~/path/to/AuthKey_XXXXXXXX.p8
   ```

3. **Accept any pending Apple Developer Program agreement**, if notarization
   returns `403: A required agreement is missing or has expired`. Only the
   account's **Account Holder** can do this, at
   [developer.apple.com/account](https://developer.apple.com/account) and/or
   [appstoreconnect.apple.com/business](https://appstoreconnect.apple.com/business).

4. **Export three environment variables** — `build-macos.sh` fails fast with
   a clear message if any is missing, rather than letting the build silently
   skip notarization:

   ```bash
   export APPLE_API_KEY=YOUR_KEY_ID
   export APPLE_API_ISSUER=YOUR_ISSUER_ID
   export APPLE_API_KEY_PATH=~/path/to/AuthKey_XXXXXXXX.p8
   ```

Full detail, including why these specific three variables and not the more
commonly documented `notarytool store-credentials` keychain profile, is in
[`../docs/macos-signing.md`](../docs/macos-signing.md).

### Build

```bash
cd launcher
./build-macos.sh              # sign + notarize
./build-macos.sh --no-notarize  # sign only, fast, local smoke test
```
> You may have to run `npm install` first if you haven't set up the launcher requirements previously.

This signs the `.app`, notarizes and staples it, builds the `.dmg`, and then
**separately notarizes and staples the `.dmg` itself** — Tauri's built-in
notarization step only covers the `.app`, and a `.dmg` without its own ticket
is Gatekeeper-blocked for anyone who downloads it even though the app inside
is genuinely notarized. Skipping this by calling `tauri build` directly
produces an undistributable `.dmg`. Details and the failure mode this was
caught by are in `docs/macos-signing.md`.

### Verify

```bash
spctl -a -vvv -t install src-tauri/target/release/bundle/dmg/*.dmg
```

Want: `source=Notarized Developer ID`. `accepted` alone is not sufficient —
a locally-built bundle passes that on its own machine regardless of whether
it would pass anywhere else.

To reproduce what an actual download looks like to Gatekeeper:

```bash
cp src-tauri/target/release/bundle/dmg/*.dmg /tmp/gatekeeper-test.dmg
xattr -w com.apple.quarantine "0081;00000000;Safari;" /tmp/gatekeeper-test.dmg
spctl -a -vvv -t open --context context:primary-signature /tmp/gatekeeper-test.dmg
```

### Known limitation: arm64-only

A build produces a bundle for the architecture of the machine that built it.
There is currently no universal (Intel + Apple Silicon) build being produced;
see `docs/macos-signing.md` for what a rustup-managed toolchain would take to
add one.

## Linux: unsigned

```bash
cd launcher
npm install
npm run tauri build
```

Produces `.deb` and `.rpm` under `src-tauri/target/release/bundle/`.
`src-tauri/tauri.linux.conf.json` restricts targets to those two — AppImage
bundling hangs on any machine with AppImageLauncher installed; see
[`README.md`](README.md) for why.

No signing story exists for Linux bundles today.

## CI

`.github/workflows/release-launcher.yml` builds both platforms on
self-hosted runners and attaches bundles to a release on a `launcher-v*` tag.
`workflow_dispatch` runs the build without releasing.

CI notarization needs three GitHub Actions **secrets**
(`APPLE_API_KEY_ID`, `APPLE_API_ISSUER_ID`, `APPLE_API_KEY_P8_BASE64`) and
one repository **variable** (`APPLE_TEAM_ID`) — see `docs/macos-signing.md`'s
"Notarization secrets" section for exactly what to enter and where each value
comes from.
