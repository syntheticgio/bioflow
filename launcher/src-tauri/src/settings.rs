//! Settings after install: storage location, port, and network exposure are
//! editable from a settings screen, not only at first run. Each rewrites
//! `.env` and recreates the stack -- see the design spec's "Settings after
//! install" and "Network exposure" sections.
//!
//! Changing storage location does not move existing data; the UI states
//! that at the point of change (see `launcher/src/Settings.tsx`), not here --
//! this module only ever writes what it is told.

use std::path::{Path, PathBuf};

use crate::docker::{ActionResult, DockerBackend};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CurrentSettings {
    pub storage_location: PathBuf,
    pub port: u16,
    /// Whether the stack is reachable from other devices on the network.
    /// Defaults to `false` (bound to loopback) -- the toggle in the UI is
    /// framed as turning exposure *on*, never as turning safety on, so the
    /// locked-down state is always the one a user does not have to find.
    pub network_exposed: bool,
}

impl CurrentSettings {
    fn bind_address(&self) -> &'static str {
        if self.network_exposed {
            "0.0.0.0"
        } else {
            "127.0.0.1"
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SettingsUpdateError {
    CouldNotWriteEnv { reason: String },
    RecreateFailed { output: String },
}

/// Rewrites `.env` in the install directory and recreates the stack so the
/// change takes effect. `env_extra` carries any `.env` lines this module
/// does not itself know about (e.g. infra variables from first-run setup)
/// so a settings change never drops them -- rewriting `.env` here is a full
/// replace, not a patch, and losing an unrelated line would be a silent
/// regression the user has no way to notice.
pub fn apply<D: DockerBackend>(
    docker: &D,
    install_dir: &Path,
    settings: &CurrentSettings,
    env_extra: &[(String, String)],
) -> Result<(), SettingsUpdateError> {
    let env_contents = render_env(settings, env_extra);
    let env_path = install_dir.join(".env");
    std::fs::write(&env_path, env_contents)
        .map_err(|e| SettingsUpdateError::CouldNotWriteEnv { reason: e.to_string() })?;

    let install_dir_str = install_dir.to_string_lossy();
    match docker.up(&install_dir_str) {
        ActionResult::Ok => Ok(()),
        ActionResult::Failed { output } => Err(SettingsUpdateError::RecreateFailed { output }),
    }
}

fn render_env(settings: &CurrentSettings, env_extra: &[(String, String)]) -> String {
    let mut lines = vec![
        format!("BIOINFO_HOME={}", settings.storage_location.display()),
        format!("WEB_PORT={}", settings.port),
        format!("BIND_ADDRESS={}", settings.bind_address()),
        "BIOFLOW_TAG=latest".to_string(),
    ];
    for (key, value) in env_extra {
        lines.push(format!("{key}={value}"));
    }
    lines.push(String::new());
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::docker::FakeDocker;

    #[test]
    fn default_settings_bind_to_loopback() {
        let settings = CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
        };
        assert_eq!(settings.bind_address(), "127.0.0.1");
    }

    #[test]
    fn network_exposed_toggle_binds_to_all_interfaces() {
        let settings = CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: true,
        };
        assert_eq!(settings.bind_address(), "0.0.0.0");
    }

    #[test]
    fn apply_rewrites_env_and_preserves_extra_lines() {
        let tmp = tempfile::tempdir().unwrap();
        let docker = FakeDocker::new();
        let settings = CurrentSettings {
            storage_location: PathBuf::from("/new/data"),
            port: 9000,
            network_exposed: true,
        };
        let extra = vec![("MONGO_URL".to_string(), "mongodb://mongo:27017".to_string())];

        apply(&docker, tmp.path(), &settings, &extra).unwrap();

        let env = std::fs::read_to_string(tmp.path().join(".env")).unwrap();
        assert!(env.contains("BIOINFO_HOME=/new/data"));
        assert!(env.contains("WEB_PORT=9000"));
        assert!(env.contains("BIND_ADDRESS=0.0.0.0"));
        assert!(env.contains("MONGO_URL=mongodb://mongo:27017"));
    }

    #[test]
    fn a_failed_recreate_is_reported_with_output() {
        let tmp = tempfile::tempdir().unwrap();
        let docker = FakeDocker::new();
        *docker.up_result.borrow_mut() = ActionResult::Failed {
            output: "port in use".to_string(),
        };
        let settings = CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
        };

        let result = apply(&docker, tmp.path(), &settings, &[]);

        assert_eq!(
            result,
            Err(SettingsUpdateError::RecreateFailed {
                output: "port in use".to_string()
            })
        );
        // .env was still written even though the recreate failed -- the
        // setting itself took, only starting the stack with it didn't.
        assert!(tmp.path().join(".env").exists());
    }
}
