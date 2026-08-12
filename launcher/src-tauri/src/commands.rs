//! Tauri commands: the thin IPC surface the UI calls. Each command
//! delegates straight to the tested logic in `state`, `actions`, `setup`,
//! and `settings` -- nothing here should ever need its own test, since
//! everything it does is already covered where the real logic lives.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::path::BaseDirectory;
use tauri::{Manager, State};
use tauri_plugin_opener::OpenerExt;

use crate::actions::{self, RunOutcome, StopOutcome, UpdateOutcome};
use crate::docker::{ActionResult, DockerBackend, DockerPresence, ShellDocker};
use crate::optional_tools::{OptionalTool, StackToolsClient, ToolsClient};
use crate::settings::{self, CurrentSettings, SettingsUpdateError};
use crate::setup::{self, InstallError, InstallInputs, PortValidation, SetupDefaults, StoragePathValidation};
use crate::setup::node::{self as node_setup, NodeInstallError, NodeInstallInputs};
use crate::remote::{self, SshCreds, SshResult};
use crate::state::{self, InstallInfo, LauncherState};
use crate::update_check::{self, DockerImageInspector, GhcrClient, VersionOptions};

/// The two images the design spec's "Changes required in this repository"
/// section names -- api and worker share bioflow-backend, so there are only
/// two distinct images to check, not three services.
const CHECKABLE_IMAGES: &[&str] = &["syntheticgio/bioflow-backend", "syntheticgio/bioflow-web"];

/// The name the launcher's bundled compose resource is registered under --
/// see `tauri.conf.json`'s `bundle.resources` mapping
/// `../../docker-compose.yml` to this name, and `launcher/README.md` on why
/// that mapping must stay a reference to the repository's own file.
const BUNDLED_COMPOSE_RESOURCE: &str = "docker-compose.yml";

/// Tracks the state that isn't a Docker fact: where (if anywhere) the stack
/// is installed, and which port it's configured to serve on. Both are
/// `None`/absent before first-run setup completes.
///
/// No `ShellDocker` field: every command below constructs one fresh inside
/// its own `spawn_blocking` closure instead, since `ShellDocker` is a
/// zero-sized unit struct with no state of its own to share, and a borrowed
/// `State<'_, LauncherApp>` cannot be moved into a `'static` blocking
/// closure anyway.
pub struct LauncherApp {
    pub install_dir: Mutex<Option<PathBuf>>,
    pub port: Mutex<Option<u16>>,
    /// Shared with the background migration thread spawned by
    /// `start_storage_migration`; `migration_progress` polls this. `None`
    /// until a migration has been started at least once this session.
    pub migration_progress: std::sync::Arc<Mutex<Option<crate::migrate::MigrationProgress>>>,
}

impl Default for LauncherApp {
    fn default() -> Self {
        Self {
            install_dir: Mutex::new(None),
            port: Mutex::new(None),
            migration_progress: std::sync::Arc::new(Mutex::new(None)),
        }
    }
}

/// The one install location this launcher ever writes to or reads from --
/// there is exactly one supported install per machine, so this is a fixed
/// path rather than something the user chooses or the launcher persists
/// elsewhere. `SetupDefaults::for_this_os().install_dir` already resolves to
/// this (`~/.bioflow`); calling it out as a named function makes call sites
/// read as "the install directory" rather than "today's default", which
/// otherwise reads as something a user picked and might differ from.
fn fixed_install_dir() -> PathBuf {
    SetupDefaults::for_this_os().install_dir
}

/// The Compose project name every install this launcher can ever discover
/// uses -- `docker-compose.yml`'s own `name: biopipe` (both the shipped
/// bundle and this repo's own copy pin the same value), so this is not a
/// guess, it is the one name a `biopipe` stack can ever have.
const BIOPIPE_PROJECT_NAME: &str = "biopipe";

/// The fast path: the in-memory value if one is already known, or the
/// fixed `~/.bioflow` path if a real launcher install sits there on disk.
/// Both checks are cheap (a lock, a couple of `stat`s) and were already
/// synchronous before the Docker-discovery fallback below existed --
/// kept as its own function so every caller can still avoid
/// `spawn_blocking` entirely in the common case where this already finds
/// an answer.
fn install_dir_str(app: &State<LauncherApp>) -> Option<String> {
    let existing = app.install_dir.lock().unwrap();
    if let Some(dir) = existing.as_ref() {
        return Some(dir.to_string_lossy().into_owned());
    }
    drop(existing);

    let fixed = fixed_install_dir();
    if setup::install_exists(&fixed) {
        let dir_str = fixed.to_string_lossy().into_owned();
        *app.install_dir.lock().unwrap() = Some(fixed);
        return Some(dir_str);
    }

    None
}

/// The full resolution, including the Docker-discovery fallback: if
/// `install_dir_str` finds nothing, shells out to look for a *running*
/// `biopipe` Compose project anywhere else on the machine.
///
/// This is what lets the launcher recognize a stack started by hand with
/// plain `docker compose up` from a repo checkout (this repo's own
/// documented dev-trunk workflow) -- without it, the launcher reports
/// `NotInstalled` even while a `biopipe` stack is genuinely running,
/// because nothing before this method ever looked anywhere but the fixed
/// path. This is a debug/dev affordance, not a redefinition of
/// "installed": a discovered project is treated identically to a real
/// install for display and Run/Stop purposes, but nothing about this
/// changes which install first-run setup writes to, or verifies that a
/// discovered `.env` has the shape the launcher's own Settings/migration
/// code expects -- those may simply not work correctly against a
/// discovered install, which is an accepted trade for a debug case.
///
/// `async`, unlike `install_dir_str` above: the discovery fallback shells
/// out (`docker compose ls`), so this must run inside `spawn_blocking`
/// like every other Docker-touching call in this file. Callers that
/// already know they're inside a `spawn_blocking` closure with a `docker`
/// value in scope should call `install_dir_str` then
/// `docker.discover_running_project_dir` directly instead of this
/// wrapper, to avoid nesting `spawn_blocking` calls.
async fn install_dir_str_blocking(app: &State<'_, LauncherApp>) -> Option<String> {
    if let Some(dir) = install_dir_str(app) {
        return Some(dir);
    }

    let discovered = tauri::async_runtime::spawn_blocking(|| {
        ShellDocker::new().discover_running_project_dir(BIOPIPE_PROJECT_NAME)
    })
    .await
    .ok()
    .flatten();

    if let Some(dir_str) = &discovered {
        *app.install_dir.lock().unwrap() = Some(PathBuf::from(dir_str));
    }
    discovered
}

/// `async`/`spawn_blocking` for the same reason as `run_stack` above --
/// `evaluate` shells out to `docker` (`probe`, and on the happy path `ps`
/// and `health` too), and the frontend polls this every
/// `STATUS_POLL_INTERVAL_MS` (3s), so even an occasionally slow or
/// momentarily unresponsive daemon would otherwise stall the UI thread on a
/// steady cadence rather than just during Run/Stop/Update.
#[tauri::command]
pub async fn status(app: State<'_, LauncherApp>) -> Result<LauncherStateDto, ()> {
    let install_dir = install_dir_str_blocking(&app).await;

    let state = tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();
        let info = InstallInfo {
            install_dir: install_dir.as_deref(),
        };
        state::evaluate(&docker, &info)
    })
    .await
    .unwrap_or(LauncherState::NotInstalled);

    Ok(state.into())
}

/// One entry per running BioFlow stack that is not the one this launcher
/// manages. Mirrors `docker::OtherStack` at the IPC edge.
#[derive(Serialize)]
pub struct OtherStackDto {
    pub project: String,
    pub slug: String,
    #[serde(rename = "webPort")]
    pub web_port: Option<u16>,
}

/// The worktree stacks running alongside the launcher's own (#320).
///
/// Read-only: this command reports, and offers no way to act. The launcher
/// owns the `biopipe` project; a worktree stack belongs to whoever started
/// it, and `ops/worktree-up.sh --down` from that worktree is how it comes
/// down. What this fixes is that four forgotten stacks wiping each other's
/// test database used to be completely invisible.
///
/// An empty list is the ordinary answer on a machine with one stack, so the
/// UI shows nothing at all in that case rather than an empty section.
/// `async`/`spawn_blocking` for the same reason as every other
/// Docker-touching command.
#[tauri::command]
pub async fn other_stacks() -> Vec<OtherStackDto> {
    tauri::async_runtime::spawn_blocking(|| {
        ShellDocker::new()
            .other_stacks()
            .into_iter()
            .map(|s| OtherStackDto {
                project: s.project,
                slug: s.slug,
                web_port: s.web_port,
            })
            .collect()
    })
    .await
    .unwrap_or_default()
}

/// Whether Docker is installed and its daemon reachable, independent of
/// whether the stack is installed. `status`'s underlying `state::evaluate`
/// short-circuits to `NotInstalled` without ever probing Docker when there
/// is no install directory yet, so the setup wizard has no other way to know
/// Docker's real state while it's showing -- this is a thin, direct probe
/// for exactly that screen. `async`/`spawn_blocking` for the same reason as
/// every other Docker-touching command.
#[tauri::command]
pub async fn docker_ready() -> bool {
    tauri::async_runtime::spawn_blocking(|| {
        ShellDocker::new().probe() == DockerPresence::InstalledDaemonUp
    })
    .await
    .unwrap_or(false)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind")]
pub enum LauncherStateDto {
    NotInstalled,
    DockerUnavailable { installed: bool },
    Stopped,
    Running,
    NodeRunning,
    NodeStopped,
}

impl From<LauncherState> for LauncherStateDto {
    fn from(state: LauncherState) -> Self {
        match state {
            LauncherState::NotInstalled => LauncherStateDto::NotInstalled,
            LauncherState::DockerUnavailable { installed } => {
                LauncherStateDto::DockerUnavailable { installed }
            }
            LauncherState::Stopped => LauncherStateDto::Stopped,
            LauncherState::Running => LauncherStateDto::Running,
            LauncherState::NodeRunning => LauncherStateDto::NodeRunning,
            LauncherState::NodeStopped => LauncherStateDto::NodeStopped,
        }
    }
}

/// How long Run waits for the API healthcheck before giving up -- 30
/// attempts at 2-second intervals is a full minute, generous enough for a
/// cold start against an empty Mongo volume without hanging forever on a
/// stack that's actually broken.
const RUN_HEALTH_MAX_ATTEMPTS: u32 = 30;
const RUN_HEALTH_POLL_INTERVAL: std::time::Duration = std::time::Duration::from_secs(2);

/// Run, then -- only once health has actually passed -- open the system
/// browser at the configured port. Health-gated rather than a fixed sleep,
/// per the spec: a cold start against an empty Mongo volume takes
/// substantially longer than a warm one, and opening too early shows a
/// connection error that reads as a broken install.
///
/// `async` and run on Tauri's blocking-task pool via `spawn_blocking`, the
/// same reason `check_for_update` already needed it: `docker compose up`
/// plus up to a minute of health polling (`RUN_HEALTH_MAX_ATTEMPTS` *
/// `RUN_HEALTH_POLL_INTERVAL`) both block the calling thread for real, and a
/// plain synchronous `#[tauri::command]` dispatches on the same thread that
/// pumps the webview's event loop. A user hit exactly this: the window
/// stopped responding to input for the duration of Run/Stop/Update and the
/// OS offered to force-quit it, even though nothing had actually hung.
#[tauri::command]
pub async fn run_stack(app: tauri::AppHandle, state: State<'_, LauncherApp>) -> Result<(), String> {
    let install_dir = install_dir_str_blocking(&state).await.ok_or("not installed")?;
    // The port the browser handoff opens after a successful Run. In-memory
    // value first (set by run_first_setup / apply_settings), then the
    // install's own .env, then the compose default -- the stack serves
    // whatever WEB_PORT resolves to, and compose defaults to 5173 when .env
    // omits it. In-memory alone is not enough: it starts None every process
    // launch and is only populated by the setup/settings flows, so a
    // hand-created install (this repo's own hand-run workflow) or a plain
    // relaunch would otherwise never hand off to the browser.
    let in_memory_port = *state.port.lock().unwrap();
    let port = in_memory_port
        .or_else(|| web_port_from_env(Path::new(&install_dir)))
        .unwrap_or(DEFAULT_WEB_PORT);

    let outcome = tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();
        actions::run(&docker, &install_dir, RUN_HEALTH_MAX_ATTEMPTS, || {
            std::thread::sleep(RUN_HEALTH_POLL_INTERVAL)
        })
    })
    .await
    .map_err(|e| e.to_string())?;

    match outcome {
        RunOutcome::Running => {
            let _ = app
                .opener()
                .open_url(format!("http://localhost:{port}"), None::<&str>);
            Ok(())
        }
        RunOutcome::ComposeFailed { output } => Err(output),
        RunOutcome::NeverBecameHealthy => {
            Err("the stack started but the API never became healthy".to_string())
        }
    }
}

/// `async`/`spawn_blocking` for the same reason as `run_stack` above --
/// `docker compose down` is a real blocking subprocess call.
#[tauri::command]
pub async fn stop_stack(app: State<'_, LauncherApp>) -> Result<(), String> {
    let install_dir = install_dir_str_blocking(&app).await.ok_or("not installed")?;

    let outcome = tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();
        actions::stop(&docker, &install_dir)
    })
    .await
    .map_err(|e| e.to_string())?;

    match outcome {
        StopOutcome::Stopped => Ok(()),
        StopOutcome::Failed { output } => Err(output),
    }
}

/// `async`/`spawn_blocking` for the same reason as `run_stack` above --
/// `docker compose pull` plus `up -d` are both real blocking subprocess
/// calls, and a pull in particular can run long on a slow connection.
#[tauri::command]
pub async fn update_stack(app: State<'_, LauncherApp>) -> Result<(), String> {
    let install_dir = install_dir_str_blocking(&app).await.ok_or("not installed")?;

    let outcome = tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();
        actions::update(&docker, &install_dir)
    })
    .await
    .map_err(|e| e.to_string())?;

    match outcome {
        UpdateOutcome::Updated => Ok(()),
        UpdateOutcome::PullFailed { output } | UpdateOutcome::RecreateFailed { output } => {
            Err(output)
        }
    }
}

/// Per-OS starting points for the wizard's two editable questions (storage
/// location, port -- always overridable, never a forced choice) plus
/// `install_dir`, which is informational only: the launcher always installs
/// to this fixed path (see `fixed_install_dir`), the wizard just shows it
/// rather than asking.
#[derive(Debug, Clone, Serialize)]
pub struct SetupDefaultsDto {
    pub storage_location: String,
    pub install_dir: String,
    pub port: u16,
}

impl From<SetupDefaults> for SetupDefaultsDto {
    fn from(defaults: SetupDefaults) -> Self {
        Self {
            storage_location: defaults.storage_location.to_string_lossy().into_owned(),
            install_dir: defaults.install_dir.to_string_lossy().into_owned(),
            port: defaults.port,
        }
    }
}

#[tauri::command]
pub fn setup_defaults() -> SetupDefaultsDto {
    SetupDefaults::for_this_os().into()
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind")]
pub enum StoragePathValidationDto {
    Ok,
    NotWritable,
    NotDockerShared,
}

impl From<StoragePathValidation> for StoragePathValidationDto {
    fn from(v: StoragePathValidation) -> Self {
        match v {
            StoragePathValidation::Ok => Self::Ok,
            StoragePathValidation::NotWritable => Self::NotWritable,
            StoragePathValidation::NotDockerShared => Self::NotDockerShared,
        }
    }
}

/// `shared_roots` is empty until the launcher grows a way to read Docker
/// Desktop's actual file-sharing configuration; an empty list means every
/// macOS path outside no explicit root trips the warning, which is the safe
/// (over-cautious) direction per the spec.
#[tauri::command]
pub fn validate_storage(path: String) -> StoragePathValidationDto {
    let home_root = dirs::home_dir().into_iter().collect::<Vec<_>>();
    setup::validate_storage_path(std::path::Path::new(&path), &home_root).into()
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind")]
pub enum PortValidationDto {
    Ok,
    InUse,
}

impl From<PortValidation> for PortValidationDto {
    fn from(v: PortValidation) -> Self {
        match v {
            PortValidation::Ok => Self::Ok,
            PortValidation::InUse => Self::InUse,
        }
    }
}

#[tauri::command]
pub fn validate_setup_port(port: u16) -> PortValidationDto {
    setup::validate_port(port).into()
}

#[derive(Debug, Deserialize)]
pub struct FirstRunSetupArgs {
    pub storage_location: String,
    pub port: u16,
}

/// `async`/`spawn_blocking` for the same reason as `run_stack` above --
/// `setup::install` ends with a `docker compose pull` and `up -d`, the exact
/// button click that first surfaced the frozen-window symptom, since this is
/// the command Install itself triggers.
///
/// The install directory is never a user input here -- only one install is
/// supported per machine, so it is always `fixed_install_dir()`
/// (`~/.bioflow`). A user-chosen install directory was tried first and
/// caused a real bug: the launcher had no way to find that directory again
/// on the *next* launch (nothing persisted it beyond the in-memory
/// `LauncherApp.install_dir`, which starts `None` every process start), so
/// a relaunch with a stack already running showed first-run setup again
/// instead of the running/stopped screen. Fixing the path removes the
/// unknown rather than adding a way to remember an arbitrary one.
#[tauri::command]
pub async fn run_first_setup(
    handle: tauri::AppHandle,
    app: State<'_, LauncherApp>,
    args: FirstRunSetupArgs,
) -> Result<(), String> {
    let bundled_compose_path = handle
        .path()
        .resolve(BUNDLED_COMPOSE_RESOURCE, BaseDirectory::Resource)
        .map_err(|e| format!("could not locate the bundled compose file: {e}"))?;

    let inputs = InstallInputs {
        storage_location: PathBuf::from(args.storage_location),
        install_dir: fixed_install_dir(),
        port: args.port,
    };

    let install_result = {
        let inputs = inputs.clone();
        tauri::async_runtime::spawn_blocking(move || {
            let docker = ShellDocker::new();
            setup::install(&docker, &inputs, &bundled_compose_path)
        })
        .await
        .map_err(|e| e.to_string())?
    };

    install_result.map_err(|e| match e {
        InstallError::CouldNotCreateInstallDir { reason } => {
            format!("could not create the install directory: {reason}")
        }
        InstallError::CouldNotCopyComposeFile { reason } => {
            format!("could not copy the compose file: {reason}")
        }
        InstallError::CouldNotWriteEnv { reason } => format!("could not write .env: {reason}"),
        InstallError::PullFailed { output } => output,
        InstallError::UpFailed { output } => output,
    })?;

    *app.install_dir.lock().unwrap() = Some(inputs.install_dir);
    *app.port.lock().unwrap() = Some(inputs.port);
    Ok(())
}

#[derive(Debug, Deserialize)]
pub struct ApplySettingsArgs {
    pub storage_location: String,
    pub port: u16,
    pub network_exposed: bool,
    /// `None` when the user left the field blank -- no hard cap.
    pub hard_mem_mb: Option<u32>,
    /// The released version the stack is pinned to: `"latest"` for Release,
    /// or a pre-release tag string (`0.3.0-alpha`, ...) the version dropdown
    /// resolved from the registry. Ignored when `developer_repo` is set.
    pub bioflow_tag: String,
    /// When `Some`, switch this install to developer mode: build and run images
    /// locally from this checkout path, ignoring `bioflow_tag`. When `None`,
    /// release mode -- use `bioflow_tag`. Passed as a String over IPC;
    /// `settings::apply` converts it to a `PathBuf`.
    pub developer_repo: Option<String>,
}

/// `async`/`spawn_blocking` for the same reason as `run_stack` above --
/// `settings::apply` ends with a `docker compose up -d` to recreate
/// containers against the rewritten `.env`.
#[tauri::command]
pub async fn apply_settings(app: State<'_, LauncherApp>, args: ApplySettingsArgs) -> Result<(), String> {
    let install_dir = app
        .install_dir
        .lock()
        .unwrap()
        .clone()
        .ok_or("not installed")?;

    let settings = CurrentSettings {
        storage_location: PathBuf::from(args.storage_location),
        port: args.port,
        network_exposed: args.network_exposed,
        hard_mem_mb: args.hard_mem_mb,
        bioflow_tag: args.bioflow_tag,
        developer_repo: args.developer_repo.map(PathBuf::from),
    };

    tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();
        settings::apply(&docker, &install_dir, &settings, &[])
    })
    .await
    .map_err(|e| e.to_string())?
    .map_err(|e| match e {
        SettingsUpdateError::CouldNotWriteEnv { reason } => format!("could not write .env: {reason}"),
        SettingsUpdateError::BuildFailed { output } => output,
        SettingsUpdateError::RecreateFailed { output } => output,
    })?;

    *app.port.lock().unwrap() = Some(args.port);
    Ok(())
}

/// Whether the Update button should appear -- a cheap registry manifest
/// check, never a pull. `async` and run on Tauri's blocking-task pool via
/// `spawn_blocking` so a slow or hung registry (bounded by `GhcrClient`'s own
/// timeout, but a real network call all the same) cannot stall the IPC
/// thread or delay anything else the UI is doing. Failing silently is the
/// point: this returns `false` for "no update to offer" whether that's
/// because the machine is offline or because there is genuinely nothing
/// newer -- per the spec, the UI is not supposed to be able to tell those
/// apart.
#[tauri::command]
pub async fn check_for_update() -> bool {
    tauri::async_runtime::spawn_blocking(|| {
        let registry = GhcrClient::default();
        let local = DockerImageInspector;
        CHECKABLE_IMAGES.iter().any(|image| {
            update_check::update_available(&registry, &local, image, "latest") == Some(true)
        })
    })
    .await
    .unwrap_or(false)
}

/// The optional-tool list for the first-run prefetch screen (task 9,
/// closing #40) -- `GET /pipelines/tools` on the just-started stack,
/// filtered to on-demand tools. Called only after `run_stack` has already
/// confirmed `RunOutcome::Running`, so the API is known reachable by the
/// time this fires; a failure here still degrades to an empty list rather
/// than an error, since a step that offers to skip itself must not become
/// the one thing blocking first-run setup from finishing.
///
/// `async`/`spawn_blocking` for the same reason as every other command here
/// that reaches outside the process: `StackToolsClient::list_tools` is a
/// real (if same-machine) HTTP call, bounded by its own timeout but still
/// not something to run on the IPC thread.
#[tauri::command]
pub async fn fetch_optional_tools(port: u16) -> Vec<OptionalTool> {
    tauri::async_runtime::spawn_blocking(move || {
        let client = StackToolsClient::default();
        client.list_tools(port).unwrap_or_default()
    })
    .await
    .unwrap_or_default()
}

/// Pulls one optional tool's image directly with `docker pull`, bypassing
/// `POST /pipelines/tools/{name}/install` -- that endpoint requires a
/// resolved profile, and none exists yet on a fresh install (profile
/// creation is the web app's own onboarding step, which the launcher has
/// never driven and has no business driving on the user's behalf here). See
/// this module's own top-of-file comment for the full reasoning.
///
/// Errors are returned to the caller rather than swallowed, unlike
/// `fetch_optional_tools` above: listing tools degrading to "offer
/// nothing" is safe, but a user who explicitly checked a box and clicked
/// through needs to know if their multi-gigabyte download actually failed,
/// the same way `run_first_setup`'s own pull failure is surfaced rather
/// than silently ignored.
#[tauri::command]
pub async fn install_optional_tool(image: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();
        match docker.pull_image(&image) {
            ActionResult::Ok => Ok(()),
            ActionResult::Failed { output } => Err(output),
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "phase")]
pub enum MigrationPhaseDto {
    Scanning,
    Copying,
    Validating,
    Removing,
    Complete,
}

impl From<crate::migrate::MigrationPhase> for MigrationPhaseDto {
    fn from(phase: crate::migrate::MigrationPhase) -> Self {
        match phase {
            crate::migrate::MigrationPhase::Scanning => Self::Scanning,
            crate::migrate::MigrationPhase::Copying => Self::Copying,
            crate::migrate::MigrationPhase::Validating => Self::Validating,
            crate::migrate::MigrationPhase::Removing => Self::Removing,
            crate::migrate::MigrationPhase::Complete => Self::Complete,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct MigrationProgressDto {
    pub phase: MigrationPhaseDto,
    pub bytes_copied: u64,
    pub total_bytes: u64,
    pub error: Option<String>,
}

impl From<crate::migrate::MigrationProgress> for MigrationProgressDto {
    fn from(p: crate::migrate::MigrationProgress) -> Self {
        Self {
            phase: p.phase.into(),
            bytes_copied: p.bytes_copied,
            total_bytes: p.total_bytes,
            error: p.error,
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct StartStorageMigrationArgs {
    pub new_location: String,
    pub keep_original: bool,
    pub validate_by_hash: bool,
}

/// Kicks off the migration on a background thread and returns immediately
/// -- unlike every other blocking command in this file, this one is not
/// `async`/`spawn_blocking`-and-await, because the frontend needs to poll
/// `migration_progress` *while* the copy is still running, not receive a
/// single result at the end. The spawned thread writes into
/// `app.migration_progress` as it goes; `finish_storage_migration` (a
/// second command) is what the frontend calls once `migration_progress`
/// reports `Complete`, to perform the `.env` rewrite + stack restart this
/// function deliberately does not do itself (see `run_migration`'s doc
/// comment in migrate.rs on why that split exists).
///
/// Errors from a failed migration are not returned here (the call returns
/// before the migration finishes) -- they are surfaced through
/// `migration_progress`'s stored error field instead. See Step 3 below,
/// which extends `MigrationProgress`'s DTO with an optional error.
#[tauri::command]
pub fn start_storage_migration(app: State<'_, LauncherApp>, args: StartStorageMigrationArgs) -> Result<(), String> {
    let install_dir = app.install_dir.lock().unwrap().clone().ok_or("not installed")?;
    // The current storage location lives in .env, not in LauncherApp's
    // in-memory state (which only tracks install_dir and port) -- read it
    // the same way settings::CurrentSettings would be reconstructed, by
    // parsing .env. A dedicated read here (rather than extending
    // LauncherApp with a third mutex) keeps the source of truth as the
    // file on disk, matching how settings::apply already treats .env as
    // the one thing it writes and nothing else caches.
    let env_path = install_dir.join(".env");
    let env_contents = std::fs::read_to_string(&env_path).map_err(|e| format!("could not read .env: {e}"))?;
    let current_storage = env_contents
        .lines()
        .find_map(|line| line.strip_prefix("BIOINFO_HOME="))
        .ok_or("BIOINFO_HOME not found in .env")?
        .to_string();

    let source = PathBuf::from(current_storage);
    let dest = PathBuf::from(args.new_location);
    let options = crate::migrate::MigrationOptions {
        keep_original: args.keep_original,
        validate_by_hash: args.validate_by_hash,
    };
    let progress_handle = std::sync::Arc::clone(&app.migration_progress);
    *progress_handle.lock().unwrap() = Some(crate::migrate::MigrationProgress::default());

    std::thread::spawn(move || {
        let progress_state = std::sync::Arc::new(std::sync::Mutex::new(crate::migrate::MigrationProgress::default()));

        // Mirror progress_state into the app-visible progress_handle every
        // 250ms while the migration runs, so migration_progress (polled by
        // the frontend) sees live updates rather than only the final
        // state. Stopped by the done flag once run_migration_with_space_check
        // returns, below.
        let done = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let mirror_progress_state = std::sync::Arc::clone(&progress_state);
        let mirror_progress_handle = std::sync::Arc::clone(&progress_handle);
        let mirror_done = std::sync::Arc::clone(&done);
        let mirror_thread = std::thread::spawn(move || {
            while !mirror_done.load(std::sync::atomic::Ordering::Relaxed) {
                let snapshot = mirror_progress_state.lock().unwrap().clone();
                *mirror_progress_handle.lock().unwrap() = Some(snapshot);
                std::thread::sleep(std::time::Duration::from_millis(250));
            }
        });

        let result = crate::migrate::run_migration_with_space_check(
            &source,
            &dest,
            &options,
            &progress_state,
            crate::migrate::available_space_at,
        );

        done.store(true, std::sync::atomic::Ordering::Relaxed);
        let _ = mirror_thread.join();

        let mut final_state = progress_state.lock().unwrap().clone();
        if let Err(e) = &result {
            final_state.error = Some(e.to_string());
        }
        *progress_handle.lock().unwrap() = Some(final_state);
    });

    Ok(())
}

/// Polled by the frontend (see `App.tsx`'s existing `status` polling for
/// the pattern) while a migration is in flight. Returns `None` if no
/// migration has been started yet this session.
#[tauri::command]
pub fn migration_progress(app: State<'_, LauncherApp>) -> Option<MigrationProgressDto> {
    app.migration_progress.lock().unwrap().clone().map(Into::into)
}

#[derive(Debug, Deserialize)]
pub struct FinishStorageMigrationArgs {
    pub new_location: String,
    pub port: u16,
    pub network_exposed: bool,
}

/// Rewrites `.env` to point at the migrated location and restarts the
/// stack -- reuses `settings::apply` unchanged, exactly as a plain
/// Settings repoint already does. Callers (the frontend) must only invoke
/// this after `migration_progress` has reported `phase: Complete` with no
/// `error` -- this command does not re-verify that the migration actually
/// succeeded, since `settings.rs`'s `apply` has no concept of a
/// migration, only of writing `.env` and recreating the stack. See
/// MigrateStorage.tsx (a later task) for where that gating lives.
#[tauri::command]
pub async fn finish_storage_migration(app: State<'_, LauncherApp>, args: FinishStorageMigrationArgs) -> Result<(), String> {
    let install_dir = app.install_dir.lock().unwrap().clone().ok_or("not installed")?;

    // The migration dialog carries no hard-limit control of its own, so this
    // preserves whatever is already on disk rather than clobbering it on
    // every migration -- same reasoning as apply_settings not being the
    // place that introduces a limit change, and the same reasoning that
    // preserves the user's chosen version/tag here rather than resetting it
    // to defaults. Read .env once and parse every field from it, matching
    // how start_storage_migration already treats .env as the source of truth.
    let env_contents =
        std::fs::read_to_string(install_dir.join(".env")).unwrap_or_default();

    let settings = CurrentSettings {
        storage_location: PathBuf::from(args.new_location),
        port: args.port,
        network_exposed: args.network_exposed,
        hard_mem_mb: parse_hard_mem_mb(&env_contents),
        bioflow_tag: parse_bioflow_tag(&env_contents)
            .unwrap_or_else(|| DEFAULT_BIOFLOW_TAG.to_string()),
        developer_repo: parse_developer_repo(&env_contents).map(PathBuf::from),
    };

    tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();
        settings::apply(&docker, &install_dir, &settings, &[])
    })
    .await
    .map_err(|e| e.to_string())?
    .map_err(|e| match e {
        SettingsUpdateError::CouldNotWriteEnv { reason } => format!("could not write .env: {reason}"),
        SettingsUpdateError::BuildFailed { output } => output,
        SettingsUpdateError::RecreateFailed { output } => output,
    })?;

    // Keep LauncherApp's in-memory port in sync with what was just written
    // to .env, matching apply_settings's own behavior above -- the
    // migration dialog does not offer a port change today, but args.port
    // is still an explicit input here, and a caller that resolved this
    // command's own DTO should not silently leave the in-memory value
    // stale relative to the file `apply` just wrote.
    *app.port.lock().unwrap() = Some(args.port);
    Ok(())
}

/// Compose's own fallback when `.env` has no `WEB_PORT` line -- must stay in
/// step with `docker-compose.yml`'s `${WEB_PORT:-5173}`.
const DEFAULT_WEB_PORT: u16 = 5173;

/// Reads `WEB_PORT` out of a `.env` body. Anything unparseable reads as
/// `None`, same rule as `parse_hard_mem_mb`: a hand-edited `.env` must not
/// stop the launcher from opening.
pub(crate) fn parse_web_port(env_contents: &str) -> Option<u16> {
    env_contents
        .lines()
        .find_map(|line| line.strip_prefix("WEB_PORT="))
        .and_then(|value| value.trim().parse().ok())
}

/// Reads `WEB_PORT` from the install directory's `.env` on disk -- the
/// source of truth for the port, the same treatment `current_settings` and
/// `start_storage_migration` already give the hard limit and `BIOINFO_HOME`.
fn web_port_from_env(install_dir: &Path) -> Option<u16> {
    std::fs::read_to_string(install_dir.join(".env"))
        .ok()
        .and_then(|contents| parse_web_port(&contents))
}

/// The default `BIOFLOW_TAG` when `.env` carries no tag line -- the value
/// `render_env` has always hard-coded, and the choice the dropdown resolves
/// "Release" to. Named so every reader of this file agrees on it rather than
/// it being sprinkled as a bare `"latest"` in a few places and drifting.
const DEFAULT_BIOFLOW_TAG: &str = "latest";

/// Reads `BIOFLOW_TAG` out of a `.env` body -- the released version the stack
/// is pinned to. `None` when the line is absent (developer mode, or a
/// hand-written `.env` that omits it): the caller falls back to
/// `DEFAULT_BIOFLOW_TAG`. An empty value also reads as `None`, so a blank
/// line cannot accidentally pin an empty tag. Same hand-edited-`.env`
/// tolerance as `parse_hard_mem_mb`/`parse_web_port`.
pub(crate) fn parse_bioflow_tag(env_contents: &str) -> Option<String> {
    env_contents
        .lines()
        .find_map(|line| line.strip_prefix("BIOFLOW_TAG="))
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

/// Reads `BIOFLOW_DEVELOPER_REPO` out of a `.env` body -- the on-disk flag
/// that developer mode is active, set to the path of a local BioFlow checkout
/// whose images are built into `:local` tags. Absent in release mode. As
/// with the other `.env` parsers, hand-editing is tolerated: a malformed
/// (empty) value reads as `None` rather than panicking the launcher on open.
pub(crate) fn parse_developer_repo(env_contents: &str) -> Option<String> {
    env_contents
        .lines()
        .find_map(|line| line.strip_prefix("BIOFLOW_DEVELOPER_REPO="))
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

/// Reads `BIOFLOW_HARD_MEM_MB` out of a `.env` body.
///
/// Anything unparseable reads as `None` rather than erroring: a hand-edited
/// `.env` must not stop the launcher from opening, and "no hard cap" is the
/// safe reading of a value nobody can interpret.
pub(crate) fn parse_hard_mem_mb(env_contents: &str) -> Option<u32> {
    env_contents
        .lines()
        .find_map(|line| line.strip_prefix("BIOFLOW_HARD_MEM_MB="))
        .and_then(|value| value.trim().parse().ok())
}

#[derive(Debug, Clone, Serialize)]
pub struct CurrentSettingsDto {
    /// `None` when there is no install yet, or `.env` has no hard-limit line.
    pub hard_mem_mb: Option<u32>,
    /// The port the stack serves on, read back from `.env` -- `None` when
    /// there is no install yet or `.env` has no `WEB_PORT` line. The
    /// frontend recovers this on launch so the Open button and port display
    /// stay right for installs the launcher did not create this session.
    pub port: Option<u16>,
    /// The `BIOFLOW_TAG` the stack is pinned to, read from `.env` to round-trip
    /// a relaunch without the dropdown's registry call. Defaults to
    /// `DEFAULT_BIOFLOW_TAG` (`latest`) when `.env` omits the line.
    pub bioflow_tag: String,
    /// `Some(path)` when developer mode is active (`BIOFLOW_DEVELOPER_REPO` is
    /// set in `.env`); `None` in release mode. Lets the Settings dialog open
    /// on the right version-mode radio without waiting on a second parse.
    pub developer_repo: Option<String>,
    /// The storage location (`BIOINFO_HOME`) read back from `.env` --
    /// `None` when there is no install yet or `.env` has no such line. The
    /// frontend recovers this on launch so a relaunch cannot silently
    /// overwrite the install's data directory with an empty placeholder
    /// when the user next presses Apply.
    pub storage_location: Option<String>,
    /// Whether the stack is bound to all interfaces, read back from
    /// `.env`'s `BIND_ADDRESS` -- `None` when there is no install yet or
    /// `.env` has no such line. Same recovery purpose as
    /// `storage_location`: without it, a relaunch would reset the
    /// network-exposure toggle to its default (off) the next time Settings
    /// is applied.
    pub network_exposed: Option<bool>,
}

/// Reads `BIOINFO_HOME` out of a `.env` body -- the storage location.
pub(crate) fn parse_storage_location(env_contents: &str) -> Option<String> {
    env_contents
        .lines()
        .find_map(|line| line.strip_prefix("BIOINFO_HOME="))
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

/// Reads the network-exposure flag out of a `.env` body: the stack is
/// network-exposed when `BIND_ADDRESS` is `0.0.0.0`, loopback otherwise.
/// Anything else (unset, unparseable) reads as `false` -- the safe,
/// locked-down default.
pub(crate) fn parse_network_exposed(env_contents: &str) -> Option<bool> {
    env_contents
        .lines()
        .find_map(|line| line.strip_prefix("BIND_ADDRESS="))
        .map(|value| value.trim() == "0.0.0.0")
}

/// Reads whatever settings can be recovered from `.env` on disk -- the hard
/// memory limit, the port, the storage location, and network exposure, the
/// fields the UI could not otherwise reconstruct on a relaunch. See
/// App.tsx's mount effect for why this exists: without it, a relaunch would
/// leave storage location and network exposure at their placeholder values,
/// and the next Apply would silently rewrite `.env` to an empty data
/// directory and loopback binding.
#[tauri::command]
pub async fn current_settings(app: State<'_, LauncherApp>) -> Result<CurrentSettingsDto, ()> {
    let Some(install_dir) = install_dir_str(&app) else {
        return Ok(CurrentSettingsDto {
            hard_mem_mb: None,
            port: None,
            bioflow_tag: DEFAULT_BIOFLOW_TAG.to_string(),
            developer_repo: None,
            storage_location: None,
            network_exposed: None,
        });
    };

    // One read of the file, four parses -- `.env` is the source of truth for
    // all fields, and a missing/unreadable file is the same as a file with
    // none of the lines: everything reads as `None`.
    let env_contents =
        std::fs::read_to_string(Path::new(&install_dir).join(".env")).unwrap_or_default();

    Ok(CurrentSettingsDto {
        hard_mem_mb: parse_hard_mem_mb(&env_contents),
        port: parse_web_port(&env_contents),
        // Developer mode if BIOFLOW_DEVELOPER_REPO is set (takes precedence);
        // otherwise the release tag, defaulting to `latest` when absent.
        developer_repo: parse_developer_repo(&env_contents),
        bioflow_tag: parse_bioflow_tag(&env_contents)
            .unwrap_or_else(|| DEFAULT_BIOFLOW_TAG.to_string()),
        storage_location: parse_storage_location(&env_contents),
        network_exposed: parse_network_exposed(&env_contents),
    })
}

/// The version choices offered by the Settings dialog's version dropdown:
/// always `release` (resolves to the `:latest` image) plus any alpha/beta
/// stage tag the registry reports. A stage is only included when a published
/// tag exists, so the dropdown can disable it rather than offer a phantom.
/// Fails silently: a registry/network hiccup collapses to Release-only
/// (`VersionOptions::offline`), never an error -- the dropdown must degrade
/// gracefully offline rather than block opening Settings.
///
/// async/spawn_blocking because `GhcrClient::tags_for` makes a real HTTP call
/// to ghcr.io (the same one check_for_update already makes for the Update
/// button, so this shares its cost model).
#[tauri::command]
pub async fn list_version_options() -> VersionOptions {
    tauri::async_runtime::spawn_blocking(|| {
        update_check::list_version_options(&GhcrClient::default(), CHECKABLE_IMAGES[0])
    })
    .await
    .unwrap_or_else(|_| update_check::VersionOptions::offline())
}

/// Rebuilds the locally-built `:local` images and restarts the stack against
/// them, without rewriting `.env` or the override file that already point at
/// them. This is the developer-mode "Rebuild" button: a code edit happened
/// since the last Apply, and the user wants the running containers to pick it
/// up without re-saving settings. async/spawn_blocking so the (potentially
/// long) build does not stall the IPC/UI thread.
#[tauri::command]
pub async fn rebuild_developer(app: State<'_, LauncherApp>) -> Result<(), String> {
    let install_dir = app
        .install_dir
        .lock()
        .unwrap()
        .clone()
        .ok_or("not installed")?;

    tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();
        settings::rebuild_developer(&docker, &install_dir)
    })
    .await
    .map_err(|e| e.to_string())?
    .map_err(|e| match e {
        SettingsUpdateError::BuildFailed { output } => output,
        SettingsUpdateError::CouldNotWriteEnv { reason } => {
            format!("could not write override: {reason}")
        }
        SettingsUpdateError::RecreateFailed { output } => output,
    })
}

// ── #221: Compute-node commands ───────────────────────────────────────

#[derive(Debug, Clone, Serialize)]
pub struct NodeConnectionInfo {
    pub mongo_url: String,
    pub redis_url: String,
    pub api_url: String,
    pub suggested_node_name: String,
}

#[derive(Debug, Deserialize)]
pub struct DiscoverNodeConnectionArgs {
    pub host: String,
    pub port: u16,
}

/// Hits `GET /api/v1/node-connection` on the primary BioFlow's API and
/// returns the auto-discovered connection details (Mongo URL, Redis URL,
/// suggested node name).
#[tauri::command]
pub async fn discover_node_connection(
    args: DiscoverNodeConnectionArgs,
) -> Result<NodeConnectionInfo, String> {
    let url = format!("http://{}:{}/api/v1/nodes/connection-details", args.host, args.port);
    let host_header = format!("{}:{}", args.host, args.port);
    let url_for_err = url.clone();

    let result = tauri::async_runtime::spawn_blocking(move || {
        ureq::get(&url)
            .set("Host", &host_header)
            .timeout(std::time::Duration::from_secs(10))
            .call()
    })
    .await
    .map_err(|e| format!("thread error: {e}"))?
    .map_err(|_e| {
        format!(
            "Could not reach the BioFlow API at {url_for_err}. Is the primary running?"
        )
    })?;

    let body = result
        .into_string()
        .map_err(|e| format!("could not read response: {e}"))?;

    let info: serde_json::Value =
        serde_json::from_str(&body).map_err(|e| format!("invalid response from BioFlow: {e}"))?;

    Ok(NodeConnectionInfo {
        mongo_url: info["mongo_url"].as_str().unwrap_or("").to_string(),
        redis_url: info["redis_url"].as_str().unwrap_or("").to_string(),
        api_url: info["api_url"].as_str().unwrap_or("").to_string(),
        suggested_node_name: info["suggested_node_name"]
            .as_str()
            .unwrap_or("child")
            .to_string(),
    })
}

#[derive(Debug, Deserialize)]
pub struct InstallNodeLocalArgs {
    pub mongo_url: String,
    pub redis_url: String,
    pub api_url: String,
    pub node_name: String,
    pub storage_location: String,
}

/// Installs a compute node on this machine: writes a node `.env`, copies
/// the bundled compose file, pulls the image, and starts only the worker
/// service (`docker compose up -d --no-deps worker`).
#[tauri::command]
pub async fn install_node_local(
    handle: tauri::AppHandle,
    app: State<'_, LauncherApp>,
    args: InstallNodeLocalArgs,
) -> Result<(), String> {
    let bundled_compose_path = handle
        .path()
        .resolve(BUNDLED_COMPOSE_RESOURCE, BaseDirectory::Resource)
        .map_err(|e| format!("could not locate the bundled compose file: {e}"))?;

    let inputs = NodeInstallInputs {
        mongo_url: args.mongo_url,
        redis_url: args.redis_url,
        api_url: args.api_url,
        node_name: args.node_name,
        storage_location: PathBuf::from(args.storage_location),
        install_dir: fixed_install_dir(),
    };

    let install_result = {
        let inputs = inputs.clone();
        tauri::async_runtime::spawn_blocking(move || {
            let docker = ShellDocker::new();
            node_setup::install_node(&docker, &inputs, &bundled_compose_path)
        })
        .await
        .map_err(|e| e.to_string())?
    };

    install_result.map_err(|e| match e {
        NodeInstallError::CouldNotCreateInstallDir { reason } => {
            format!("could not create the install directory: {reason}")
        }
        NodeInstallError::CouldNotCopyComposeFile { reason } => {
            format!("could not copy the compose file: {reason}")
        }
        NodeInstallError::CouldNotWriteEnv { reason } => {
            format!("could not write .env: {reason}")
        }
        NodeInstallError::PullFailed { output } => output,
        NodeInstallError::UpFailed { output } => output,
    })?;

    *app.install_dir.lock().unwrap() = Some(inputs.install_dir);
    Ok(())
}

#[derive(Debug, Deserialize)]
pub struct TestSshArgs {
    pub host: String,
    pub user: String,
    pub password: Option<String>,
    pub port: u16,
}

#[derive(Debug, Clone, Serialize)]
pub struct RemoteInfoDto {
    pub hostname: String,
    pub os_arch: String,
    pub docker_ready: bool,
}

/// Tests SSH connectivity to a remote machine and gathers basic info:
/// hostname, OS/arch, and whether Docker is available.
#[tauri::command]
pub async fn test_ssh_connection(args: TestSshArgs) -> Result<RemoteInfoDto, String> {
    let creds = SshCreds {
        host: args.host,
        user: args.user,
        password: args.password,
        port: args.port,
    };

    let output = tauri::async_runtime::spawn_blocking(move || remote::test_connection(&creds))
        .await
        .map_err(|e| format!("thread error: {e}"))?;

    match output {
        SshResult::Ok(out) => remote::parse_remote_info(&out)
            .map(|info| RemoteInfoDto {
                hostname: info.hostname,
                os_arch: info.os_arch,
                docker_ready: info.docker_ready,
            })
            .ok_or_else(|| format!("unexpected response from remote: {out}")),
        SshResult::Failed { output } => Err(output),
        SshResult::SshpassNotFound => Err(
            "Password authentication requires 'sshpass' to be installed on this machine.\n\n\
             macOS:  brew install sshpass\n\
             Linux:  sudo apt install sshpass / sudo dnf install sshpass"
                .to_string(),
        ),
    }
}

#[derive(Debug, Deserialize)]
pub struct InstallNodeRemoteArgs {
    pub mongo_url: String,
    pub redis_url: String,
    pub api_url: String,
    pub node_name: String,
    pub storage_location: String,
    pub ssh_host: String,
    pub ssh_user: String,
    pub ssh_password: Option<String>,
    pub ssh_port: u16,
}

/// Installs a compute node on a remote machine via SSH: creates the
/// install directory, copies the compose file, writes `.env`, pulls the
/// image, and starts only the worker.
#[tauri::command]
pub async fn install_node_remote(
    handle: tauri::AppHandle,
    args: InstallNodeRemoteArgs,
) -> Result<(), String> {
    let bundled_compose_path = handle
        .path()
        .resolve(BUNDLED_COMPOSE_RESOURCE, BaseDirectory::Resource)
        .map_err(|e| format!("could not locate the bundled compose file: {e}"))?;

    let creds = SshCreds {
        host: args.ssh_host,
        user: args.ssh_user,
        password: args.ssh_password,
        port: args.ssh_port,
    };
    let remote_install_dir = "~/.bioflow";
    let compose_dest = format!("{}/docker-compose.yml", remote_install_dir);

    tauri::async_runtime::spawn_blocking(move || {
        // 1. Create install directory
        match remote::remote_exec(&creds, &format!("mkdir -p {}", remote_install_dir)) {
            SshResult::Ok(_) => {}
            SshResult::Failed { output } => return Err(output),
            SshResult::SshpassNotFound => return Err("sshpass not installed".into()),
        }

        // 2. Copy compose file via scp
        let bundled_path_str = bundled_compose_path.to_string_lossy();
        match remote::remote_copy(&creds, &bundled_path_str, &compose_dest) {
            SshResult::Ok(_) => {}
            SshResult::Failed { output } => return Err(output),
            SshResult::SshpassNotFound => return Err("sshpass not installed".into()),
        }

        // 3. Write .env on remote
        let env_contents = format!(
            "NODE_TYPE=compute\\n\
             MONGO_URL={}\\n\
             REDIS_URL={}\\n\
             WORKER_NODE_ID={}\\n\
             BIOINFO_HOME={}\\n\
             BIOINFO_REGISTER_ROOTS={}\\n\
             BIOFLOW_TAG=latest\\n\
             WORKER_REPLICAS=2\\n",
            args.mongo_url, args.redis_url, args.node_name,
            args.storage_location, args.storage_location
        );
        match remote::remote_exec(
            &creds,
            &format!(
                "printf '%s' '{}' > {}/.env",
                env_contents.replace('\'', "'\\\\''"),
                remote_install_dir
            ),
        ) {
            SshResult::Ok(_) => {}
            SshResult::Failed { output } => return Err(output),
            SshResult::SshpassNotFound => return Err("sshpass not installed".into()),
        }

        // 4. Pull image and start worker
        for cmd in &[
            format!("cd {} && docker compose pull", remote_install_dir),
            format!("cd {} && docker compose up -d --no-deps worker", remote_install_dir),
        ] {
            match remote::remote_exec(&creds, cmd) {
                SshResult::Ok(_) => {}
                SshResult::Failed { output } => return Err(output),
                SshResult::SshpassNotFound => return Err("sshpass not installed".into()),
            }
        }

        Ok(())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Starts only the worker service on an already-installed node.
#[tauri::command]
pub async fn run_node(app: State<'_, LauncherApp>) -> Result<(), String> {
    let install_dir = install_dir_str_blocking(&app)
        .await
        .ok_or("not installed")?;

    let outcome = tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();
        actions::run_node(&docker, &install_dir)
    })
    .await
    .map_err(|e| e.to_string())?;

    match outcome {
        actions::NodeRunOutcome::Running => Ok(()),
        actions::NodeRunOutcome::ComposeFailed { output } => Err(output),
    }
}

/// Stops all containers on a node install (same as `docker compose down`
/// -- works for both full and node installs).
#[tauri::command]
pub async fn stop_node(app: State<'_, LauncherApp>) -> Result<(), String> {
    let install_dir = install_dir_str_blocking(&app)
        .await
        .ok_or("not installed")?;

    let outcome = tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();
        actions::stop(&docker, &install_dir)
    })
    .await
    .map_err(|e| e.to_string())?;

    match outcome {
        StopOutcome::Stopped => Ok(()),
        StopOutcome::Failed { output } => Err(output),
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct NodeStatusDto {
    pub installed: bool,
    pub running: bool,
    /// The node name from `.env` (WORKER_NODE_ID), or None if not a node.
    pub node_name: Option<String>,
    /// The primary API URL derived from .env, or None if not a node.
    pub primary_url: Option<String>,
}

/// Reads the local install to determine whether it's a compute node
/// (NODE_TYPE=compute in .env) and whether the worker is running.
/// The frontend uses this to decide which screen to show and then
/// polls the primary's `GET /api/v1/nodes` for live job stats.
#[tauri::command]
pub async fn node_status(app: State<'_, LauncherApp>) -> Result<NodeStatusDto, ()> {
    let install_dir = install_dir_str_blocking(&app).await;

    let Some(dir) = install_dir else {
        return Ok(NodeStatusDto {
            installed: false,
            running: false,
            node_name: None,
            primary_url: None,
        });
    };

    let status = tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();

        // Check .env for node type
        let env_path = Path::new(&dir).join(".env");
        let env_contents = std::fs::read_to_string(&env_path).ok();
        let is_node = env_contents
            .as_ref()
            .map(|s| s.contains("NODE_TYPE=compute"))
            .unwrap_or(false);

        if !is_node {
            return NodeStatusDto {
                installed: false,
                running: false,
                node_name: None,
                primary_url: None,
            };
        }

        let services = docker.ps(&dir);
        let worker_running = services.iter().any(|s| s.name == "worker" && s.running);

        let node_name = env_contents
            .as_ref()
            .and_then(|s| {
                s.lines()
                    .find_map(|l| l.strip_prefix("WORKER_NODE_ID="))
                    .map(|v| v.to_string())
            });

        let primary_url = env_contents.as_ref().and_then(|s| {
            let mongo = s
                .lines()
                .find_map(|l| l.strip_prefix("MONGO_URL="))?;
            // Extract host:port from mongodb://host:port/...
            let host_port = mongo
                .strip_prefix("mongodb://")?
                .split('/')
                .next()?;
            Some(format!("http://{host_port}"))
        });

        NodeStatusDto {
            installed: true,
            running: worker_running,
            node_name,
            primary_url,
        }
    })
    .await
    .unwrap_or(NodeStatusDto {
        installed: false,
        running: false,
        node_name: None,
        primary_url: None,
    });
    
    Ok(status)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_hard_mem_mb_back_from_env() {
        let env = "BIOINFO_HOME=/data\nWEB_PORT=5173\nBIOFLOW_HARD_MEM_MB=16384\n";
        assert_eq!(parse_hard_mem_mb(env), Some(16384));
    }

    #[test]
    fn absent_hard_mem_reads_as_no_limit() {
        let env = "BIOINFO_HOME=/data\nWEB_PORT=5173\n";
        assert_eq!(parse_hard_mem_mb(env), None);
    }

    #[test]
    fn malformed_hard_mem_reads_as_no_limit() {
        // A hand-edited .env should not stop the launcher from opening.
        let env = "BIOFLOW_HARD_MEM_MB=not-a-number\n";
        assert_eq!(parse_hard_mem_mb(env), None);
    }

    #[test]
    fn round_trips_through_render_env() {
        // Guards against settings::render_env's write format and this
        // module's parse_hard_mem_mb silently drifting apart -- they agree
        // by convention only, with no shared constant tying them together.
        let settings = crate::settings::CurrentSettings {
            storage_location: std::path::PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
            hard_mem_mb: Some(16384),
            bioflow_tag: "latest".to_string(),
            developer_repo: None,
        };
        let env = crate::settings::render_env(&settings, &[]);
        assert_eq!(parse_hard_mem_mb(&env), Some(16384));
    }

    #[test]
    fn reads_web_port_back_from_env() {
        let env = "BIOINFO_HOME=/data\nWEB_PORT=5173\nBIND_ADDRESS=127.0.0.1\n";
        assert_eq!(parse_web_port(env), Some(5173));
    }

    #[test]
    fn absent_web_port_reads_as_none() {
        let env = "BIOINFO_HOME=/data\n";
        assert_eq!(parse_web_port(env), None);
    }

    #[test]
    fn malformed_web_port_reads_as_none() {
        let env = "WEB_PORT=not-a-number\n";
        assert_eq!(parse_web_port(env), None);
    }

    #[test]
    fn web_port_from_env_reads_the_install_env_file() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join(".env"), "BIOINFO_HOME=/data\nWEB_PORT=9000\n")
            .unwrap();
        assert_eq!(web_port_from_env(tmp.path()), Some(9000));
    }

    #[test]
    fn reads_storage_location_back_from_env() {
        let env = "BIOINFO_HOME=/data\nWEB_PORT=5173\n";
        assert_eq!(parse_storage_location(env), Some("/data".to_string()));
    }

    #[test]
    fn absent_storage_location_reads_as_none() {
        let env = "WEB_PORT=5173\n";
        assert_eq!(parse_storage_location(env), None);
    }

    #[test]
    fn empty_storage_location_reads_as_none() {
        // A hand-emptied BIOINFO_HOME= should read as "not present" rather
        // than an empty path the UI would show as a blank field and then
        // apply back to .env.
        let env = "BIOINFO_HOME=\nWEB_PORT=5173\n";
        assert_eq!(parse_storage_location(env), None);
    }

    #[test]
    fn exposed_bind_address_reads_as_network_exposed() {
        let env = "BIND_ADDRESS=0.0.0.0\n";
        assert_eq!(parse_network_exposed(env), Some(true));
    }

    #[test]
    fn loopback_bind_address_reads_as_not_exposed() {
        let env = "BIND_ADDRESS=127.0.0.1\n";
        assert_eq!(parse_network_exposed(env), Some(false));
    }

    #[test]
    fn absent_bind_address_reads_as_none() {
        // None (not Some(false)) so the frontend can tell "locked down by
        // explicit choice" from "unknown" and keep its placeholder default.
        let env = "WEB_PORT=5173\n";
        assert_eq!(parse_network_exposed(env), None);
    }

    #[test]
    fn settings_fields_round_trip_through_render_env() {
        // Every parser here must agree with render_env's write format, or a
        // relaunch would silently lose one Settings dialog feature on Apply.
        let settings = crate::settings::CurrentSettings {
            storage_location: std::path::PathBuf::from("/data"),
            port: 5173,
            network_exposed: true,
            hard_mem_mb: None,
            bioflow_tag: "0.3.0-alpha".to_string(),
            developer_repo: None,
        };
        let env = crate::settings::render_env(&settings, &[]);
        assert_eq!(parse_storage_location(&env), Some("/data".to_string()));
        assert_eq!(parse_network_exposed(&env), Some(true));
        assert_eq!(parse_bioflow_tag(&env), Some("0.3.0-alpha".to_string()));
        assert_eq!(parse_developer_repo(&env), None);
    }
}
