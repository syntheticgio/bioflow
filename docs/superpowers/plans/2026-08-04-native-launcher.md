# Native Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a small cross-platform desktop application that detects Docker, collects three settings, writes an `.env` beside a bundled `docker-compose.yml`, and runs, stops, and reports on the BioFlow stack — so a user never types `docker compose`.

**Architecture:** A Tauri v2 app in `launcher/` at the repository root. Rust owns everything that touches the operating system: probing the Docker daemon, spawning `docker compose`, reading and writing `.env`, and polling the API healthcheck. The React UI owns only presentation and a state machine that is a pure function of a status snapshot, so it is unit-testable with no Docker present. The two halves talk over Tauri commands (UI → Rust, request/response) and Tauri events (Rust → UI, streamed compose output).

**Tech Stack:** Rust (Tauri v2, `tauri-plugin-shell`, `tauri-plugin-dialog`, `tauri-plugin-opener`, `serde`), React 18 + TypeScript + Vite (matching `frontend/`), `vitest` for tests.

**Spec:** `docs/superpowers/specs/2026-08-04-native-launcher-contract-design.md`

---

## Before you start

**Prerequisites on the build machine:** Rust (`cargo --version` — verified at 1.96.0), Node 22+ (`node --version` — verified at v22.22.3), and the Tauri system dependencies for your platform (macOS: Xcode command line tools; Linux: `libwebkit2gtk-4.1-dev`, `build-essential`, `curl`, `wget`, `file`, `libxdo-dev`, `libssl-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev`; Windows: WebView2, present on Windows 11).

**A note on what you can and cannot verify.** Tasks 1–9 are testable on any machine. Task 10 (real `docker compose` execution) requires Docker running locally, and the end-to-end install flow cannot fully succeed until issue #37 publishes the container images — before that, `docker compose up` will fail on missing build contexts. That is expected. Do not attempt to work around it by adding `build:` directives to the launcher's bundled compose file; the failure is the correct behavior until #37 lands.

**Do not run `docker compose` from this worktree.** A `PreToolUse` hook blocks it, for the reasons in `CLAUDE.md`. The launcher writes its own compose project into a temporary install directory with its own project name, which is why its commands pass `--project-directory` explicitly.

---

## File Structure

**Rust (`launcher/src-tauri/src/`)** — one module per responsibility, each independently testable:

| File | Responsibility |
|---|---|
| `main.rs` | Tauri builder, plugin registration, command handler registration |
| `docker.rs` | Locating the `docker` binary, probing the daemon, distinguishing "absent" from "stopped", platform-specific start commands |
| `compose.rs` | Building and spawning `docker compose` invocations; streaming stdout/stderr as events |
| `install.rs` | Reading and writing the install directory: `.env` serialization, compose asset extraction, validation of paths and ports |
| `health.rs` | Polling the API `/healthz` endpoint |
| `status.rs` | Assembling the `StatusSnapshot` the UI consumes |
| `update.rs` | Registry manifest digest check |

**TypeScript (`launcher/src/`)**:

| File | Responsibility |
|---|---|
| `state.ts` | Pure `deriveState(snapshot)` state machine — no I/O, fully unit-tested |
| `state.test.ts` | State machine tests |
| `validate.ts` | Pure client-side validation helpers (port range, path shape) |
| `validate.test.ts` | Validation tests |
| `api.ts` | Thin typed wrappers over `invoke()` — the only file importing from `@tauri-apps/api` |
| `App.tsx` | Screen routing based on derived state |
| `screens/*.tsx` | One component per screen: `DockerMissing`, `Setup`, `Running`, `Stopped`, `Settings`, `Waiting` |

**Assets:** `launcher/src-tauri/assets/docker-compose.yml` — copied from the repository root at build time (Task 3).

---

## Task 1: Scaffold the Tauri application

**Files:**
- Create: `launcher/` (entire scaffold)
- Modify: `.gitignore`

- [ ] **Step 1: Scaffold the project**

From the repository root:

```bash
npm create tauri-app@latest launcher -- --template react-ts --manager npm --identifier app.bioflow.launcher
```

If the interactive prompt appears anyway, answer: project name `launcher`, identifier `app.bioflow.launcher`, language TypeScript, package manager npm, UI template React, flavor TypeScript.

- [ ] **Step 2: Verify it builds and runs**

```bash
cd launcher && npm install && npm run tauri build -- --debug
```

Expected: compiles, and a debug binary appears under `launcher/src-tauri/target/debug/`. The first build downloads and compiles the full Rust dependency tree and takes several minutes — this is normal and only happens once.

- [ ] **Step 3: Ignore build artifacts**

Append to the repository root `.gitignore`:

```gitignore
# Tauri launcher build output
launcher/src-tauri/target/
launcher/node_modules/
launcher/dist/
```

- [ ] **Step 4: Configure the window**

Replace the `app.windows` array in `launcher/src-tauri/tauri.conf.json` with a single fixed-size window. The launcher is a small control panel, not a resizable document window:

```json
"windows": [
  {
    "label": "main",
    "title": "BioFlow Launcher",
    "width": 620,
    "height": 560,
    "resizable": false,
    "center": true
  }
]
```

Also set `"productName": "BioFlow Launcher"` at the top level of that file.

- [ ] **Step 5: Commit**

```bash
git add launcher .gitignore
git commit -m "feat(launcher): scaffold Tauri application"
```

---

## Task 2: Add the Rust plugins and dependencies

**Files:**
- Modify: `launcher/src-tauri/Cargo.toml`
- Modify: `launcher/src-tauri/src/lib.rs`
- Modify: `launcher/src-tauri/capabilities/default.json`
- Modify: `launcher/package.json`

- [ ] **Step 1: Add Rust dependencies**

In `launcher/src-tauri/Cargo.toml`, add to `[dependencies]`:

```toml
tauri-plugin-shell = "2"
tauri-plugin-dialog = "2"
tauri-plugin-opener = "2"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tokio = { version = "1", features = ["time", "process", "io-util"] }
reqwest = { version = "0.12", default-features = false, features = ["rustls-tls", "json"] }
```

`reqwest` uses `rustls-tls` rather than the default `native-tls` so the build does not require OpenSSL development headers on Linux.

- [ ] **Step 2: Register the plugins**

In `launcher/src-tauri/src/lib.rs`, add each plugin to the builder chain:

```rust
tauri::Builder::default()
    .plugin(tauri_plugin_shell::init())
    .plugin(tauri_plugin_dialog::init())
    .plugin(tauri_plugin_opener::init())
```

- [ ] **Step 3: Add the frontend plugin packages**

```bash
cd launcher && npm install @tauri-apps/plugin-shell @tauri-apps/plugin-dialog @tauri-apps/plugin-opener
```

- [ ] **Step 4: Grant capabilities**

In `launcher/src-tauri/capabilities/default.json`, ensure the `permissions` array contains:

```json
"permissions": [
  "core:default",
  "shell:allow-execute",
  "dialog:allow-open",
  "opener:allow-open-url"
]
```

- [ ] **Step 5: Verify it still builds**

```bash
cd launcher && npm run tauri build -- --debug
```

Expected: compiles with no errors.

- [ ] **Step 6: Commit**

```bash
git add launcher
git commit -m "feat(launcher): add shell, dialog, and opener plugins"
```

---

## Task 3: Bundle the compose file as a build asset

The launcher ships `docker-compose.yml` verbatim. It must be copied from the repository root at build time so it cannot drift from the real stack definition.

**Files:**
- Create: `launcher/src-tauri/build.rs` (modify if the scaffold created one)
- Create: `launcher/src-tauri/assets/.gitkeep`
- Modify: `.gitignore`

- [ ] **Step 1: Write the build script**

Replace the contents of `launcher/src-tauri/build.rs`:

```rust
use std::path::PathBuf;

fn main() {
    // The launcher ships the repository's compose file verbatim. Copying it at
    // build time rather than checking in a duplicate is what makes "the
    // launcher never drifts from the stack" true by construction instead of by
    // discipline. See docs/superpowers/specs/2026-08-04-native-launcher-contract-design.md
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let repo_root = manifest.join("..").join("..");
    let source = repo_root.join("docker-compose.yml");
    let assets = manifest.join("assets");
    std::fs::create_dir_all(&assets).expect("create assets dir");
    std::fs::copy(&source, assets.join("docker-compose.yml"))
        .unwrap_or_else(|e| panic!("copy {}: {e}", source.display()));

    println!("cargo:rerun-if-changed={}", source.display());

    tauri_build::build()
}
```

- [ ] **Step 2: Keep the assets directory but ignore its contents**

```bash
touch launcher/src-tauri/assets/.gitkeep
```

Append to the root `.gitignore`:

```gitignore
# Copied from the repo root at build time by launcher/src-tauri/build.rs
launcher/src-tauri/assets/docker-compose.yml
```

- [ ] **Step 3: Verify the copy happens**

```bash
cd launcher && cargo build --manifest-path src-tauri/Cargo.toml 2>&1 | tail -5 && ls -l src-tauri/assets/
```

Expected: `docker-compose.yml` is present in `assets/`.

- [ ] **Step 4: Embed it in the binary**

In `launcher/src-tauri/src/install.rs` (created in Task 6), the file is embedded with `include_str!`. For now, confirm the path resolves by adding this to `lib.rs` temporarily and building:

```rust
pub const BUNDLED_COMPOSE: &str = include_str!("../assets/docker-compose.yml");
```

```bash
cd launcher && cargo build --manifest-path src-tauri/Cargo.toml 2>&1 | tail -3
```

Expected: compiles. Leave the constant in place — Task 6 uses it.

- [ ] **Step 5: Commit**

```bash
git add launcher .gitignore
git commit -m "feat(launcher): bundle repo compose file as a build asset"
```

---

## Task 4: Docker detection

**Files:**
- Create: `launcher/src-tauri/src/docker.rs`
- Modify: `launcher/src-tauri/src/lib.rs`

- [ ] **Step 1: Write the failing test**

Create `launcher/src-tauri/src/docker.rs` with the types and a test module:

```rust
use serde::Serialize;

/// What a daemon probe found. The three cases need different UI, which is why
/// this is not a bool: "not installed" sends the user to a download page,
/// "installed but stopped" is something the launcher can fix itself.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum DockerState {
    Ready,
    NotRunning,
    NotInstalled,
}

/// Classify the outcome of `docker info`. Separated from the process spawn so
/// the mapping is testable without Docker present.
pub fn classify(binary_found: bool, exit_ok: bool, stderr: &str) -> DockerState {
    if !binary_found {
        return DockerState::NotInstalled;
    }
    if exit_ok {
        return DockerState::Ready;
    }
    // `docker info` against a stopped daemon reports a connection failure. The
    // wording differs across platforms and Docker versions, so match on the
    // stable substrings rather than the whole message.
    let lowered = stderr.to_lowercase();
    if lowered.contains("cannot connect")
        || lowered.contains("is the docker daemon running")
        || lowered.contains("connection refused")
        || lowered.contains("//./pipe/docker_engine")
    {
        DockerState::NotRunning
    } else {
        DockerState::NotRunning
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_binary_is_not_installed() {
        assert_eq!(classify(false, false, ""), DockerState::NotInstalled);
    }

    #[test]
    fn successful_info_is_ready() {
        assert_eq!(classify(true, true, ""), DockerState::Ready);
    }

    #[test]
    fn connection_refused_is_not_running() {
        assert_eq!(
            classify(true, false, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock."),
            DockerState::NotRunning
        );
    }

    #[test]
    fn windows_pipe_error_is_not_running() {
        assert_eq!(
            classify(true, false, "open //./pipe/docker_engine: The system cannot find the file specified."),
            DockerState::NotRunning
        );
    }
}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml docker:: 2>&1 | tail -20
```

Expected: FAIL — `docker` module is not declared in `lib.rs` yet, so compilation errors.

- [ ] **Step 3: Declare the module**

Add to the top of `launcher/src-tauri/src/lib.rs`:

```rust
pub mod docker;
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml docker:: 2>&1 | tail -20
```

Expected: `test result: ok. 4 passed`.

- [ ] **Step 5: Add the probe and the platform start command**

Append to `docker.rs`:

```rust
use std::process::Command;

/// Run `docker info` and classify the result.
pub fn probe() -> DockerState {
    match Command::new("docker").arg("info").output() {
        Ok(out) => classify(
            true,
            out.status.success(),
            &String::from_utf8_lossy(&out.stderr),
        ),
        // ErrorKind::NotFound means no `docker` on PATH; anything else (a
        // permissions problem, for instance) still means we could not run it.
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => DockerState::NotInstalled,
        Err(_) => DockerState::NotInstalled,
    }
}

/// The platform's way of starting an installed-but-stopped Docker daemon.
/// Returns None where no reliable non-interactive command exists.
pub fn start_command() -> Option<(&'static str, Vec<&'static str>)> {
    #[cfg(target_os = "macos")]
    {
        Some(("open", vec!["-a", "Docker"]))
    }
    #[cfg(target_os = "windows")]
    {
        Some((
            "cmd",
            vec!["/C", "start", "", "C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe"],
        ))
    }
    #[cfg(target_os = "linux")]
    {
        // User-scoped first: a rootless or Desktop install needs no sudo. A
        // system daemon requires privileges the launcher deliberately does not
        // ask for, so that case falls through to the manual screen.
        Some(("systemctl", vec!["--user", "start", "docker-desktop"]))
    }
}

/// Attempt to start Docker. Returns false when no start command exists or the
/// spawn itself failed; the caller falls back to the manual screen.
pub fn try_start() -> bool {
    match start_command() {
        Some((bin, args)) => Command::new(bin).args(args).status().is_ok(),
        None => false,
    }
}
```

- [ ] **Step 6: Verify the probe compiles and runs against the real daemon**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml docker:: 2>&1 | tail -5
```

Expected: still 4 passed. (`probe()` itself is verified manually in Task 10 — it reads real machine state and is deliberately not unit-tested.)

- [ ] **Step 7: Commit**

```bash
git add launcher/src-tauri/src
git commit -m "feat(launcher): detect Docker daemon state"
```

---

## Task 5: Install-directory validation

**Files:**
- Create: `launcher/src-tauri/src/install.rs`
- Modify: `launcher/src-tauri/src/lib.rs`

- [ ] **Step 1: Write the failing tests**

Create `launcher/src-tauri/src/install.rs`:

```rust
use serde::{Deserialize, Serialize};
use std::path::Path;

/// The three answers first-run setup collects, plus the two toggles the
/// settings screen can change. This is exactly what gets written to .env --
/// the launcher owns no other configuration.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Settings {
    pub storage_path: String,
    pub install_dir: String,
    pub port: u16,
    /// false = 127.0.0.1 (default), true = 0.0.0.0
    pub allow_network_access: bool,
    pub tag: String,
}

impl Settings {
    pub fn bind_address(&self) -> &'static str {
        if self.allow_network_access { "0.0.0.0" } else { "127.0.0.1" }
    }
}

/// Serialize settings to .env contents. The launcher writes only variables it
/// owns; it never edits compose YAML.
pub fn render_env(s: &Settings) -> String {
    format!(
        "# Written by BioFlow Launcher. Edit through the launcher's Settings screen.\n\
         BIOINFO_HOME={}\n\
         WEB_PORT={}\n\
         BIND_ADDRESS={}\n\
         BIOFLOW_TAG={}\n",
        s.storage_path,
        s.port,
        s.bind_address(),
        s.tag,
    )
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum PathVerdict {
    Ok,
    /// macOS only: outside the directories Docker Desktop shares by default.
    /// The stack starts fine and /data is empty, which is why this is caught
    /// up front rather than left to surface later.
    NotShareable,
    Missing,
    NotWritable,
}

/// macOS Docker Desktop shares $HOME, /Volumes, /private, and /tmp by default.
/// A path outside those needs a manual File Sharing entry.
pub fn macos_shareable(path: &str, home: &str) -> bool {
    let p = Path::new(path);
    p.starts_with(home)
        || p.starts_with("/Volumes")
        || p.starts_with("/private")
        || p.starts_with("/tmp")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings() -> Settings {
        Settings {
            storage_path: "/Users/me/BioFlow".into(),
            install_dir: "/Users/me/.bioflow".into(),
            port: 5173,
            allow_network_access: false,
            tag: "latest".into(),
        }
    }

    #[test]
    fn loopback_is_the_default_bind() {
        assert_eq!(settings().bind_address(), "127.0.0.1");
    }

    #[test]
    fn network_access_opens_the_bind() {
        let mut s = settings();
        s.allow_network_access = true;
        assert_eq!(s.bind_address(), "0.0.0.0");
    }

    #[test]
    fn env_contains_every_owned_variable() {
        let out = render_env(&settings());
        assert!(out.contains("BIOINFO_HOME=/Users/me/BioFlow"));
        assert!(out.contains("WEB_PORT=5173"));
        assert!(out.contains("BIND_ADDRESS=127.0.0.1"));
        assert!(out.contains("BIOFLOW_TAG=latest"));
    }

    #[test]
    fn env_does_not_write_variables_the_launcher_does_not_own() {
        let out = render_env(&settings());
        assert!(!out.contains("MONGO_URL"));
        assert!(!out.contains("WORKER_REPLICAS"));
    }

    #[test]
    fn home_paths_are_shareable() {
        assert!(macos_shareable("/Users/me/BioFlow", "/Users/me"));
        assert!(macos_shareable("/Volumes/External/BioFlow", "/Users/me"));
    }

    #[test]
    fn root_level_paths_are_not_shareable() {
        assert!(!macos_shareable("/data/bioflow", "/Users/me"));
        assert!(!macos_shareable("/opt/bioflow", "/Users/me"));
    }
}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml install:: 2>&1 | tail -20
```

Expected: FAIL — module not declared.

- [ ] **Step 3: Declare the module**

Add to `launcher/src-tauri/src/lib.rs`:

```rust
pub mod install;
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml install:: 2>&1 | tail -20
```

Expected: `test result: ok. 6 passed`.

- [ ] **Step 5: Commit**

```bash
git add launcher/src-tauri/src
git commit -m "feat(launcher): settings model and .env rendering"
```

---

## Task 6: Writing and reading the install directory

**Files:**
- Modify: `launcher/src-tauri/src/install.rs`

- [ ] **Step 1: Write the failing tests**

Append to the `tests` module in `install.rs`:

```rust
    #[test]
    fn write_then_read_round_trips() {
        let dir = std::env::temp_dir().join(format!("bioflow-test-{}", std::process::id()));
        let mut s = settings();
        s.install_dir = dir.to_string_lossy().into();
        write_install(&s).expect("write");

        assert!(dir.join("docker-compose.yml").exists());
        assert!(dir.join(".env").exists());

        let read = read_install(&dir.to_string_lossy()).expect("read");
        assert_eq!(read.port, 5173);
        assert_eq!(read.storage_path, "/Users/me/BioFlow");
        assert!(!read.allow_network_access);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_reports_missing_install() {
        let missing = std::env::temp_dir().join("bioflow-does-not-exist-xyz");
        assert!(read_install(&missing.to_string_lossy()).is_none());
    }

    #[test]
    fn parse_env_ignores_comments_and_blanks() {
        let parsed = parse_env("# a comment\n\nWEB_PORT=8080\nBIND_ADDRESS=0.0.0.0\n");
        assert_eq!(parsed.get("WEB_PORT").map(String::as_str), Some("8080"));
        assert_eq!(parsed.get("BIND_ADDRESS").map(String::as_str), Some("0.0.0.0"));
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml install:: 2>&1 | tail -20
```

Expected: FAIL — `write_install`, `read_install`, and `parse_env` are not defined.

- [ ] **Step 3: Implement them**

Append to `install.rs`, above the `tests` module:

```rust
use std::collections::HashMap;

/// The compose file bundled at build time by build.rs. Written verbatim.
pub const BUNDLED_COMPOSE: &str = include_str!("../assets/docker-compose.yml");

/// Create the install directory and write both files. The compose file is
/// overwritten on every write so an upgraded launcher refreshes it; .env is
/// the launcher's own state and is rewritten from the passed settings.
pub fn write_install(s: &Settings) -> std::io::Result<()> {
    let dir = Path::new(&s.install_dir);
    std::fs::create_dir_all(dir)?;
    std::fs::write(dir.join("docker-compose.yml"), BUNDLED_COMPOSE)?;
    std::fs::write(dir.join(".env"), render_env(s))?;
    Ok(())
}

/// Parse a .env file into key/value pairs, ignoring comments and blank lines.
pub fn parse_env(contents: &str) -> HashMap<String, String> {
    contents
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .filter_map(|l| l.split_once('='))
        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
        .collect()
}

/// Read settings back from an install directory. None means "not installed",
/// which is the launcher's first state.
pub fn read_install(dir: &str) -> Option<Settings> {
    let path = Path::new(dir);
    let contents = std::fs::read_to_string(path.join(".env")).ok()?;
    let env = parse_env(&contents);
    Some(Settings {
        storage_path: env.get("BIOINFO_HOME")?.clone(),
        install_dir: dir.to_string(),
        port: env.get("WEB_PORT").and_then(|p| p.parse().ok()).unwrap_or(5173),
        allow_network_access: env.get("BIND_ADDRESS").map(|b| b == "0.0.0.0").unwrap_or(false),
        tag: env.get("BIOFLOW_TAG").cloned().unwrap_or_else(|| "latest".into()),
    })
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml install:: 2>&1 | tail -20
```

Expected: `test result: ok. 9 passed`.

- [ ] **Step 5: Remove the temporary constant from Task 3**

Delete the `pub const BUNDLED_COMPOSE` line added to `lib.rs` in Task 3 Step 4 — it now lives in `install.rs`.

```bash
cd launcher && cargo build --manifest-path src-tauri/Cargo.toml 2>&1 | tail -3
```

Expected: compiles with no unused-constant warning.

- [ ] **Step 6: Commit**

```bash
git add launcher/src-tauri/src
git commit -m "feat(launcher): read and write the install directory"
```

---

## Task 7: Port availability and health polling

**Files:**
- Create: `launcher/src-tauri/src/health.rs`
- Modify: `launcher/src-tauri/src/lib.rs`

- [ ] **Step 1: Write the failing tests**

Create `launcher/src-tauri/src/health.rs`:

```rust
use std::net::TcpListener;

/// True when nothing is listening on the port. Checked by binding it, which is
/// the only way to know for certain -- a connect-attempt probe cannot
/// distinguish "free" from "listening but not accepting".
pub fn port_is_free(port: u16) -> bool {
    TcpListener::bind(("127.0.0.1", port)).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unbound_port_is_free() {
        // Port 0 asks the OS for any free port; bind it, learn its number, then
        // release it. That number is then genuinely free.
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        assert!(port_is_free(port));
    }

    #[test]
    fn a_bound_port_is_not_free() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(!port_is_free(port));
        drop(listener);
    }
}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml health:: 2>&1 | tail -20
```

Expected: FAIL — module not declared.

- [ ] **Step 3: Declare the module and add health polling**

Add `pub mod health;` to `lib.rs`, then append to `health.rs`:

```rust
use std::time::Duration;

/// Poll the API's healthcheck until it answers or the deadline passes.
///
/// The browser handoff is gated on this rather than a fixed sleep: a cold
/// start against an empty Mongo volume takes far longer than a warm one, and
/// opening the browser early shows a connection error that reads as a broken
/// install.
pub async fn wait_for_healthy(port: u16, timeout_secs: u64) -> bool {
    let url = format!("http://127.0.0.1:{port}/healthz");
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
    {
        Ok(c) => c,
        Err(_) => return false,
    };
    let deadline = std::time::Instant::now() + Duration::from_secs(timeout_secs);
    while std::time::Instant::now() < deadline {
        if let Ok(resp) = client.get(&url).send().await {
            if resp.status().is_success() {
                return true;
            }
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
    false
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml health:: 2>&1 | tail -20
```

Expected: `test result: ok. 2 passed`.

- [ ] **Step 5: Commit**

```bash
git add launcher/src-tauri/src
git commit -m "feat(launcher): port checking and health polling"
```

---

## Task 8: Compose command construction

**Files:**
- Create: `launcher/src-tauri/src/compose.rs`
- Modify: `launcher/src-tauri/src/lib.rs`

- [ ] **Step 1: Write the failing tests**

Create `launcher/src-tauri/src/compose.rs`:

```rust
/// Build the argument list for a `docker compose` invocation.
///
/// --project-directory is always explicit: the launcher runs from wherever the
/// OS launched it, never from the install directory, so relying on the current
/// working directory would resolve the compose file and its relative paths
/// against the wrong place.
pub fn compose_args(install_dir: &str, action: &[&str]) -> Vec<String> {
    let mut args = vec![
        "compose".to_string(),
        "--project-directory".to_string(),
        install_dir.to_string(),
        "--project-name".to_string(),
        "bioflow".to_string(),
    ];
    args.extend(action.iter().map(|s| s.to_string()));
    args
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn up_is_detached() {
        let args = compose_args("/tmp/install", &["up", "-d"]);
        assert_eq!(
            args,
            vec!["compose", "--project-directory", "/tmp/install",
                 "--project-name", "bioflow", "up", "-d"]
        );
    }

    #[test]
    fn project_directory_always_precedes_the_action() {
        let args = compose_args("/tmp/install", &["down"]);
        let dir_idx = args.iter().position(|a| a == "--project-directory").unwrap();
        let action_idx = args.iter().position(|a| a == "down").unwrap();
        assert!(dir_idx < action_idx);
    }

    #[test]
    fn project_name_is_pinned() {
        // Compose derives a project name from the directory when none is given.
        // Pinning it means Stop finds what Run started regardless of where the
        // user chose to install.
        let args = compose_args("/somewhere/else", &["ps"]);
        assert!(args.windows(2).any(|w| w[0] == "--project-name" && w[1] == "bioflow"));
    }
}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml compose:: 2>&1 | tail -20
```

Expected: FAIL — module not declared.

- [ ] **Step 3: Declare the module**

Add `pub mod compose;` to `lib.rs`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml compose:: 2>&1 | tail -20
```

Expected: `test result: ok. 3 passed`.

- [ ] **Step 5: Add the streaming runner**

Append to `compose.rs`:

```rust
use tauri::{AppHandle, Emitter};
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

/// Run a compose action, streaming each output line to the UI as a
/// `compose-output` event, and resolve to the exit status.
///
/// Streaming rather than buffering matters because `up` on a cold machine
/// spends minutes pulling images. A launcher that showed nothing until the
/// command returned would be indistinguishable from a hung one.
pub async fn run_streaming(
    app: &AppHandle,
    install_dir: &str,
    action: &[&str],
) -> Result<bool, String> {
    let args = compose_args(install_dir, action);
    let (mut rx, _child) = app
        .shell()
        .command("docker")
        .args(args)
        .spawn()
        .map_err(|e| format!("failed to run docker: {e}"))?;

    let mut success = false;
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                let text = String::from_utf8_lossy(&line).to_string();
                let _ = app.emit("compose-output", text);
            }
            CommandEvent::Terminated(payload) => {
                success = payload.code == Some(0);
            }
            _ => {}
        }
    }
    Ok(success)
}
```

- [ ] **Step 6: Verify it compiles**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml compose:: 2>&1 | tail -20
```

Expected: still 3 passed, no compilation errors.

- [ ] **Step 7: Commit**

```bash
git add launcher/src-tauri/src
git commit -m "feat(launcher): compose command construction and output streaming"
```

---

## Task 9: The UI state machine

This is the piece with real branching logic, and it is a pure function so it can be tested exhaustively without Docker, without Tauri, and without a browser.

**Files:**
- Create: `launcher/src/state.ts`
- Create: `launcher/src/state.test.ts`
- Modify: `launcher/package.json`

- [ ] **Step 1: Add vitest**

```bash
cd launcher && npm install -D vitest
```

Add to the `scripts` block of `launcher/package.json`, matching `frontend/package.json`:

```json
"test": "vitest run"
```

- [ ] **Step 2: Write the failing tests**

Create `launcher/src/state.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { deriveState, type Snapshot } from "./state";

function snapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    docker: "ready",
    installed: true,
    containersUp: false,
    apiHealthy: false,
    updateAvailable: false,
    ...overrides,
  };
}

describe("deriveState", () => {
  it("sends an uninstalled user to setup even when Docker is ready", () => {
    expect(deriveState(snapshot({ installed: false }))).toBe("setup");
  });

  it("reports Docker missing before anything else", () => {
    // Docker outranks install state: there is nothing useful to do on any
    // other screen without a daemon.
    expect(deriveState(snapshot({ docker: "notInstalled", installed: false })))
      .toBe("dockerMissing");
  });

  it("reports a stopped daemon distinctly from a missing one", () => {
    expect(deriveState(snapshot({ docker: "notRunning" }))).toBe("dockerStopped");
  });

  it("is stopped when the daemon is ready but nothing is up", () => {
    expect(deriveState(snapshot({ containersUp: false }))).toBe("stopped");
  });

  it("is starting while containers are up but the API has not answered", () => {
    // The gap between `up` returning and the API serving is where a user would
    // otherwise be shown a Running state that does not work yet.
    expect(deriveState(snapshot({ containersUp: true, apiHealthy: false })))
      .toBe("starting");
  });

  it("is running only once the API answers", () => {
    expect(deriveState(snapshot({ containersUp: true, apiHealthy: true })))
      .toBe("running");
  });

  it("treats a dead daemon as Docker-stopped even if containers were up", () => {
    // Stale state is the failure this prevents: the last poll said Running,
    // the daemon has since died, and the UI must not keep claiming Running.
    expect(deriveState(snapshot({
      docker: "notRunning",
      containersUp: true,
      apiHealthy: true,
    }))).toBe("dockerStopped");
  });
});

describe("update availability", () => {
  it("is independent of the screen", () => {
    // The Update button is shown on the running screen only when an update
    // exists, but availability itself never changes which screen is shown.
    expect(deriveState(snapshot({
      containersUp: true, apiHealthy: true, updateAvailable: true,
    }))).toBe("running");
  });
});
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd launcher && npm test
```

Expected: FAIL — `./state` cannot be resolved.

- [ ] **Step 4: Implement the state machine**

Create `launcher/src/state.ts`:

```typescript
export type DockerState = "ready" | "notRunning" | "notInstalled";

export type Screen =
  | "dockerMissing"
  | "dockerStopped"
  | "setup"
  | "stopped"
  | "starting"
  | "running";

/** Everything the Rust side reports about the machine, in one poll. */
export interface Snapshot {
  docker: DockerState;
  installed: boolean;
  containersUp: boolean;
  apiHealthy: boolean;
  updateAvailable: boolean;
}

/**
 * Map a status snapshot to the screen to show.
 *
 * Order matters. Docker is checked first because no other screen can do
 * anything useful without a daemon, and `running` requires apiHealthy rather
 * than merely containersUp so that "Running" always means the API answered.
 */
export function deriveState(s: Snapshot): Screen {
  if (s.docker === "notInstalled") return "dockerMissing";
  if (s.docker === "notRunning") return "dockerStopped";
  if (!s.installed) return "setup";
  if (!s.containersUp) return "stopped";
  return s.apiHealthy ? "running" : "starting";
}
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd launcher && npm test
```

Expected: `Test Files 1 passed`, `Tests 8 passed`.

- [ ] **Step 6: Commit**

```bash
git add launcher/src launcher/package.json launcher/package-lock.json
git commit -m "feat(launcher): UI state machine with tests"
```

---

## Task 10: Wire the Tauri commands

**Files:**
- Create: `launcher/src-tauri/src/status.rs`
- Modify: `launcher/src-tauri/src/lib.rs`
- Create: `launcher/src/api.ts`

- [ ] **Step 1: Write the status snapshot type**

Create `launcher/src-tauri/src/status.rs`:

```rust
use serde::Serialize;

use crate::docker::{self, DockerState};
use crate::install;

/// Mirrors the TypeScript `Snapshot` in src/state.ts. Both sides must agree on
/// these field names; serde's camelCase rename is what makes them match.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Snapshot {
    pub docker: DockerState,
    pub installed: bool,
    pub containers_up: bool,
    pub api_healthy: bool,
    pub update_available: bool,
}

/// Default install directory per platform. Chosen so an ordinary user never
/// has to answer this question, while still being able to.
pub fn default_install_dir() -> String {
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".into());
    format!("{home}/.bioflow")
}

pub fn default_storage_dir() -> String {
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".into());
    format!("{home}/BioFlow")
}

/// Assemble the snapshot the UI's state machine consumes.
pub async fn snapshot() -> Snapshot {
    let docker_state = docker::probe();
    let dir = default_install_dir();
    let settings = install::read_install(&dir);
    let installed = settings.is_some();

    // Containers and health are only meaningful with a daemon and an install.
    let (containers_up, api_healthy) = match (&docker_state, &settings) {
        (DockerState::Ready, Some(s)) => {
            let up = crate::compose::any_running(&s.install_dir);
            let healthy = if up {
                crate::health::wait_for_healthy(s.port, 1).await
            } else {
                false
            };
            (up, healthy)
        }
        _ => (false, false),
    };

    Snapshot {
        docker: docker_state,
        installed,
        containers_up,
        api_healthy,
        update_available: false,
    }
}
```

- [ ] **Step 2: Add the container check to compose.rs**

Append to `launcher/src-tauri/src/compose.rs`:

```rust
/// True when the compose project has at least one running container.
/// Uses a plain std Command rather than the shell plugin because this runs on
/// a poll and needs no streaming.
pub fn any_running(install_dir: &str) -> bool {
    let args = compose_args(install_dir, &["ps", "--quiet"]);
    match std::process::Command::new("docker").args(args).output() {
        Ok(out) => out.status.success() && !out.stdout.is_empty(),
        Err(_) => false,
    }
}
```

- [ ] **Step 3: Register the commands**

Replace the builder in `launcher/src-tauri/src/lib.rs` with:

```rust
pub mod compose;
pub mod docker;
pub mod health;
pub mod install;
pub mod status;

use install::Settings;

#[tauri::command]
async fn get_status() -> status::Snapshot {
    status::snapshot().await
}

#[tauri::command]
fn get_defaults() -> (String, String) {
    (status::default_storage_dir(), status::default_install_dir())
}

#[tauri::command]
fn check_port(port: u16) -> bool {
    health::port_is_free(port)
}

#[tauri::command]
fn start_docker() -> bool {
    docker::try_start()
}

#[tauri::command]
async fn save_and_start(app: tauri::AppHandle, settings: Settings) -> Result<bool, String> {
    install::write_install(&settings).map_err(|e| e.to_string())?;
    compose::run_streaming(&app, &settings.install_dir, &["up", "-d"]).await
}

#[tauri::command]
async fn stop_stack(app: tauri::AppHandle, install_dir: String) -> Result<bool, String> {
    compose::run_streaming(&app, &install_dir, &["down"]).await
}

#[tauri::command]
async fn update_stack(app: tauri::AppHandle, install_dir: String) -> Result<bool, String> {
    let pulled = compose::run_streaming(&app, &install_dir, &["pull"]).await?;
    if !pulled {
        return Ok(false);
    }
    compose::run_streaming(&app, &install_dir, &["up", "-d"]).await
}

#[tauri::command]
async fn wait_healthy(port: u16) -> bool {
    health::wait_for_healthy(port, 180).await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            get_status,
            get_defaults,
            check_port,
            start_docker,
            save_and_start,
            stop_stack,
            update_stack,
            wait_healthy,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
```

- [ ] **Step 4: Verify it compiles and the existing tests still pass**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml 2>&1 | tail -20
```

Expected: compiles; 18 tests pass (4 docker + 9 install + 2 health + 3 compose).

- [ ] **Step 5: Write the typed frontend wrapper**

Create `launcher/src/api.ts`:

```typescript
import { invoke } from "@tauri-apps/api/core";
import type { Snapshot } from "./state";

export interface Settings {
  storagePath: string;
  installDir: string;
  port: number;
  allowNetworkAccess: boolean;
  tag: string;
}

export const getStatus = () => invoke<Snapshot>("get_status");
export const getDefaults = () => invoke<[string, string]>("get_defaults");
export const checkPort = (port: number) => invoke<boolean>("check_port", { port });
export const startDocker = () => invoke<boolean>("start_docker");
export const saveAndStart = (settings: Settings) =>
  invoke<boolean>("save_and_start", { settings });
export const stopStack = (installDir: string) =>
  invoke<boolean>("stop_stack", { installDir });
export const updateStack = (installDir: string) =>
  invoke<boolean>("update_stack", { installDir });
export const waitHealthy = (port: number) => invoke<boolean>("wait_healthy", { port });
```

- [ ] **Step 6: Commit**

```bash
git add launcher
git commit -m "feat(launcher): wire Tauri commands to the frontend"
```

---

## Task 11: Screens

**Files:**
- Modify: `launcher/src/App.tsx`
- Create: `launcher/src/screens/DockerMissing.tsx`
- Create: `launcher/src/screens/Setup.tsx`
- Create: `launcher/src/screens/Running.tsx`
- Create: `launcher/src/screens/Stopped.tsx`

- [ ] **Step 1: The Docker screens**

Create `launcher/src/screens/DockerMissing.tsx`:

```tsx
import { openUrl } from "@tauri-apps/plugin-opener";

export function DockerMissing({ onRecheck }: { onRecheck: () => void }) {
  return (
    <div className="screen">
      <h1>Docker is required</h1>
      <p>
        BioFlow runs inside Docker. Install Docker Desktop, start it, then click
        Check Again.
      </p>
      <button onClick={() => openUrl("https://www.docker.com/products/docker-desktop/")}>
        Download Docker Desktop
      </button>
      <button onClick={onRecheck}>Check Again</button>
    </div>
  );
}
```

Create `launcher/src/screens/Stopped.tsx`:

```tsx
export function Stopped({ onRun, busy }: { onRun: () => void; busy: boolean }) {
  return (
    <div className="screen">
      <h1>BioFlow is stopped</h1>
      <button onClick={onRun} disabled={busy}>
        {busy ? "Starting…" : "Run BioFlow"}
      </button>
    </div>
  );
}
```

- [ ] **Step 2: The setup screen**

Create `launcher/src/screens/Setup.tsx`:

```tsx
import { useEffect, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { checkPort, getDefaults, type Settings } from "../api";

export function Setup({ onSubmit }: { onSubmit: (s: Settings) => void }) {
  const [storagePath, setStoragePath] = useState("");
  const [installDir, setInstallDir] = useState("");
  const [port, setPort] = useState(5173);
  const [allowNetworkAccess, setAllow] = useState(false);
  const [portError, setPortError] = useState<string | null>(null);

  useEffect(() => {
    getDefaults().then(([storage, install]) => {
      setStoragePath(storage);
      setInstallDir(install);
    });
  }, []);

  async function pickFolder(set: (v: string) => void) {
    const chosen = await open({ directory: true });
    if (typeof chosen === "string") set(chosen);
  }

  async function submit() {
    if (!(await checkPort(port))) {
      setPortError(`Port ${port} is already in use. Choose another.`);
      return;
    }
    setPortError(null);
    onSubmit({ storagePath, installDir, port, allowNetworkAccess, tag: "latest" });
  }

  return (
    <div className="screen">
      <h1>Set up BioFlow</h1>

      <label>Where should BioFlow store your data?</label>
      <div className="row">
        <input value={storagePath} onChange={(e) => setStoragePath(e.target.value)} />
        <button onClick={() => pickFolder(setStoragePath)}>Browse…</button>
      </div>

      <label>Where should BioFlow be installed?</label>
      <div className="row">
        <input value={installDir} onChange={(e) => setInstallDir(e.target.value)} />
        <button onClick={() => pickFolder(setInstallDir)}>Browse…</button>
      </div>

      <label>Port</label>
      <input
        type="number"
        value={port}
        onChange={(e) => setPort(Number(e.target.value))}
      />
      {portError && <p className="error">{portError}</p>}

      <label className="checkbox">
        <input
          type="checkbox"
          checked={allowNetworkAccess}
          onChange={(e) => setAllow(e.target.checked)}
        />
        Allow access from other devices on my network
      </label>
      <p className="hint">
        Off by default. BioFlow has no password, so anyone who can reach this
        computer on the network could use it.
      </p>

      <button onClick={submit}>Install and Run</button>
    </div>
  );
}
```

- [ ] **Step 3: The running screen**

Create `launcher/src/screens/Running.tsx`:

```tsx
import { openUrl } from "@tauri-apps/plugin-opener";

interface Props {
  port: number;
  updateAvailable: boolean;
  busy: boolean;
  onStop: () => void;
  onUpdate: () => void;
}

export function Running({ port, updateAvailable, busy, onStop, onUpdate }: Props) {
  const url = `http://localhost:${port}`;
  return (
    <div className="screen">
      <h1>BioFlow is running</h1>
      <p>
        <a onClick={() => openUrl(url)}>{url}</a>
      </p>
      <button onClick={() => openUrl(url)}>Open BioFlow</button>
      <button onClick={onStop} disabled={busy}>Stop</button>
      {updateAvailable && (
        <button onClick={onUpdate} disabled={busy}>Update</button>
      )}
      <p className="hint">
        You can close this window — BioFlow keeps running. Open the launcher
        again to stop it or change settings.
      </p>
    </div>
  );
}
```

- [ ] **Step 4: Route between them**

Replace `launcher/src/App.tsx`:

```tsx
import { useCallback, useEffect, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { openUrl } from "@tauri-apps/plugin-opener";
import { deriveState, type Snapshot } from "./state";
import * as api from "./api";
import { DockerMissing } from "./screens/DockerMissing";
import { Setup } from "./screens/Setup";
import { Stopped } from "./screens/Stopped";
import { Running } from "./screens/Running";
import "./App.css";

const EMPTY: Snapshot = {
  docker: "notInstalled",
  installed: false,
  containersUp: false,
  apiHealthy: false,
  updateAvailable: false,
};

export default function App() {
  const [snap, setSnap] = useState<Snapshot>(EMPTY);
  const [port, setPort] = useState(5173);
  const [installDir, setInstallDir] = useState("");
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState<string[]>([]);

  const refresh = useCallback(async () => {
    setSnap(await api.getStatus());
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    const unlisten = listen<string>("compose-output", (e) =>
      setLog((prev) => [...prev.slice(-200), e.payload]),
    );
    return () => {
      clearInterval(timer);
      unlisten.then((f) => f());
    };
  }, [refresh]);

  useEffect(() => {
    api.getDefaults().then(([, install]) => setInstallDir(install));
  }, []);

  const screen = deriveState(snap);

  async function handleSetup(settings: api.Settings) {
    setBusy(true);
    setPort(settings.port);
    setInstallDir(settings.installDir);
    await api.saveAndStart(settings);
    // Health-gated, not a fixed sleep: a cold start against an empty Mongo
    // volume takes far longer than a warm one.
    if (await api.waitHealthy(settings.port)) {
      await openUrl(`http://localhost:${settings.port}`);
    }
    setBusy(false);
    refresh();
  }

  async function handleRun() {
    setBusy(true);
    await api.saveAndStart({
      storagePath: "",
      installDir,
      port,
      allowNetworkAccess: false,
      tag: "latest",
    });
    setBusy(false);
    refresh();
  }

  return (
    <main className="container">
      {screen === "dockerMissing" && <DockerMissing onRecheck={refresh} />}
      {screen === "dockerStopped" && (
        <DockerMissing onRecheck={async () => { await api.startDocker(); refresh(); }} />
      )}
      {screen === "setup" && <Setup onSubmit={handleSetup} />}
      {screen === "stopped" && <Stopped onRun={handleRun} busy={busy} />}
      {screen === "starting" && <p>Starting BioFlow…</p>}
      {screen === "running" && (
        <Running
          port={port}
          updateAvailable={snap.updateAvailable}
          busy={busy}
          onStop={async () => { setBusy(true); await api.stopStack(installDir); setBusy(false); refresh(); }}
          onUpdate={async () => { setBusy(true); await api.updateStack(installDir); setBusy(false); refresh(); }}
        />
      )}
      {busy && log.length > 0 && (
        <pre className="log">{log.slice(-12).join("\n")}</pre>
      )}
    </main>
  );
}
```

**Known gap, resolved in Task 12:** `handleRun` passes an empty `storagePath`, which would rewrite `.env` with a blank `BIOINFO_HOME`. Task 12 replaces it with a read of the saved settings. Do not ship Task 11 on its own.

- [ ] **Step 5: Verify the state machine tests still pass and the app builds**

```bash
cd launcher && npm test && npm run tauri build -- --debug 2>&1 | tail -5
```

Expected: 8 tests pass; the debug binary builds.

- [ ] **Step 6: Commit**

```bash
git add launcher/src
git commit -m "feat(launcher): setup, running, and Docker screens"
```

---

## Task 12: Load saved settings on start

Task 11 left `handleRun` sending an empty storage path. The launcher must read what setup already saved.

**Files:**
- Modify: `launcher/src-tauri/src/lib.rs`
- Modify: `launcher/src/api.ts`
- Modify: `launcher/src/App.tsx`

- [ ] **Step 1: Expose saved settings as a command**

Add to `launcher/src-tauri/src/lib.rs`, and register it in `generate_handler!`:

```rust
#[tauri::command]
fn get_settings() -> Option<Settings> {
    install::read_install(&status::default_install_dir())
}
```

- [ ] **Step 2: Add the wrapper**

Add to `launcher/src/api.ts`:

```typescript
export const getSettings = () => invoke<Settings | null>("get_settings");
```

- [ ] **Step 3: Use it**

In `launcher/src/App.tsx`, replace the `getDefaults` effect and `handleRun`:

```tsx
  const [settings, setSettings] = useState<api.Settings | null>(null);

  useEffect(() => {
    api.getSettings().then((s) => {
      if (s) {
        setSettings(s);
        setPort(s.port);
        setInstallDir(s.installDir);
      }
    });
  }, [snap.installed]);

  async function handleRun() {
    if (!settings) return;
    setBusy(true);
    await api.saveAndStart(settings);
    setBusy(false);
    refresh();
  }
```

- [ ] **Step 4: Verify**

```bash
cd launcher && npm test && cargo test --manifest-path src-tauri/Cargo.toml 2>&1 | tail -5
```

Expected: 8 TypeScript tests pass, 18 Rust tests pass.

- [ ] **Step 5: Commit**

```bash
git add launcher
git commit -m "fix(launcher): run uses saved settings rather than blanks"
```

---

## Task 13: Update availability check

**Files:**
- Create: `launcher/src-tauri/src/update.rs`
- Modify: `launcher/src-tauri/src/lib.rs`
- Modify: `launcher/src-tauri/src/status.rs`

- [ ] **Step 1: Write the failing test**

Create `launcher/src-tauri/src/update.rs`:

```rust
/// Decide whether to offer an Update, given the local and remote image digests.
///
/// Unknown remote (offline, registry down, rate-limited) must mean "no update
/// offered" rather than "update available" -- a button that fails the moment
/// it is pressed is worse than no button.
pub fn update_available(local: Option<&str>, remote: Option<&str>) -> bool {
    match (local, remote) {
        (Some(l), Some(r)) => l != r,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn differing_digests_offer_an_update() {
        assert!(update_available(Some("sha256:aaa"), Some("sha256:bbb")));
    }

    #[test]
    fn matching_digests_offer_nothing() {
        assert!(!update_available(Some("sha256:aaa"), Some("sha256:aaa")));
    }

    #[test]
    fn offline_offers_nothing() {
        assert!(!update_available(Some("sha256:aaa"), None));
    }

    #[test]
    fn a_missing_local_image_offers_nothing() {
        // Nothing pulled yet: `up` will fetch it anyway, so an Update button
        // would be redundant and confusing.
        assert!(!update_available(None, Some("sha256:bbb")));
    }
}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml update:: 2>&1 | tail -20
```

Expected: FAIL — module not declared.

- [ ] **Step 3: Declare the module**

Add `pub mod update;` to `lib.rs`.

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml update:: 2>&1 | tail -20
```

Expected: `test result: ok. 4 passed`.

- [ ] **Step 5: Add the digest lookups**

Append to `update.rs`:

```rust
/// The local image digest, or None when the image has never been pulled.
pub fn local_digest(image: &str) -> Option<String> {
    let out = std::process::Command::new("docker")
        .args(["image", "inspect", image, "--format", "{{index .RepoDigests 0}}"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if s.is_empty() { None } else { Some(s) }
}

/// The registry's current digest for the tag. Cheap: a manifest HEAD, not a
/// download. Fails silently offline, which `update_available` treats as
/// "no update".
pub fn remote_digest(image: &str) -> Option<String> {
    let out = std::process::Command::new("docker")
        .args(["manifest", "inspect", image, "--verbose"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let json: serde_json::Value = serde_json::from_slice(&out.stdout).ok()?;
    json.get("Descriptor")
        .and_then(|d| d.get("digest"))
        .and_then(|d| d.as_str())
        .map(String::from)
}
```

- [ ] **Step 6: Use it in the snapshot**

In `launcher/src-tauri/src/status.rs`, replace `update_available: false` with:

```rust
        update_available: {
            let image = format!(
                "ghcr.io/syntheticgio/bioflow-api:{}",
                settings.as_ref().map(|s| s.tag.as_str()).unwrap_or("latest")
            );
            crate::update::update_available(
                crate::update::local_digest(&image).as_deref(),
                crate::update::remote_digest(&image).as_deref(),
            )
        },
```

- [ ] **Step 7: Verify**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml 2>&1 | tail -5
```

Expected: 22 tests pass (18 + 4 update).

- [ ] **Step 8: Commit**

```bash
git add launcher/src-tauri/src
git commit -m "feat(launcher): offer Update only when a newer image exists"
```

---

## Task 14: Settings screen

**Files:**
- Create: `launcher/src/screens/Settings.tsx`
- Modify: `launcher/src/screens/Running.tsx`
- Modify: `launcher/src/App.tsx`

- [ ] **Step 1: Build the screen**

Create `launcher/src/screens/Settings.tsx`:

```tsx
import { useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { checkPort, type Settings as S } from "../api";

interface Props {
  initial: S;
  onSave: (s: S) => void;
  onCancel: () => void;
}

export function Settings({ initial, onSave, onCancel }: Props) {
  const [s, setS] = useState<S>(initial);
  const [portError, setPortError] = useState<string | null>(null);

  async function save() {
    if (s.port !== initial.port && !(await checkPort(s.port))) {
      setPortError(`Port ${s.port} is already in use.`);
      return;
    }
    setPortError(null);
    onSave(s);
  }

  const storageChanged = s.storagePath !== initial.storagePath;

  return (
    <div className="screen">
      <h1>Settings</h1>

      <label>Data location</label>
      <div className="row">
        <input
          value={s.storagePath}
          onChange={(e) => setS({ ...s, storagePath: e.target.value })}
        />
        <button
          onClick={async () => {
            const c = await open({ directory: true });
            if (typeof c === "string") setS({ ...s, storagePath: c });
          }}
        >
          Browse…
        </button>
      </div>
      {storageChanged && (
        <p className="warn">
          Changing this points BioFlow at a different folder. It does not move
          your existing data — projects stored in the old location will not
          appear until you point it back.
        </p>
      )}

      <label>Port</label>
      <input
        type="number"
        value={s.port}
        onChange={(e) => setS({ ...s, port: Number(e.target.value) })}
      />
      {portError && <p className="error">{portError}</p>}

      <label className="checkbox">
        <input
          type="checkbox"
          checked={s.allowNetworkAccess}
          onChange={(e) => setS({ ...s, allowNetworkAccess: e.target.checked })}
        />
        Allow access from other devices on my network
      </label>

      <p className="hint">Saving restarts BioFlow.</p>
      <button onClick={save}>Save and Restart</button>
      <button onClick={onCancel}>Cancel</button>
    </div>
  );
}
```

- [ ] **Step 2: Add the entry point**

In `launcher/src/screens/Running.tsx`, add `onSettings: () => void` to `Props` and a button beside Stop:

```tsx
      <button onClick={onSettings}>Settings</button>
```

- [ ] **Step 3: Wire it in App.tsx**

Add to `App.tsx` state and rendering:

```tsx
  const [showSettings, setShowSettings] = useState(false);

  async function handleSettingsSave(next: api.Settings) {
    setBusy(true);
    setShowSettings(false);
    await api.stopStack(next.installDir);
    await api.saveAndStart(next);
    setSettings(next);
    setPort(next.port);
    setBusy(false);
    refresh();
  }
```

And render it ahead of the screen switch:

```tsx
  if (showSettings && settings) {
    return (
      <main className="container">
        <Settings
          initial={settings}
          onSave={handleSettingsSave}
          onCancel={() => setShowSettings(false)}
        />
      </main>
    );
  }
```

Pass `onSettings={() => setShowSettings(true)}` to `<Running />`.

- [ ] **Step 4: Verify**

```bash
cd launcher && npm test && npm run tauri build -- --debug 2>&1 | tail -5
```

Expected: 8 tests pass; the binary builds.

- [ ] **Step 5: Commit**

```bash
git add launcher/src
git commit -m "feat(launcher): settings screen for port, storage, and network access"
```

---

## Task 15: The remaining error paths

The spec names five error cases. Tasks 5 and 11 cover two (port in use, macOS sharing — though the sharing check is still unreachable from the UI). This task wires that one up and adds the other three. Without it, `macos_shareable` and `PathVerdict` are dead code that passes its own tests while never running — the failure `CLAUDE.md` describes, where green tests describe hand-built objects rather than the real path.

**Files:**
- Modify: `launcher/src-tauri/src/install.rs`
- Modify: `launcher/src-tauri/src/lib.rs`
- Modify: `launcher/src-tauri/src/docker.rs`
- Modify: `launcher/src/api.ts`
- Modify: `launcher/src/screens/Setup.tsx`
- Modify: `launcher/src/App.tsx`

- [ ] **Step 1: Write the failing tests**

Append to the `tests` module in `install.rs`:

```rust
    #[test]
    fn a_missing_directory_is_reported_missing() {
        let missing = std::env::temp_dir().join("bioflow-nope-abc123");
        assert_eq!(verdict_for(&missing.to_string_lossy(), "/Users/me"), PathVerdict::Missing);
    }

    #[test]
    fn an_existing_writable_home_path_is_ok() {
        let dir = std::env::temp_dir();
        // temp_dir is under /private or /tmp on macOS, both shareable.
        assert_eq!(verdict_for(&dir.to_string_lossy(), "/Users/me"), PathVerdict::Ok);
    }

    #[test]
    fn a_half_written_install_is_detected() {
        // A pull that failed leaves the directory and compose file but no
        // running stack. Setup must be able to resume rather than refuse.
        let dir = std::env::temp_dir().join(format!("bioflow-partial-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("docker-compose.yml"), "services: {}").unwrap();
        assert!(is_partial_install(&dir.to_string_lossy()));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_complete_install_is_not_partial() {
        let dir = std::env::temp_dir().join(format!("bioflow-complete-{}", std::process::id()));
        let mut s = settings();
        s.install_dir = dir.to_string_lossy().into();
        write_install(&s).unwrap();
        assert!(!is_partial_install(&dir.to_string_lossy()));
        std::fs::remove_dir_all(&dir).ok();
    }
```

Append to the `tests` module in `docker.rs`:

```rust
    #[test]
    fn disk_full_is_recognised_in_compose_output() {
        assert!(is_disk_full("write /var/lib/docker: no space left on device"));
        assert!(is_disk_full("failed to register layer: No space left on device"));
    }

    #[test]
    fn ordinary_output_is_not_disk_full() {
        assert!(!is_disk_full("Pulling from syntheticgio/bioflow-api"));
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml 2>&1 | tail -20
```

Expected: FAIL — `verdict_for`, `is_partial_install`, and `is_disk_full` are not defined.

- [ ] **Step 3: Implement them**

Append to `install.rs`, above the `tests` module:

```rust
/// Classify a chosen storage path before the stack is ever started.
///
/// The macOS sharing case is the reason this exists: an unshared path starts
/// cleanly and leaves /data empty, and the symptom appears much later with
/// nothing pointing at the cause.
pub fn verdict_for(path: &str, home: &str) -> PathVerdict {
    let p = Path::new(path);
    if !p.exists() {
        return PathVerdict::Missing;
    }
    match std::fs::metadata(p) {
        Ok(m) if m.permissions().readonly() => return PathVerdict::NotWritable,
        Err(_) => return PathVerdict::NotWritable,
        _ => {}
    }
    if cfg!(target_os = "macos") && !macos_shareable(path, home) {
        return PathVerdict::NotShareable;
    }
    PathVerdict::Ok
}

/// True when an install directory exists but has no .env -- the shape a failed
/// or interrupted first run leaves behind. Setup resumes from here rather than
/// treating the directory as a complete install.
pub fn is_partial_install(dir: &str) -> bool {
    let p = Path::new(dir);
    p.exists() && !p.join(".env").exists()
}
```

Append to `docker.rs`, above its `tests` module:

```rust
/// Recognise an out-of-disk failure in compose output so it can be surfaced
/// as its own message rather than buried in a wall of pull progress.
pub fn is_disk_full(output: &str) -> bool {
    output.to_lowercase().contains("no space left on device")
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd launcher && cargo test --manifest-path src-tauri/Cargo.toml 2>&1 | tail -5
```

Expected: 28 tests pass (22 + 4 install + 2 docker).

- [ ] **Step 5: Expose the path check and wire it into Setup**

Add to `lib.rs`, registering it in `generate_handler!`:

```rust
#[tauri::command]
fn check_path(path: String) -> install::PathVerdict {
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_default();
    install::verdict_for(&path, &home)
}
```

Add to `api.ts`:

```typescript
export type PathVerdict = "ok" | "notShareable" | "missing" | "notWritable";
export const checkPath = (path: string) => invoke<PathVerdict>("check_path", { path });
```

In `Setup.tsx`, add a storage-path verdict alongside the existing port check. Add to the component's state and `submit`:

```tsx
  const [pathWarning, setPathWarning] = useState<string | null>(null);

  async function validatePath(p: string) {
    const verdict = await checkPath(p);
    setPathWarning(
      verdict === "notShareable"
        ? "Docker Desktop cannot read this folder by default. Either choose a folder inside your home directory, or add this one under Docker Desktop → Settings → Resources → File Sharing."
        : verdict === "notWritable"
        ? "This folder is not writable."
        : null,
    );
    return verdict !== "notWritable";
  }
```

Call `validatePath(storagePath)` at the top of `submit()` and return early when it is false; render `{pathWarning && <p className="warn">{pathWarning}</p>}` under the storage input. Import `checkPath` from `../api`.

`Missing` is not an error here — setup creates the directory. Only `NotWritable` blocks, and `NotShareable` warns without blocking, since a user who has already added a File Sharing entry knows better than the heuristic.

- [ ] **Step 5b: Use the partial-install check so a failed first run resumes**

`is_partial_install` exists to make a failed setup recoverable: a pull that ran out of disk leaves the directory and compose file behind but no `.env`, and `read_install` correctly reports "not installed" — so the user lands back on Setup. Without this, Setup shows blank defaults and the half-written directory is invisible.

Add to `lib.rs`, registered in `generate_handler!`:

```rust
#[tauri::command]
fn is_resumable() -> bool {
    install::is_partial_install(&status::default_install_dir())
}
```

Add to `api.ts`:

```typescript
export const isResumable = () => invoke<boolean>("is_resumable");
```

In `Setup.tsx`, call it in the existing mount effect and show a banner when true:

```tsx
  const [resuming, setResuming] = useState(false);

  useEffect(() => {
    isResumable().then(setResuming);
  }, []);
```

Render above the form:

```tsx
      {resuming && (
        <p className="warn">
          A previous setup did not finish. Continuing will reuse the same
          folder and pick up where it stopped.
        </p>
      )}
```

Import `isResumable` from `../api`.

- [ ] **Step 6: Surface disk-full and daemon death**

In `App.tsx`, add to the `compose-output` listener so the specific cause is not lost in the stream:

```tsx
    const [fatal, setFatal] = useState<string | null>(null);

    const unlisten = listen<string>("compose-output", (e) => {
      setLog((prev) => [...prev.slice(-200), e.payload]);
      if (e.payload.toLowerCase().includes("no space left on device")) {
        setFatal("Out of disk space. Free up space and try again.");
      }
    });
```

Render `{fatal && <p className="error">{fatal}</p>}`, and clear it (`setFatal(null)`) at the start of `handleRun` and `handleSetup`.

Daemon death needs no new code: the 5-second poll in Task 11 already re-reads `docker::probe()` on every tick, and `deriveState` returns `dockerStopped` whenever the daemon is unreachable regardless of the previous state. The state-machine test `treats a dead daemon as Docker-stopped even if containers were up` (Task 9) is what proves it.

- [ ] **Step 7: Implement the 60-second Docker start timeout**

The spec bounds the "Waiting for Docker…" state at 60 seconds. Add to `lib.rs`, registered in `generate_handler!`:

```rust
/// Start Docker and wait for the daemon, bounded at 60 seconds.
///
/// The bound is what stops the UI spinning forever: daemon startup takes
/// 10-30 seconds and can fail without reporting anything. On timeout the user
/// returns to the manual screen and its Check Again button.
#[tauri::command]
async fn start_docker_and_wait() -> bool {
    if !docker::try_start() {
        return false;
    }
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
    while std::time::Instant::now() < deadline {
        if docker::probe() == docker::DockerState::Ready {
            return true;
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
    false
}
```

Add to `api.ts`:

```typescript
export const startDockerAndWait = () => invoke<boolean>("start_docker_and_wait");
```

In `App.tsx`, replace the `dockerStopped` handler's `api.startDocker()` with `api.startDockerAndWait()`. Keep `startDocker` registered — it is the unwaited variant and the Check Again button uses `refresh()` alone.

- [ ] **Step 8: Verify**

```bash
cd launcher && npm test && cargo test --manifest-path src-tauri/Cargo.toml 2>&1 | tail -5
```

Expected: 8 TypeScript tests pass, 28 Rust tests pass.

- [ ] **Step 9: Commit**

```bash
git add launcher
git commit -m "feat(launcher): surface the remaining error paths"
```

---

## Task 16: Manual end-to-end verification

No automated test covers this; the spec is explicit that cross-platform behavior is verified by hand.

**Prerequisite:** issue #37 must be complete. Until the images are published, `docker compose up` fails on missing build contexts and only the pre-start states below can be checked.

- [ ] **Step 1: Build a release binary**

```bash
cd launcher && npm run tauri build
```

- [ ] **Step 2: Verify each state**

Work through the table, recording what you observe:

| Check | How to produce it | Expected |
|---|---|---|
| Docker missing | Rename the `docker` binary temporarily | Download screen, no crash |
| Docker stopped | Quit Docker Desktop | Start attempt, then Running once it comes up |
| First run | Delete `~/.bioflow` | Setup screen with populated defaults |
| Port in use | Enter a port something else is listening on | Inline error, setup does not proceed |
| Cold start | Fresh install, empty volumes | Browser opens only after the API answers |
| Close and reopen | Close the window while running | Reopens in the Running state, stack never stopped |
| Stop | Click Stop | Containers gone; `docker ps` confirms |
| Loopback default | Start without the toggle | Unreachable from another device on the LAN |
| Network toggle | Enable it, save | Reachable from another device |

- [ ] **Step 3: Confirm the bind address took effect**

```bash
docker inspect bioflow-web-1 --format '{{json .NetworkSettings.Ports}}'
```

Expected: `127.0.0.1` as the `HostIp` by default; `0.0.0.0` after enabling network access.

- [ ] **Step 4: Repeat on the other two platforms**

macOS, Windows, and Linux each need their own pass. The platform-specific code is `docker::start_command` and the default paths in `status.rs`, so those are what to watch.

- [ ] **Step 5: Record the results**

Add a short verification note to issue #28 listing what was checked on each platform and anything that failed.

---

## Task 17: Close out the work

- [ ] **Step 1: Run the full suite**

```bash
cd launcher && npm test && cargo test --manifest-path src-tauri/Cargo.toml 2>&1 | tail -5
```

Expected: 8 TypeScript tests, 28 Rust tests, all passing. Read the counts rather than the exit code.

- [ ] **Step 2: Update `docs/TODO.md`**

The `Helper install program` entry is resolved by this work *except* for the optional-tools checkbox, which moved to issue #40. Per `CLAUDE.md`, a partially resolved entry stays in `docs/TODO.md` rather than moving to `TODO-done.md`. Append a note under the heading recording what shipped, when, where the code lives, and that the pre-pull checkbox remains open as #40.

- [ ] **Step 3: Note what the implementation did differently**

In the same note, record any departures from `docs/superpowers/specs/2026-08-04-native-launcher-contract-design.md`. Per `CLAUDE.md`, that delta is the most valuable sentence in the entry.

- [ ] **Step 4: Commit**

```bash
git add docs/TODO.md
git commit -m "docs: record launcher progress against the install-program entry"
```

---

## Verification Summary

| Layer | Covered by | Count |
|---|---|---|
| Docker classification | `docker.rs` tests | 4 |
| Settings and `.env` | `install.rs` tests | 9 |
| Ports | `health.rs` tests | 2 |
| Compose arguments | `compose.rs` tests | 3 |
| Update decision | `update.rs` tests | 4 |
| UI state machine | `state.test.ts` | 8 |
| Path verdicts and partial installs | `install.rs` tests | 4 |
| Disk-full detection | `docker.rs` tests | 2 |
| Real Docker, three platforms | Task 16, manual | — |
