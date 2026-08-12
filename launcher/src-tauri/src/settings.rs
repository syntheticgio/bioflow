//! Settings after install: storage location, port, network exposure, and image
//! version are editable from a settings screen, not only at first run. Each
//! rewrites `.env` and recreates the stack -- see the design spec's "Settings
//! after install" and "Network exposure" sections (and the version-switch
//! design spec for the version/developer cases).
//!
//! Changing storage location does not move existing data; the UI states
//! that at the point of change (see `launcher/src/Settings.tsx`), not here --
//! this module only ever writes what it is told.

use std::path::{Path, PathBuf};

use crate::docker::{ActionResult, DockerBackend};

/// The compose override the launcher writes (only in developer mode) to
/// point api, worker, and web at locally-built `:local` images instead of
/// registry tags. Compose auto-loads `docker-compose.override.yml` from the
/// project directory, which is why this exact name is required -- anything
/// else would need a `-f` flag on every compose call, and the shipped docker
/// compose invocation (see `docker::ShellDocker::compose`) takes no such flag.
pub const DEVELOPER_OVERRIDE_FILE: &str = "docker-compose.override.yml";

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
    /// The `BIOFLOW_TAG` the stack runs. `"latest"` (the default, matching
    /// the old hard-coded value in `render_env`) points Release at the most
    /// recent production image; a pre-release stage tag (`0.3.0-alpha`,
    /// `0.4.0-beta`, ...) points the stack at that published tag. Ignored
    /// when `developer_repo` is set -- see the field below.
    pub bioflow_tag: String,
    /// When `Some`, the stack is pointed at images built locally from this
    /// repository path instead of registry tags: `bioflow_tag` is dropped and
    /// a `docker-compose.override.yml` (see `DEVELOPER_OVERRIDE_FILE`)
    /// rewrites api/worker/web to `:local` images with `build:` stanzas, and
    /// `.env` records `BIOFLOW_DEVELOPER_REPO=<path>` instead of
    /// `BIOFLOW_TAG=...`. Only one of the two is ever present on disk.
    pub developer_repo: Option<PathBuf>,
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
    /// Developer mode only: the local `docker compose build` failed. Surfaced
    /// separately from RecreateFailed so a compile error in the user's source
    /// reads as "build failed", not as "the stack won't start".
    BuildFailed { output: String },
    RecreateFailed { output: String },
}

/// Rewrites `.env` in the install directory and recreates the stack so the
/// change takes effect. `env_extra` carries any `.env` lines this module
/// does not itself know about (e.g. infra variables from first-run setup)
/// so a settings change never drops them -- rewriting `.env` here is a full
/// replace, not a patch, and losing an unrelated line would be a silent
/// regression the user has no way to notice.
///
/// In developer mode (`settings.developer_repo` set) this additionally
/// writes the compose override that points api/worker/web at `:local` images
/// and runs `docker compose build` before `up`, so the freshly built images
/// are the ones started. A build failure is reported as
/// `SettingsUpdateError::BuildFailed`, distinct from a failed `up`. A
/// switch back to release mode removes a previously-written override so no
/// stale `:local` stanza lingers.
pub fn apply<D: DockerBackend>(
    docker: &D,
    install_dir: &Path,
    settings: &CurrentSettings,
    env_extra: &[(String, String)],
) -> Result<(), SettingsUpdateError> {
    // Render the override file (if any) and the .env first: both are plain
    // file writes, and a failed build/up should still leave the setting
    // itself on disk ("it took, the stack just won't compile/run").
    write_override(install_dir, settings)?;
    let env_contents = render_env(settings, env_extra);
    let env_path = install_dir.join(".env");
    std::fs::write(&env_path, env_contents)
        .map_err(|e| SettingsUpdateError::CouldNotWriteEnv { reason: e.to_string() })?;

    let install_dir_str = install_dir.to_string_lossy();
    if settings.developer_repo.is_some() {
        // Locally-built :local images must exist before `up` can start a
        // container off them; build them here, and report a build failure
        // distinctly rather than as a downstream "image not found".
        match docker.build(&install_dir_str) {
            ActionResult::Ok => {}
            ActionResult::Failed { output } => {
                return Err(SettingsUpdateError::BuildFailed { output });
            }
        }
    }
    match docker.up(&install_dir_str) {
        ActionResult::Ok => Ok(()),
        ActionResult::Failed { output } => Err(SettingsUpdateError::RecreateFailed { output }),
    }
}

/// Developer mode only: rebuilds the locally-built `:local` images and
/// restarts the stack, reusing the `docker-compose.override.yml` that
/// `apply` already wrote. `up` runs after `build` so the freshly built
/// images are the ones started. This is the "Rebuild" button in Settings:
/// a code edit happened since the last apply, and the user wants the running
/// containers to pick it up without re-saving settings.
pub fn rebuild_developer<D: DockerBackend>(
    docker: &D,
    install_dir: &Path,
) -> Result<(), SettingsUpdateError> {
    let install_dir_str = install_dir.to_string_lossy();
    match docker.build(&install_dir_str) {
        ActionResult::Ok => {}
        ActionResult::Failed { output } => {
            return Err(SettingsUpdateError::BuildFailed { output });
        }
    }
    match docker.up(&install_dir_str) {
        ActionResult::Ok => Ok(()),
        ActionResult::Failed { output } => Err(SettingsUpdateError::RecreateFailed { output }),
    }
}

/// (Re)writes or removes the developer-mode compose override, depending on
/// whether `settings.developer_repo` is set. A release-mode setting leaves no
/// override file behind -- so switching back from developer does not keep an
/// old `:local` build stanza pointing at a path the user abandoned.
fn write_override(
    install_dir: &Path,
    settings: &CurrentSettings,
) -> Result<(), SettingsUpdateError> {
    let override_path = install_dir.join(DEVELOPER_OVERRIDE_FILE);
    match &settings.developer_repo {
        Some(repo) => {
            std::fs::write(override_path, render_developer_override(repo))
                .map_err(|e| SettingsUpdateError::CouldNotWriteEnv { reason: e.to_string() })?;
        }
        None => {
            // Ignore a "file not found" -- it just means we were already in
            // release mode, which is the state we're leaving things in.
            let _ = std::fs::remove_file(&override_path);
        }
    }
    Ok(())
}

/// Renders the `docker-compose.override.yml` that points api, worker, and web
/// at locally-built `:local` images instead of registry tags. The three
/// services share two images (api/worker -> bioflow-backend, web ->
/// bioflow-web), and `build:` lets `docker compose build` construct them from
/// the user's repository -- the same `backend/Dockerfile` and
/// `frontend/Dockerfile` shipped to production, so the local build is an
/// apples-to-apples stand-in for the published image.
pub fn render_developer_override(repo: &Path) -> String {
    let repo = repo.to_string_lossy();
    let lines: Vec<String> = vec![
        "# Auto-generated by the launcher Settings dialog (developer mode).".to_string(),
        "# Compose auto-loads this file alongside docker-compose.yml; it points".to_string(),
        "# api, worker, and web at locally-built :local images instead of".to_string(),
        "# registry tags, built from this machine checkout.".to_string(),
        "services:".to_string(),
        "  api:".to_string(),
        "    image: ghcr.io/syntheticgio/bioflow-backend:local".to_string(),
        "    build:".to_string(),
        format!("      context: {repo}"),
        "      dockerfile: backend/Dockerfile".to_string(),
        "  worker:".to_string(),
        "    image: ghcr.io/syntheticgio/bioflow-backend:local".to_string(),
        "    build:".to_string(),
        format!("      context: {repo}"),
        "      dockerfile: backend/Dockerfile".to_string(),
        "  web:".to_string(),
        "    image: ghcr.io/syntheticgio/bioflow-web:local".to_string(),
        "    build:".to_string(),
        format!("      context: {repo}"),
        "      dockerfile: frontend/Dockerfile".to_string(),
        String::new(),
    ];
    lines.join("\n")
}

pub(crate) fn render_env(settings: &CurrentSettings, env_extra: &[(String, String)]) -> String {
    let mut lines = vec![
        format!("BIOINFO_HOME={}", settings.storage_location.display()),
        format!("WEB_PORT={}", settings.port),
        format!("BIND_ADDRESS={}", settings.bind_address()),
    ];

    // Release mode pins a registry tag; developer mode drops BIOFLOW_TAG
    // entirely and instead records the local repo under BIOFLOW_DEVELOPER_REPO
    // -- only one of the two is ever present, so render_env never emits both.
    if let Some(repo) = &settings.developer_repo {
        lines.push(format!("BIOFLOW_DEVELOPER_REPO={}", repo.display()));
    } else {
        lines.push(format!("BIOFLOW_TAG={}", settings.bioflow_tag));
    }

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
            bioflow_tag: "latest".to_string(),
            developer_repo: None,
        }
    }

    #[test]
    fn default_settings_bind_to_loopback() {
        let settings = CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
            hard_mem_mb: None,
            bioflow_tag: "latest".to_string(),
            developer_repo: None,
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
            bioflow_tag: "latest".to_string(),
            developer_repo: None,
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
            bioflow_tag: "0.3.0-alpha".to_string(),
            developer_repo: None,
        };
        let extra = vec![("MONGO_URL".to_string(), "mongodb://mongo:27017".to_string())];

        apply(&docker, tmp.path(), &settings, &extra).unwrap();

        let env = std::fs::read_to_string(tmp.path().join(".env")).unwrap();
        assert!(env.contains("BIOINFO_HOME=/new/data"));
        assert!(env.contains("WEB_PORT=9000"));
        assert!(env.contains("BIND_ADDRESS=0.0.0.0"));
        assert!(env.contains("BIOFLOW_TAG=0.3.0-alpha"));
        assert!(!env.contains("BIOFLOW_DEVELOPER_REPO"));
        assert!(env.contains("MONGO_URL=mongodb://mongo:27017"));
        // Release mode writes no override file.
        assert!(!tmp.path().join(DEVELOPER_OVERRIDE_FILE).exists());
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
            bioflow_tag: "latest".to_string(),
            developer_repo: None,
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
    fn developer_mode_writes_override_and_builds_and_ups() {
        let tmp = tempfile::tempdir().unwrap();
        let docker = FakeDocker::new();
        // build and up both succeed -- the happy path for a developer
        // switching on local images.
        *docker.build_result.borrow_mut() = ActionResult::Ok;
        *docker.up_result.borrow_mut() = ActionResult::Ok;

        let settings = CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
            hard_mem_mb: None,
            bioflow_tag: "latest".to_string(),
            developer_repo: Some(PathBuf::from("/Users/example/bioflow")),
        };

        let result = apply(&docker, tmp.path(), &settings, &[]);
        assert!(result.is_ok(), "developer apply failed: {result:?}");

        let env = std::fs::read_to_string(tmp.path().join(".env")).unwrap();
        assert!(env.contains("BIOFLOW_DEVELOPER_REPO=/Users/example/bioflow"));
        // Release tag is not written in developer mode.
        assert!(!env.contains("BIOFLOW_TAG"));

        let override_file =
            std::fs::read_to_string(tmp.path().join(DEVELOPER_OVERRIDE_FILE)).unwrap();
        assert!(override_file.contains("ghcr.io/syntheticgio/bioflow-backend:local"));
        assert!(override_file.contains("ghcr.io/syntheticgio/bioflow-web:local"));
        assert!(override_file.contains("context: /Users/example/bioflow"));
        assert!(override_file.contains("dockerfile: backend/Dockerfile"));
        assert!(override_file.contains("dockerfile: frontend/Dockerfile"));
    }

    #[test]
    fn developer_mode_build_failure_is_reported_distinctly() {
        let tmp = tempfile::tempdir().unwrap();
        let docker = FakeDocker::new();
        *docker.build_result.borrow_mut() = ActionResult::Failed {
            output: "Dockerfile parse error".to_string(),
        };

        let settings = CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
            hard_mem_mb: None,
            bioflow_tag: "latest".to_string(),
            developer_repo: Some(PathBuf::from("/Users/example/bioflow")),
        };

        let result = apply(&docker, tmp.path(), &settings, &[]);

        assert_eq!(
            result,
            Err(SettingsUpdateError::BuildFailed {
                output: "Dockerfile parse error".to_string()
            })
        );
        // The override + .env were still written even though build failed.
        assert!(tmp.path().join(DEVELOPER_OVERRIDE_FILE).exists());
        let env = std::fs::read_to_string(tmp.path().join(".env")).unwrap();
        assert!(env.contains("BIOFLOW_DEVELOPER_REPO=/Users/example/bioflow"));
    }

    #[test]
    fn leaving_developer_mode_removes_a_stale_override() {
        // Switching back from developer -> release must not leave the
        // override file behind, else a stale `:local` build stanza lingers
        // pointing at a repo the user abandoned.
        let tmp = tempfile::tempdir().unwrap();
        let docker = FakeDocker::new();

        // First, developer mode: writes the override.
        let dev = CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
            hard_mem_mb: None,
            bioflow_tag: "latest".to_string(),
            developer_repo: Some(PathBuf::from("/Users/example/bioflow")),
        };
        apply(&docker, tmp.path(), &dev, &[]).unwrap();
        assert!(tmp.path().join(DEVELOPER_OVERRIDE_FILE).exists());

        // Then release mode: no build is needed/called, override removed,
        // tag written.
        *docker.build_result.borrow_mut() = ActionResult::Failed {
            output: "build must not run in release mode".to_string(),
        };
        let release = CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
            hard_mem_mb: None,
            bioflow_tag: "0.2.6".to_string(),
            developer_repo: None,
        };
        apply(&docker, tmp.path(), &release, &[]).unwrap();
        assert!(!tmp.path().join(DEVELOPER_OVERRIDE_FILE).exists());
        let env = std::fs::read_to_string(tmp.path().join(".env")).unwrap();
        assert!(env.contains("BIOFLOW_TAG=0.2.6"));
        assert!(!env.contains("BIOFLOW_DEVELOPER_REPO"));
    }

    #[test]
    fn render_env_writes_tag_for_release_and_repo_for_developer() {
        let release = CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
            hard_mem_mb: None,
            bioflow_tag: "0.3.0-alpha".to_string(),
            developer_repo: None,
        };
        let env = render_env(&release, &[]);
        assert!(env.contains("BIOFLOW_TAG=0.3.0-alpha"));
        assert!(!env.contains("BIOFLOW_DEVELOPER_REPO"));

        let dev = CurrentSettings {
            storage_location: PathBuf::from("/data"),
            port: 5173,
            network_exposed: false,
            hard_mem_mb: None,
            bioflow_tag: "latest".to_string(),
            developer_repo: Some(PathBuf::from("/src/bioflow")),
        };
        let env = render_env(&dev, &[]);
        assert!(env.contains("BIOFLOW_DEVELOPER_REPO=/src/bioflow"));
        assert!(!env.contains("BIOFLOW_TAG"));
    }

    #[test]
    fn render_developer_override_points_all_services_at_local_images() {
        let yaml = render_developer_override(Path::new("/src/bioflow"));
        // api and worker share the backend image; web is separate.
        assert_eq!(yaml.matches("bioflow-backend:local").count(), 2);
        assert_eq!(yaml.matches("bioflow-web:local").count(), 1);
        assert!(yaml.contains("context: /src/bioflow"));
        assert!(yaml.contains("dockerfile: backend/Dockerfile"));
        assert!(yaml.contains("dockerfile: frontend/Dockerfile"));
        assert!(yaml.contains("services:"));
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
        // And a release-mode default renders as the latest tag.
        assert!(env.contains("BIOFLOW_TAG=latest"));
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
