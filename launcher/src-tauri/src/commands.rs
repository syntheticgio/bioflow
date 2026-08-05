//! Tauri commands: the thin IPC surface the UI calls. Each command
//! delegates straight to the tested logic in `state`, `actions`, `setup`,
//! and `settings` -- nothing here should ever need its own test, since
//! everything it does is already covered where the real logic lives.

use std::path::PathBuf;
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::path::BaseDirectory;
use tauri::{Manager, State};
use tauri_plugin_opener::OpenerExt;

use crate::actions::{self, RunOutcome, StopOutcome, UpdateOutcome};
use crate::docker::{DockerBackend, DockerPresence, ShellDocker};
use crate::settings::{self, CurrentSettings, SettingsUpdateError};
use crate::setup::{self, InstallError, InstallInputs, PortValidation, SetupDefaults, StoragePathValidation};
use crate::state::{self, InstallInfo, LauncherState};
use crate::update_check::{self, DockerImageInspector, GhcrClient};

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
}

impl Default for LauncherApp {
    fn default() -> Self {
        Self {
            install_dir: Mutex::new(None),
            port: Mutex::new(None),
        }
    }
}

fn install_dir_str(app: &State<LauncherApp>) -> Option<String> {
    app.install_dir
        .lock()
        .unwrap()
        .as_ref()
        .map(|p| p.to_string_lossy().into_owned())
}

/// `async`/`spawn_blocking` for the same reason as `run_stack` above --
/// `evaluate` shells out to `docker` (`probe`, and on the happy path `ps`
/// and `health` too), and the frontend polls this every
/// `STATUS_POLL_INTERVAL_MS` (3s), so even an occasionally slow or
/// momentarily unresponsive daemon would otherwise stall the UI thread on a
/// steady cadence rather than just during Run/Stop/Update.
#[tauri::command]
pub async fn status(app: State<'_, LauncherApp>) -> Result<LauncherStateDto, ()> {
    let install_dir = install_dir_str(&app);

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
    let install_dir = install_dir_str(&state).ok_or("not installed")?;
    let port = *state.port.lock().unwrap();

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
            if let Some(port) = port {
                let _ = app
                    .opener()
                    .open_url(format!("http://localhost:{port}"), None::<&str>);
            }
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
    let install_dir = install_dir_str(&app).ok_or("not installed")?;

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
    let install_dir = install_dir_str(&app).ok_or("not installed")?;

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

/// Per-OS starting points for the three first-run questions -- always
/// overridable in the wizard, never a forced choice.
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
    pub install_dir: String,
    pub port: u16,
}

/// `async`/`spawn_blocking` for the same reason as `run_stack` above --
/// `setup::install` ends with a `docker compose pull` and `up -d`, the exact
/// button click that first surfaced the frozen-window symptom, since this is
/// the command Install itself triggers.
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
        install_dir: PathBuf::from(&args.install_dir),
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
    };

    tauri::async_runtime::spawn_blocking(move || {
        let docker = ShellDocker::new();
        settings::apply(&docker, &install_dir, &settings, &[])
    })
    .await
    .map_err(|e| e.to_string())?
    .map_err(|e| match e {
        SettingsUpdateError::CouldNotWriteEnv { reason } => format!("could not write .env: {reason}"),
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
