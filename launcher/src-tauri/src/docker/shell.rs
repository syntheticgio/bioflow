//! The real `DockerBackend`: shells out to the user's own `docker` binary.
//! No Docker API client library, no bundled Docker daemon -- per the design
//! spec's "Actions" section, every action is exactly the command a user would
//! type themselves, with `--project-directory` pointed at the install
//! directory so it never depends on the launcher's own cwd.

use std::process::Command;

use super::{ActionResult, DockerBackend, DockerPresence, ServiceStatus};

pub struct ShellDocker;

impl ShellDocker {
    pub fn new() -> Self {
        Self
    }

    fn compose(install_dir: &str) -> Command {
        let mut cmd = Command::new("docker");
        cmd.arg("compose").arg("--project-directory").arg(install_dir);
        cmd
    }

    fn run_action(mut cmd: Command) -> ActionResult {
        match cmd.output() {
            Ok(output) if output.status.success() => ActionResult::Ok,
            Ok(output) => {
                let mut combined = String::from_utf8_lossy(&output.stdout).into_owned();
                combined.push_str(&String::from_utf8_lossy(&output.stderr));
                ActionResult::Failed { output: combined }
            }
            Err(err) => ActionResult::Failed {
                output: err.to_string(),
            },
        }
    }
}

impl Default for ShellDocker {
    fn default() -> Self {
        Self::new()
    }
}

impl DockerBackend for ShellDocker {
    fn probe(&self) -> DockerPresence {
        let binary_exists = Command::new("docker")
            .arg("--version")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);

        if !binary_exists {
            return DockerPresence::NotInstalled;
        }

        let daemon_up = Command::new("docker")
            .arg("info")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);

        if daemon_up {
            DockerPresence::InstalledDaemonUp
        } else {
            DockerPresence::InstalledDaemonDown
        }
    }

    fn up(&self, install_dir: &str) -> ActionResult {
        let mut cmd = Self::compose(install_dir);
        cmd.arg("up").arg("-d");
        Self::run_action(cmd)
    }

    fn down(&self, install_dir: &str) -> ActionResult {
        let mut cmd = Self::compose(install_dir);
        cmd.arg("down");
        Self::run_action(cmd)
    }

    fn ps(&self, install_dir: &str) -> Vec<ServiceStatus> {
        let mut cmd = Self::compose(install_dir);
        cmd.arg("ps").arg("--format").arg("json");
        let output = match cmd.output() {
            Ok(output) if output.status.success() => output,
            _ => return Vec::new(),
        };
        let text = String::from_utf8_lossy(&output.stdout);
        // `docker compose ps --format json` emits one JSON object per line,
        // not a JSON array.
        text.lines()
            .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
            .filter_map(|value| {
                let name = value.get("Service")?.as_str()?.to_string();
                let state = value.get("State")?.as_str()?.to_string();
                Some(ServiceStatus {
                    name,
                    running: state == "running",
                })
            })
            .collect()
    }

    fn pull(&self, install_dir: &str) -> ActionResult {
        let mut cmd = Self::compose(install_dir);
        cmd.arg("pull");
        Self::run_action(cmd)
    }

    fn pull_image(&self, image: &str) -> ActionResult {
        let mut cmd = Command::new("docker");
        cmd.arg("pull").arg(image);
        Self::run_action(cmd)
    }

    fn health(&self, install_dir: &str) -> bool {
        // The api service's own healthcheck already probes /healthz (see
        // docker-compose.yml); reading compose's view of that healthcheck
        // status avoids the launcher needing its own HTTP client and port
        // knowledge duplicated from .env.
        let mut cmd = Self::compose(install_dir);
        cmd.arg("ps").arg("api").arg("--format").arg("json");
        let output = match cmd.output() {
            Ok(output) if output.status.success() => output,
            _ => return false,
        };
        let text = String::from_utf8_lossy(&output.stdout);
        text.lines()
            .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
            .any(|value| {
                value
                    .get("Health")
                    .and_then(|h| h.as_str())
                    .map(|h| h == "healthy")
                    .unwrap_or(false)
            })
    }

    fn manifest_digest_differs(&self, _install_dir: &str) -> Option<bool> {
        // Deferred to Phase 6: requires a real published registry to compare
        // against (blocked on #37). Returning None means "no update to
        // offer," which is the safe default -- the check is non-blocking and
        // fails silently per the spec, never an error.
        None
    }

    fn attempt_daemon_start(&self) {
        #[cfg(target_os = "macos")]
        {
            let _ = Command::new("open").arg("-a").arg("Docker").spawn();
        }
        #[cfg(target_os = "linux")]
        {
            let _ = Command::new("systemctl")
                .arg("--user")
                .arg("start")
                .arg("docker")
                .spawn();
        }
        #[cfg(target_os = "windows")]
        {
            let _ = Command::new("cmd")
                .args(["/C", "start", "", "Docker Desktop"])
                .spawn();
        }
    }
}
