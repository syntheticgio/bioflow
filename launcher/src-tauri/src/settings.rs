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
    /// Kernel-enforced memory ceiling for the worker container, in MB.
    /// `None` means no hard cap -- the default, and what Compose sees as an
    /// unset `mem_limit`. There is deliberately no separate on/off flag: a
    /// toggle plus a number is two controls that can disagree, and the
    /// number alone already expresses off.
    pub hard_mem_mb: Option<u32>,
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

    // Emitted only when a limit is set, so "off" is the variable being
    // absent rather than present-and-empty. Replicas are pinned because
    // mem_limit is per-container: two workers under a 16 GB limit would
    // let the machine reach 32 GB, and the wall would sit at twice the
    // number the user typed.
    if let Some(mb) = settings.hard_mem_mb {
        lines.push(format!("BIOFLOW_HARD_MEM_LIMIT={mb}m"));
        lines.push(format!("BIOFLOW_HARD_MEM_MB={mb}"));
        lines.push("WORKER_REPLICAS=1".to_string());
    }

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

    fn settings_with_hard_mem(hard_mem_mb: Option<u32>) -> CurrentSettings {
        CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
            hard_mem_mb,
        }
    }

    #[test]
    fn default_settings_bind_to_loopback() {
        let settings = CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
            hard_mem_mb: None,
        };
        assert_eq!(settings.bind_address(), "127.0.0.1");
    }

    #[test]
    fn network_exposed_toggle_binds_to_all_interfaces() {
        let settings = CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: true,
            hard_mem_mb: None,
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
            hard_mem_mb: None,
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
            hard_mem_mb: None,
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

    #[test]
    fn blank_hard_limit_writes_no_limit_lines_at_all() {
        // Absence, not an empty assignment. `BIOFLOW_HARD_MEM_LIMIT=` would
        // also read as unlimited to Compose today, but that makes the "off"
        // state depend on Compose's empty-value handling rather than on the
        // variable simply being unset.
        let env = render_env(&settings_with_hard_mem(None), &[]);
        assert!(!env.contains("BIOFLOW_HARD_MEM_LIMIT"));
        assert!(!env.contains("BIOFLOW_HARD_MEM_MB"));
        assert!(!env.contains("WORKER_REPLICAS"));
    }

    #[test]
    fn set_hard_limit_writes_both_vars_and_pins_replicas() {
        let env = render_env(&settings_with_hard_mem(Some(16384)), &[]);
        assert!(env.contains("BIOFLOW_HARD_MEM_LIMIT=16384m"));
        assert!(env.contains("BIOFLOW_HARD_MEM_MB=16384"));
        // mem_limit is per-container; 2 replicas would double the wall.
        assert!(env.contains("WORKER_REPLICAS=1"));
    }

    #[test]
    fn clearing_a_previously_set_limit_removes_every_trace() {
        // The direction that regresses silently: a stale
        // BIOFLOW_HARD_MEM_LIMIT left behind would keep enforcing a limit the
        // user believes they removed. render_env is a full replace, so this
        // asserts the replace is genuinely total.
        let was_set = render_env(&settings_with_hard_mem(Some(8192)), &[]);
        assert!(was_set.contains("BIOFLOW_HARD_MEM_LIMIT=8192m"));

        let now_clear = render_env(&settings_with_hard_mem(None), &[]);
        assert!(!now_clear.contains("BIOFLOW_HARD_MEM_LIMIT"));
        assert!(!now_clear.contains("BIOFLOW_HARD_MEM_MB"));
        assert!(!now_clear.contains("WORKER_REPLICAS"));
    }
}
