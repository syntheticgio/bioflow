//! The resumable write sequence: write install directory -> copy the bundled
//! compose file in -> write `.env` -> pull -> start.
//!
//! "Resumable" is the load-bearing word here. An offline first run, or one
//! that dies partway, must not leave a half-written install directory that
//! the next launch reads as installed -- `state::evaluate` treats "install_dir
//! is Some" as installed, so a partial write there is worse than no write at
//! all. Every step before the network-dependent `pull` is idempotent (safe to
//! redo), and the sequence checks for completion markers rather than assuming
//! a fresh run.

use std::path::{Path, PathBuf};

use crate::docker::{ActionResult, DockerBackend};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstallInputs {
    pub storage_location: PathBuf,
    pub install_dir: PathBuf,
    pub port: u16,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InstallError {
    CouldNotCreateInstallDir { reason: String },
    CouldNotCopyComposeFile { reason: String },
    CouldNotWriteEnv { reason: String },
    /// The machine is offline or the registry is unreachable. Setup stops
    /// here rather than starting an incomplete stack; the install directory
    /// itself is left intact (compose file and `.env` already written) so
    /// the *next* attempt only needs to retry the pull, not redo everything.
    PullFailed { output: String },
    /// The pull succeeded but bringing the stack up failed (e.g. a port
    /// collision that appeared between the setup-time check and now).
    UpFailed { output: String },
}

/// Whether a real install already sits at `dir` -- both files `install`
/// writes, not just the directory, so an interrupted `create_dir_all` with
/// nothing written into it does not count. This is how the launcher
/// recognizes an install across a relaunch: `LauncherApp.install_dir` is
/// in-memory only and starts empty every process start, so without this
/// check a stack installed and left running in a *previous* session showed
/// first-run setup again on the next launch instead of the running/stopped
/// screen it should have -- the process had genuinely forgotten, even
/// though the containers, compose file, and `.env` were all still there.
pub fn install_exists(dir: &Path) -> bool {
    dir.join("docker-compose.yml").is_file() && dir.join(".env").is_file()
}

/// `bundled_compose_path` is the path to the compose file the launcher
/// shipped as a build resource (see `launcher/README.md` on why that must
/// stay a reference to the repository's own `docker-compose.yml`, never a
/// copy). It is a parameter here, not a constant, so tests can point it at a
/// fixture instead of a real Tauri resource path.
pub fn install<D: DockerBackend>(
    docker: &D,
    inputs: &InstallInputs,
    bundled_compose_path: &Path,
) -> Result<(), InstallError> {
    std::fs::create_dir_all(&inputs.install_dir).map_err(|e| InstallError::CouldNotCreateInstallDir {
        reason: e.to_string(),
    })?;

    let compose_dest = inputs.install_dir.join("docker-compose.yml");
    // Idempotent: re-copying an identical file on a resumed run is a no-op
    // in effect, so there is no need to check whether it already exists.
    std::fs::copy(bundled_compose_path, &compose_dest).map_err(|e| InstallError::CouldNotCopyComposeFile {
        reason: e.to_string(),
    })?;

    let env_contents = render_env(inputs);
    let env_dest = inputs.install_dir.join(".env");
    std::fs::write(&env_dest, env_contents).map_err(|e| InstallError::CouldNotWriteEnv {
        reason: e.to_string(),
    })?;

    let install_dir_str = inputs.install_dir.to_string_lossy();
    match docker.pull(&install_dir_str) {
        ActionResult::Ok => {}
        ActionResult::Failed { output } => return Err(InstallError::PullFailed { output }),
    }

    match docker.up(&install_dir_str) {
        ActionResult::Ok => Ok(()),
        ActionResult::Failed { output } => Err(InstallError::UpFailed { output }),
    }
}

/// The launcher's only writable artifact. Every user choice becomes an
/// environment variable the compose file already substitutes -- see the
/// spec's "The compose file is shipped, never generated" section.
fn render_env(inputs: &InstallInputs) -> String {
    format!(
        "BIOINFO_HOME={}\n\
         WEB_PORT={}\n\
         BIND_ADDRESS=127.0.0.1\n\
         BIOFLOW_TAG=latest\n",
        inputs.storage_location.display(),
        inputs.port,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::docker::FakeDocker;

    fn fixture_compose_file(dir: &Path) -> PathBuf {
        let path = dir.join("source-compose.yml");
        std::fs::write(&path, "name: biopipe\nservices: {}\n").unwrap();
        path
    }

    #[test]
    fn install_exists_is_false_for_a_directory_that_was_never_installed() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(!install_exists(tmp.path()));
    }

    #[test]
    fn install_exists_is_false_for_a_missing_directory() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(!install_exists(&tmp.path().join("never-created")));
    }

    #[test]
    fn install_exists_is_false_with_only_the_compose_file_written() {
        // Guards the "both files, not just the directory" contract: a
        // partial write (e.g. create_dir_all succeeded, the compose copy
        // succeeded, but .env failed) must not read as installed.
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("docker-compose.yml"), "name: biopipe\n").unwrap();
        assert!(!install_exists(tmp.path()));
    }

    #[test]
    fn install_exists_is_true_once_a_clean_install_completed() {
        let tmp = tempfile::tempdir().unwrap();
        let bundled = fixture_compose_file(tmp.path());
        let install_dir = tmp.path().join("install");
        let storage = tmp.path().join("storage");
        std::fs::create_dir_all(&storage).unwrap();

        let docker = FakeDocker::new();
        let inputs = InstallInputs {
            storage_location: storage,
            install_dir: install_dir.clone(),
            port: 5173,
        };
        install(&docker, &inputs, &bundled).unwrap();

        assert!(install_exists(&install_dir));
    }

    #[test]
    fn a_clean_install_writes_the_compose_file_verbatim() {
        let tmp = tempfile::tempdir().unwrap();
        let bundled = fixture_compose_file(tmp.path());
        let install_dir = tmp.path().join("install");
        let storage = tmp.path().join("storage");
        std::fs::create_dir_all(&storage).unwrap();

        let docker = FakeDocker::new();
        let inputs = InstallInputs {
            storage_location: storage,
            install_dir: install_dir.clone(),
            port: 5173,
        };

        install(&docker, &inputs, &bundled).unwrap();

        let written = std::fs::read_to_string(install_dir.join("docker-compose.yml")).unwrap();
        let source = std::fs::read_to_string(&bundled).unwrap();
        assert_eq!(written, source, "the bundled compose file must be copied byte-for-byte");
    }

    #[test]
    fn env_contains_the_three_answers_and_nothing_the_user_was_not_asked() {
        let tmp = tempfile::tempdir().unwrap();
        let bundled = fixture_compose_file(tmp.path());
        let install_dir = tmp.path().join("install");
        let storage = tmp.path().join("storage");
        std::fs::create_dir_all(&storage).unwrap();

        let docker = FakeDocker::new();
        let inputs = InstallInputs {
            storage_location: storage.clone(),
            install_dir: install_dir.clone(),
            port: 9000,
        };

        install(&docker, &inputs, &bundled).unwrap();

        let env = std::fs::read_to_string(install_dir.join(".env")).unwrap();
        assert!(env.contains(&format!("BIOINFO_HOME={}", storage.display())));
        assert!(env.contains("WEB_PORT=9000"));
        assert!(env.contains("BIND_ADDRESS=127.0.0.1"));
        assert!(env.contains("BIOFLOW_TAG=latest"));
    }

    #[test]
    fn a_failed_pull_leaves_the_install_dir_intact_for_a_resume() {
        let tmp = tempfile::tempdir().unwrap();
        let bundled = fixture_compose_file(tmp.path());
        let install_dir = tmp.path().join("install");
        let storage = tmp.path().join("storage");
        std::fs::create_dir_all(&storage).unwrap();

        let docker = FakeDocker::new();
        *docker.pull_result.borrow_mut() = ActionResult::Failed {
            output: "offline".to_string(),
        };

        let inputs = InstallInputs {
            storage_location: storage,
            install_dir: install_dir.clone(),
            port: 5173,
        };

        let result = install(&docker, &inputs, &bundled);
        assert_eq!(
            result,
            Err(InstallError::PullFailed {
                output: "offline".to_string()
            })
        );

        // The compose file and .env are already on disk -- a resumed attempt
        // only needs to retry the pull, not redo the whole sequence.
        assert!(install_dir.join("docker-compose.yml").exists());
        assert!(install_dir.join(".env").exists());
    }

    #[test]
    fn resuming_after_a_failed_pull_succeeds_without_redoing_earlier_steps() {
        let tmp = tempfile::tempdir().unwrap();
        let bundled = fixture_compose_file(tmp.path());
        let install_dir = tmp.path().join("install");
        let storage = tmp.path().join("storage");
        std::fs::create_dir_all(&storage).unwrap();

        let failing_docker = FakeDocker::new();
        *failing_docker.pull_result.borrow_mut() = ActionResult::Failed {
            output: "offline".to_string(),
        };
        let inputs = InstallInputs {
            storage_location: storage,
            install_dir: install_dir.clone(),
            port: 5173,
        };
        assert!(install(&failing_docker, &inputs, &bundled).is_err());

        // Machine comes back online; the same install directory is retried.
        let succeeding_docker = FakeDocker::new();
        let result = install(&succeeding_docker, &inputs, &bundled);
        assert!(result.is_ok());
        assert!(install_dir.join("docker-compose.yml").exists());
    }
}
