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

### 2. The notarytool credential profile

Notarization uploads to Apple and needs credentials. Two options; the API key
is preferred because it survives Apple ID password changes and is what CI uses.

**App Store Connect API key (recommended).** At
[appstoreconnect.apple.com/access/integrations/api](https://appstoreconnect.apple.com/access/integrations/api),
create a key with the **Developer** role — the least privilege that can
notarize. Download the `.p8`; **Apple allows exactly one download**, so store
it somewhere durable. Note the Key ID and Issuer ID, then:

```bash
xcrun notarytool store-credentials "bioflow" --key ~/AuthKey_XXXXXXXX.p8 --key-id YOUR_KEY_ID --issuer YOUR_ISSUER_ID
```

**App-specific password (simpler, more fragile).** Generate at
[account.apple.com](https://account.apple.com) → Sign-In and Security →
App-Specific Passwords:

```bash
xcrun notarytool store-credentials "bioflow" --apple-id you@example.com --team-id YOUR_TEAM_ID --password xxxx-xxxx-xxxx-xxxx
```

Verify either way:

```bash
xcrun notarytool history --keychain-profile "bioflow"
```

An empty history is success — it means the credentials authenticated. An error
means they did not.

## Building

From `launcher/`:

```bash
./build-macos.sh
```

The script resolves the signing identity from the keychain, checks the
notarytool profile *before* starting the build (a missing profile otherwise
surfaces only at the very end, after everything else has succeeded), runs
`tauri build`, and prints verification output.

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

Two repository variables (Settings → Secrets and variables → Actions →
Variables) configure it:

| Variable | Purpose | Default if unset |
| --- | --- | --- |
| `APPLE_TEAM_ID` | Team ID for notarization | none — set this |
| `APPLE_KEYCHAIN_PROFILE` | notarytool profile name on the runner | `bioflow` |

The runner's keychain must be **unlocked** for signing to work non-interactively.
A runner installed via `svc.sh` runs as a LaunchAgent at user login, so the
login keychain is normally unlocked already — but a machine that has been
locked or is sitting at the login window will fail signing with
`errSecInternalComponent`, which is an unhelpfully generic error for "the
keychain is locked."

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `errSecInternalComponent` at signing | Keychain locked, or private key missing for the certificate |
| App crashes immediately after a successful notarization | Missing JIT entitlements — see above |
| `spctl` says `rejected` on a fresh build | Notarization did not actually run; check the build log's final step |
| "damaged and can't be opened" for a downloaded copy | Signed but not notarized |
| Notarization hangs for many minutes | Normal — Apple's queue is often slow. `xcrun notarytool log <id> --keychain-profile bioflow` shows why a submission was rejected |
