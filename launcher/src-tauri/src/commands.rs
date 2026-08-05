//! Tauri commands: the thin IPC surface the UI calls. Each command
//! delegates straight to the tested logic in `state`, `actions`, `setup`,
//! and `settings` -- nothing here should ever need its own test, since
//! everything it does is already covered where the real logic lives.

use std::path::PathBuf;
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::State;
use tauri_plugin_opener::OpenerExt;

use crate::actions::{self, RunOutcome, StopOutcome, UpdateOutcome};
use crate::docker::ShellDocker;
use crate::settings::{self, CurrentSettings, SettingsUpdateError};
use crate::setup::{self, InstallError, InstallInputs};
use crate::state::{self, InstallInfo, LauncherState};

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

#[derive(Debug, Deserialize)]
pub struct FirstRunSetupArgs {
    pub storage_location: String,
    pub install_dir: String,
    pub port: u16,
}

#[tauri::command]
pub fn run_first_setup(
    app: State<LauncherApp>,
    args: FirstRunSetupArgs,
    bundled_compose_path: String,
) -> Result<(), String> {
    let inputs = InstallInputs {
        storage_location: PathBuf::from(args.storage_location),
        install_dir: PathBuf::from(&args.install_dir),
        port: args.port,
    };

    setup::install(&app.docker, &inputs, &PathBuf::from(bundled_compose_path)).map_err(
        |e| match e {
            InstallError::CouldNotCreateInstallDir { reason } => {
                format!("could not create the install directory: {reason}")
            }
            InstallError::CouldNotCopyComposeFile { reason } => {
                format!("could not copy the compose file: {reason}")
            }
            InstallError::CouldNotWriteEnv { reason } => format!("could not write .env: {reason}"),
            InstallError::PullFailed { output } => output,
            InstallError::UpFailed { output } => output,
        },
    )?;

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
