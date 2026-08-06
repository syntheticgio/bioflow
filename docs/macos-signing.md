# Signing and notarizing the macOS launcher

The launcher is distributed as a `.dmg` outside the Mac App Store. That path
requires two separate things, and conflating them is the most common way this
goes wrong:

| | What it is | What happens without it |
| --- | --- | --- |
| **Signing** | A Developer ID certificate stamps the bundle, proving who built it | "unidentified developer", right-click-to-open workaround |
| **Notarization** | Apple scans the signed bundle and issues a ticket | **Blocked outright.** macOS reports the app "is damaged and can't be opened" |

Both are needed. A signed-but-un-notarized build passes `codesign --verify`
cleanly and is still blocked on every machine that downloads it — which is why
the verification step below checks `spctl`, not `codesign`.

## One-time setup

### 1. The Developer ID Application certificate

Requires a paid Apple Developer Program membership. **"Apple Development" is
not a substitute** — it signs for local development only and cannot sign for
distribution.

Confirm what is installed:

```bash
security find-identity -v -p codesigning
```

You want a line reading `Developer ID Application: <name> (<TEAMID>)`. If it is
missing, create one at
[developer.apple.com/account/resources/certificates](https://developer.apple.com/account/resources/certificates)
(type: **Developer ID Application**) and double-click the download to install.

Seeing the certificate is not proof the *private key* is present — a cert
imported without its key looks identical in that listing and fails at signing
time. Prove it end to end:

```bash
T=$(mktemp -d) && echo x > "$T/f" && codesign -s "Developer ID Application" "$T/f" && echo "private key present"; rm -rf "$T"
```

**These expire yearly.** When it lapses, builds fail with `errSecInternalComponent`
or simply find no identity. Renewal is a new certificate, not an extension.

### 2. An App Store Connect API key

Notarization uploads to Apple and needs credentials. At
[appstoreconnect.apple.com/access/integrations/api](https://appstoreconnect.apple.com/access/integrations/api),
create a key with the **Developer** role — the least privilege that can
notarize. Download the `.p8`; **Apple allows exactly one download**, so store
it somewhere durable and restrict its permissions:

```bash
chmod 600 ~/path/to/AuthKey_XXXXXXXX.p8
```

Note the **Key ID** (also the filename) and the **Issuer ID** (a UUID shown
above the key list on that page, not in the downloaded file).

**Two different credential mechanisms exist here, and they are easy to
conflate — `build-macos.sh` uses only the second one:**

- `xcrun notarytool store-credentials` stores the key in the login keychain
  under a profile name, for manual `notarytool submit`/`history` calls. This
  is what most notarization guides show first, and it is useful for
  diagnostics (see Troubleshooting below), but Tauri's own build-time
  notarization step does **not** read a keychain profile.
- Tauri's `tauri build` notarizes via three environment variables read
  directly: `APPLE_API_KEY` (the Key ID), `APPLE_API_ISSUER`, and
  `APPLE_API_KEY_PATH` (path to the `.p8`). `build-macos.sh` requires these
  three and fails fast if any is missing, rather than letting Tauri silently
  skip notarization — see below.

Set up both, since the keychain profile is genuinely useful for
troubleshooting even though the build doesn't consume it:

```bash
xcrun notarytool store-credentials "bioflow" --key ~/AuthKey_XXXXXXXX.p8 --key-id YOUR_KEY_ID --issuer YOUR_ISSUER_ID
xcrun notarytool history --keychain-profile "bioflow"   # empty history = credentials authenticated
```

Then export the three variables `build-macos.sh` actually reads (e.g. in your
shell profile, or see `BUILDING.local.md` for this machine's values):

```bash
export APPLE_API_KEY=YOUR_KEY_ID
export APPLE_API_ISSUER=YOUR_ISSUER_ID
export APPLE_API_KEY_PATH=~/path/to/AuthKey_XXXXXXXX.p8
```

**The silent-skip trap:** if these three are unset, `tauri build` does not
error. It prints a `Warn skipping app notarization, no APPLE_ID & ...` line
that scrolls past in normal output, signs the bundle successfully, and
finishes looking green — with an unnotarized `.dmg` as the result.
`build-macos.sh` checks for all three up front specifically to turn that into
a hard failure before the build starts.

## Building

From `launcher/`:

```bash
./build-macos.sh
```

The script resolves the signing identity from the keychain, checks the three
`APPLE_API_*` variables *before* starting the build (missing ones otherwise
surface only at the very end, after everything else has succeeded), runs
`tauri build`, **separately notarizes and staples the `.dmg` itself**, and
prints verification output. That separate `.dmg` step exists because of a gap
in Tauri's own notarization (next section) — do not skip it by calling
`tauri build` directly for a distributable bundle.

For a fast local check that skips the slow Apple round-trip:

```bash
./build-macos.sh --no-notarize
```

That bundle runs on the machine that built it and is Gatekeeper-blocked
anywhere else. It is a smoke test, not something to hand to anyone.

## Verifying

The check that matters:

```bash
spctl -a -vvv -t install launcher/src-tauri/target/release/bundle/dmg/*.dmg
```

`source=Notarized Developer ID` is the pass condition. `accepted` alone is not
sufficient — a locally-built bundle is accepted on its own machine regardless.

To reproduce what a *downloading* user sees, set the quarantine attribute that
browsers apply, which is what triggers Gatekeeper at all:

```bash
cp launcher/src-tauri/target/release/bundle/dmg/*.dmg /tmp/gatekeeper-test.dmg
xattr -w com.apple.quarantine "0081;00000000;Safari;" /tmp/gatekeeper-test.dmg
open /tmp/gatekeeper-test.dmg
```

Testing without that attribute is the single most common false pass here: the
bundle opens fine on the build machine and is blocked for everyone else.

## Tauri notarizes the `.app`, not the `.dmg` — a gap `build-macos.sh` works around

Discovered on the first real end-to-end run (2026-08-06): `tauri build`'s
built-in notarization step notarizes and staples the `.app`, but the `.dmg`
is assembled *after* that, wrapping the already-notarized app, and Tauri does
not separately notarize or staple the `.dmg` container. The result:

```bash
spctl -a -vvv -t install "BioFlow Launcher.app"   # source=Notarized Developer ID
spctl -a -vvv -t install "BioFlow Launcher.dmg"   # source=Unnotarized Developer ID
```

— run right after the same build, on the same machine. The `.app` is
genuinely notarized; the `.dmg` sitting next to it is not, and a real user's
download of it is Gatekeeper-blocked, with the app inside being irrelevant
because macOS never gets that far. `codesign --verify` and opening the
locally-built `.dmg` directly both look fine, because neither exercises
Gatekeeper's online check the way a quarantined download does — this is
exactly the false-pass mode the quarantine-attribute test above exists to
catch, and it is what caught this.

The fix, which `build-macos.sh` now does automatically after `tauri build`
completes: notarize and staple the `.dmg` as its own, second submission:

```bash
xcrun notarytool submit "$DMG" --key "$APPLE_API_KEY_PATH" --key-id "$APPLE_API_KEY" --issuer "$APPLE_API_ISSUER" --wait
xcrun stapler staple "$DMG"
```

If this script is ever bypassed in favor of calling `tauri build` directly,
this step has to be reproduced by hand — the `.dmg` it produces is not
distributable without it, regardless of how successful the build output
looks.

## Hardened runtime and entitlements

Notarization requires the hardened runtime, which denies several things this
app needs. `launcher/src-tauri/entitlements.plist` grants exactly three:

- `allow-jit` and `allow-unsigned-executable-memory` — the window is a
  WKWebView and JavaScriptCore JITs. Without them the app signs, notarizes,
  passes Gatekeeper, and then **crashes as soon as the webview loads**. The
  symptom points at the signature; the cause is the entitlement.
- `disable-library-validation` — the webview loads system frameworks not
  signed with this team ID.

There is deliberately no App Sandbox entitlement. The launcher shells out to
`docker` and `open -a Docker` (`launcher/src-tauri/src/docker/shell.rs`) and
writes an `.env` to a user-chosen directory; the sandbox forbids both.
Developer ID distribution does not require it — only the App Store does.

**The entitlements file must contain no XML comments.** `codesign` parses it
with AMFI's stricter parser, which rejects `<!-- -->` anywhere in the file —
including in the positions where a normal plist tolerates them:

```
Failed to parse entitlements: AMFIUnserializeXML: syntax error near line 5
```

The reported line number is the comment's location, which makes this look
like a malformed-XML problem. It is not. `plutil -lint` reports the file as
`OK` either way, so it does **not** catch this — the only reliable check is
running a build. That is why the rationale for each key lives in this document
rather than inline in the file where it would be more discoverable.

## Architecture: arm64 only, today

Builds produce a bundle for the architecture of the machine that built it. The
CI macOS job runs on an Apple Silicon runner, so releases are **arm64-only and
will not run on an Intel Mac**.

Making a universal bundle needs the `x86_64-apple-darwin` target and
`tauri build --target universal-apple-darwin`. That requires a rustup-managed
toolchain — a Homebrew-installed `rustc` (which is what this project's dev
machine has) cannot add targets. If Intel support is wanted, that toolchain
swap is the prerequisite, and it is not currently tracked by an issue.

## CI

`.github/workflows/release-launcher.yml` builds both platforms and attaches
the bundles to a release on a `launcher-v*` tag.

The macOS job runs on the **self-hosted Apple Silicon runner**, which is also
the machine holding the certificate (see [`ci-runners.md`](ci-runners.md) for
why this repo has no GitHub-hosted jobs). That is why the workflow does *not*
export a base64 `.p12` into a GitHub secret the way most macOS signing
pipelines do — the keychain is already there. The tradeoff is that the signing
key lives on a dev machine rather than in GitHub's secret store, which for a
single-maintainer project is the simpler and less leaky of the two.

`APPLE_TEAM_ID` (Settings → Secrets and variables → Actions → Variables)
configures the team ID. The three `APPLE_API_*` values that
`build-macos.sh` requires for notarization are not yet wired into the
workflow — see "Still open" below.

The runner's keychain must be **unlocked** for signing to work non-interactively.
A runner installed via `svc.sh` runs as a LaunchAgent at user login, so the
login keychain is normally unlocked already — but a machine that has been
locked or is sitting at the login window will fail signing with
`errSecInternalComponent`, which is an unhelpfully generic error for "the
keychain is locked."

### Still open: getting `APPLE_API_KEY_PATH` onto the runner

The `.p8` file needs to exist on the self-hosted runner's filesystem (or be
written there by the workflow from a secret) for CI to notarize. Options, not
yet decided:

- Leave the `.p8` in place on the runner's disk (it already lives there for
  manual builds) and set `APPLE_API_KEY_PATH` as a repository variable
  pointing at it. Simplest, but ties the workflow to a path that exists only
  on this one machine.
- Store the `.p8` contents as a GitHub Actions **secret** (base64-encoded)
  and have the workflow write it to a temp file at job start. More portable,
  standard practice, but the secret must be marked `sensitive`-equivalent and
  the temp file cleaned up after the job.

Either way, `APPLE_API_KEY` and `APPLE_API_ISSUER` are not secret-sensitive
in the same way (they don't grant anything without the `.p8`) and can be
plain repository variables.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `errSecInternalComponent` at signing | Keychain locked, or private key missing for the certificate |
| App crashes immediately after a successful notarization | Missing JIT entitlements — see above |
| Build log says `skipping app notarization, no APPLE_ID & ...` | `APPLE_API_KEY` / `APPLE_API_ISSUER` / `APPLE_API_KEY_PATH` not all set — this is a silent skip, not a build failure, so the build otherwise looks successful |
| `.app` shows `Notarized`, `.dmg` right next to it shows `Unnotarized` | The Tauri gap described above — the `.dmg` needs its own notarize+staple pass, which `build-macos.sh` now does automatically |
| `spctl` says `rejected` on a fresh build | Notarization did not actually run; check the build log's final step |
| "damaged and can't be opened" for a downloaded copy | Signed but not notarized |
| Notarization hangs for many minutes | Normal — Apple's queue is often slow. `xcrun notarytool log <id> --keychain-profile bioflow` shows why a submission was rejected (this uses the keychain profile from setup step 2, for diagnostics only) |
| `HTTP status code: 403. A required agreement is missing or has expired` | An Apple Developer Program License Agreement update needs accepting by the **Account Holder** at [developer.apple.com/account](https://developer.apple.com/account) (check the banner) and/or [App Store Connect → Business](https://appstoreconnect.apple.com/business). Not fixable by re-running the command. |
