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
use crate::docker::ShellDocker;
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
pub struct LauncherApp {
    pub docker: ShellDocker,
    pub install_dir: Mutex<Option<PathBuf>>,
    pub port: Mutex<Option<u16>>,
}

impl Default for LauncherApp {
    fn default() -> Self {
        Self {
            docker: ShellDocker::new(),
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

#[tauri::command]
pub fn status(app: State<LauncherApp>) -> LauncherStateDto {
    let install_dir = app.install_dir.lock().unwrap();
    let info = InstallInfo {
        install_dir: install_dir.as_ref().and_then(|p| p.to_str()),
    };
    state::evaluate(&app.docker, &info).into()
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
#[tauri::command]
pub fn run_stack(app: tauri::AppHandle, state: State<LauncherApp>) -> Result<(), String> {
    let install_dir = install_dir_str(&state).ok_or("not installed")?;
    match actions::run(&state.docker, &install_dir, RUN_HEALTH_MAX_ATTEMPTS, || {
        std::thread::sleep(RUN_HEALTH_POLL_INTERVAL)
    }) {
        RunOutcome::Running => {
            if let Some(port) = *state.port.lock().unwrap() {
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

#[tauri::command]
pub fn stop_stack(app: State<LauncherApp>) -> Result<(), String> {
    let install_dir = install_dir_str(&app).ok_or("not installed")?;
    match actions::stop(&app.docker, &install_dir) {
        StopOutcome::Stopped => Ok(()),
        StopOutcome::Failed { output } => Err(output),
    }
}

#[tauri::command]
pub fn update_stack(app: State<LauncherApp>) -> Result<(), String> {
    let install_dir = install_dir_str(&app).ok_or("not installed")?;
    match actions::update(&app.docker, &install_dir) {
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

#[tauri::command]
pub fn run_first_setup(
    handle: tauri::AppHandle,
    app: State<LauncherApp>,
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

    setup::install(&app.docker, &inputs, &bundled_compose_path).map_err(|e| match e {
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

#[tauri::command]
pub fn apply_settings(app: State<LauncherApp>, args: ApplySettingsArgs) -> Result<(), String> {
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

    settings::apply(&app.docker, &install_dir, &settings, &[]).map_err(|e| match e {
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
