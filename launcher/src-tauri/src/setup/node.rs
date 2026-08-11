//! Compute-node install: writes a node `.env` and starts only the worker
//! (no API, no web UI, no local mongo/redis). The worker connects to the
//! primary BioFlow instance's Mongo and Redis via the externally-routable
//! URLs returned by `GET /api/v1/node-connection`.

use std::path::{Path, PathBuf};

use crate::docker::{ActionResult, DockerBackend};

/// The node install writes the same two files as a full install
/// (`docker-compose.yml` + `.env`), so `install_exists` from the parent
/// module covers both. The `.env` contents are what distinguish a node
/// from a full stack.
pub use super::install::install_exists;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NodeInstallInputs {
    /// The primary's externally-routable Mongo URL (from the auto-discovery
    /// endpoint — internal Docker hostnames already rewritten).
    pub mongo_url: String,
    /// The primary's externally-routable Redis URL.
    pub redis_url: String,
    /// The primary's API URL, for live status polling after connection.
    pub api_url: String,
    /// Human-readable node name shown in the primary's node list. Defaults
    /// to the suggestion from the auto-discovery endpoint; the user can
    /// override it.
    pub node_name: String,
    /// Where data (pipeline working directories, downloaded references,
    /// scratch space) lives on this machine.
    pub storage_location: PathBuf,
    /// The fixed install directory (`~/.bioflow`), same as a full install.
    pub install_dir: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NodeInstallError {
    CouldNotCreateInstallDir { reason: String },
    CouldNotCopyComposeFile { reason: String },
    CouldNotWriteEnv { reason: String },
    PullFailed { output: String },
    UpFailed { output: String },
}

/// Install a compute node: create the install directory, copy the bundled
/// compose file in, write a node `.env`, pull the image, and start only the
/// worker (no mongo, redis, api, or web — those live on the primary).
///
/// `bundled_compose_path` is the path to the compose file the launcher
/// shipped as a build resource (same as the full install path).
pub fn install_node<D: DockerBackend>(
    docker: &D,
    inputs: &NodeInstallInputs,
    bundled_compose_path: &Path,
) -> Result<(), NodeInstallError> {
    std::fs::create_dir_all(&inputs.install_dir).map_err(|e| {
        NodeInstallError::CouldNotCreateInstallDir {
            reason: e.to_string(),
        }
    })?;

    let compose_dest = inputs.install_dir.join("docker-compose.yml");
    std::fs::copy(bundled_compose_path, &compose_dest).map_err(|e| {
        NodeInstallError::CouldNotCopyComposeFile {
            reason: e.to_string(),
        }
    })?;

    let env_contents = render_node_env(inputs);
    let env_dest = inputs.install_dir.join(".env");
    std::fs::write(&env_dest, env_contents).map_err(|e| {
        NodeInstallError::CouldNotWriteEnv {
            reason: e.to_string(),
        }
    })?;

    let install_dir_str = inputs.install_dir.to_string_lossy();

    // Pull only the backend image (worker uses the same image as api).
    // We can't `docker compose pull` with `--no-deps` because pull doesn't
    // support it — just pull the image directly.
    match docker.pull(&install_dir_str) {
        ActionResult::Ok => {}
        ActionResult::Failed { output } => {
            return Err(NodeInstallError::PullFailed { output });
        }
    }

    match docker.up_node(&install_dir_str) {
        ActionResult::Ok => Ok(()),
        ActionResult::Failed { output } => Err(NodeInstallError::UpFailed { output }),
    }
}

/// The node `.env` — minimal, only what the worker needs to connect to the
/// primary and operate.
fn render_node_env(inputs: &NodeInstallInputs) -> String {
    format!(
        "NODE_TYPE=compute\n\
         MONGO_URL={}\n\
         REDIS_URL={}\n\
         WORKER_NODE_ID={}\n\
         BIOINFO_HOME={}\n\
         BIOINFO_REGISTER_ROOTS={}\n\
         BIOFLOW_TAG=latest\n\
         WORKER_REPLICAS=2\n",
        inputs.mongo_url,
        inputs.redis_url,
        inputs.node_name,
        inputs.storage_location.display(),
        inputs.storage_location.display(),
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

    fn test_inputs(install_dir: PathBuf, storage: PathBuf) -> NodeInstallInputs {
        NodeInstallInputs {
            mongo_url: "mongodb://192.168.1.50:27017/biopipe?replicaSet=rs0&directConnection=true".into(),
            redis_url: "redis://192.168.1.50:6379/0".into(),
            api_url: "http://192.168.1.50:8000".into(),
            node_name: "test-node".into(),
            storage_location: storage,
            install_dir,
        }
    }

    #[test]
    fn node_env_contains_connection_urls() {
        let env = render_node_env(&test_inputs(
            PathBuf::from("/tmp/install"),
            PathBuf::from("/tmp/data"),
        ));
        assert!(env.contains("MONGO_URL=mongodb://192.168.1.50:27017"));
        assert!(env.contains("REDIS_URL=redis://192.168.1.50:6379/0"));
        assert!(env.contains("WORKER_NODE_ID=test-node"));
        assert!(env.contains("NODE_TYPE=compute"));
    }

    #[test]
    fn node_env_includes_storage_location() {
        let env = render_node_env(&test_inputs(
            PathBuf::from("/tmp/install"),
            PathBuf::from("/big/raid"),
        ));
        assert!(env.contains("BIOINFO_HOME=/big/raid"));
    }

    #[test]
    fn node_install_writes_both_files() {
        let tmp = tempfile::tempdir().unwrap();
        let bundled = fixture_compose_file(tmp.path());
        let install_dir = tmp.path().join("install");
        let storage = tmp.path().join("storage");
        std::fs::create_dir_all(&storage).unwrap();

        let docker = FakeDocker::new();
        let inputs = test_inputs(install_dir.clone(), storage);

        install_node(&docker, &inputs, &bundled).unwrap();

        assert!(install_dir.join("docker-compose.yml").exists());
        assert!(install_dir.join(".env").exists());
        // install_exists covers nodes too.
        assert!(install_exists(&install_dir));
    }

    #[test]
    fn node_env_does_not_contain_web_port_or_bind_address() {
        // A node has no web UI — nothing that binds to a port the user
        // would navigate to. These fields exist only in full-stack .env.
        let env = render_node_env(&test_inputs(
            PathBuf::from("/tmp/install"),
            PathBuf::from("/tmp/data"),
        ));
        assert!(!env.contains("WEB_PORT"));
        assert!(!env.contains("BIND_ADDRESS"));
    }
}
