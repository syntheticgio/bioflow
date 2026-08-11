//! The real `DockerBackend`: shells out to the user's own `docker` binary.
//! No Docker API client library, no bundled Docker daemon -- per the design
//! spec's "Actions" section, every action is exactly the command a user would
//! type themselves, with `--project-directory` pointed at the install
//! directory so it never depends on the launcher's own cwd.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;

use super::{ActionResult, DockerBackend, DockerPresence, ServiceStatus};

/// Explicit locations a GUI-launched app cannot reach through PATH. These
/// are the two places a Docker CLI actually lives on most machines, plus
/// Docker Desktop's own bundled CLI (what both symlinks point at).
const DOCKER_CANDIDATES: &[&str] = &[
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
    "/Applications/Docker.app/Contents/Resources/bin/docker",
];

/// The resolved path to the `docker` CLI, cached for the process lifetime.
///
/// Why this exists: a Tauri app launched from Finder on macOS gets a
/// stripped PATH (`/usr/bin:/bin:/usr/sbin:/sbin`) that does not include
/// `/usr/local/bin` or `/opt/homebrew/bin`, so `Command::new("docker")`
/// fails with "No such file or directory" from a GUI-launched app even
/// while Docker Desktop runs fine from a terminal -- which reads to the
/// user as "Docker not installed." Resolve the binary against the known
/// install locations first, then fall back to a PATH lookup, which covers
/// `npm run tauri dev` from a terminal (complete PATH) and Linux distros,
/// where docker lives wherever the package manager put it.
fn docker_binary() -> &'static Path {
    static DOCKER: OnceLock<Option<PathBuf>> = OnceLock::new();
    DOCKER
        .get_or_init(|| resolve_docker(DOCKER_CANDIDATES, std::env::var_os("PATH").as_deref()))
        .as_deref()
        // No docker anywhere: fall back to the bare name so the failure is
        // exactly what it was before (command-not-found -> NotInstalled),
        // rather than a resolved path that reads as if it should exist.
        .unwrap_or_else(|| Path::new("docker"))
}

/// Pure resolution logic, split out so tests can exercise it against
/// fixtures without depending on the host machine's real Docker install.
fn resolve_docker(candidates: &[&str], path_var: Option<&OsStr>) -> Option<PathBuf> {
    candidates
        .iter()
        .map(Path::new)
        .find(|p| p.is_file())
        .map(PathBuf::from)
        .or_else(|| {
            path_var
                .into_iter()
                .flat_map(std::env::split_paths)
                .map(|dir| dir.join("docker"))
                .find(|p| p.is_file())
        })
}

pub struct ShellDocker;

impl ShellDocker {
    pub fn new() -> Self {
        Self
    }

    fn compose(install_dir: &str) -> Command {
        let mut cmd = Command::new(docker_binary());
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
        let binary_exists = Command::new(docker_binary())
            .arg("--version")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);

        if !binary_exists {
            return DockerPresence::NotInstalled;
        }

        let daemon_up = Command::new(docker_binary())
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

    fn up_node(&self, install_dir: &str) -> ActionResult {
        let mut cmd = Self::compose(install_dir);
        cmd.arg("up").arg("-d").arg("--no-deps").arg("worker");
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
        let mut cmd = Command::new(docker_binary());
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

    fn discover_running_project_dir(&self, project_name: &str) -> Option<String> {
        // `docker compose ls` lists every running Compose project on the
        // machine, by name -- exactly what's needed to find a `biopipe`
        // stack wherever it was started from, without depending on
        // `--project-directory` (which is the very thing being looked
        // for). `-a` is deliberately omitted: a stopped project with
        // leftover containers should not be "discovered" as if it were
        // running.
        let output = Command::new(docker_binary())
            .arg("compose")
            .arg("ls")
            .arg("--format")
            .arg("json")
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        let projects: serde_json::Value = serde_json::from_slice(&output.stdout).ok()?;
        let matched = projects.as_array()?.iter().find(|p| {
            p.get("Name").and_then(|n| n.as_str()) == Some(project_name)
        })?;

        // `docker compose ls` reports config file paths but not the
        // working directory those paths were resolved from -- the two
        // differ whenever compose is invoked with `--project-directory`
        // explicitly (as this launcher itself always does), so config
        // file paths are not reliable here. `working_dir` is carried on
        // every container's own compose labels instead, and is exactly
        // the value this launcher needs to pass back into
        // `--project-directory` on every subsequent call.
        let config_files = matched.get("ConfigFiles")?.as_str()?;
        let first_compose_file = config_files.split(',').next()?;

        let inspect_output = Command::new(docker_binary())
            .arg("inspect")
            .arg("--format")
            .arg("{{ index .Config.Labels \"com.docker.compose.project.working_dir\" }}")
            .arg(format!("{project_name}-mongo-1"))
            .output()
            .ok()?;
        if inspect_output.status.success() {
            let working_dir = String::from_utf8_lossy(&inspect_output.stdout).trim().to_string();
            if !working_dir.is_empty() {
                return Some(working_dir);
            }
        }

        // Fall back to the directory containing the first compose file if
        // the label lookup above failed for any reason (e.g. no `mongo`
        // service, an unexpected container naming scheme) -- still better
        // than reporting nothing, since a discovered project with any
        // running container at all should be usable.
        std::path::Path::new(first_compose_file)
            .parent()
            .map(|p| p.to_string_lossy().into_owned())
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsStr;

    #[test]
    fn explicit_candidate_wins_even_without_a_path() {
        let tmp = tempfile::tempdir().unwrap();
        let docker = tmp.path().join("docker");
        std::fs::write(&docker, "").unwrap();
        let resolved = resolve_docker(&[docker.to_str().unwrap()], None);
        assert_eq!(resolved, Some(docker));
    }

    #[test]
    fn candidates_are_checked_in_order_before_the_path() {
        let tmp = tempfile::tempdir().unwrap();
        let first = tmp.path().join("first");
        let second = tmp.path().join("second");
        std::fs::create_dir_all(&first).unwrap();
        std::fs::create_dir_all(&second).unwrap();
        std::fs::write(first.join("docker"), "").unwrap();
        std::fs::write(second.join("docker"), "").unwrap();
        let resolved = resolve_docker(
            &[
                first.join("docker").to_str().unwrap(),
                second.join("docker").to_str().unwrap(),
            ],
            None,
        );
        assert_eq!(resolved, Some(first.join("docker")));
    }

    #[test]
    fn falls_back_to_a_path_lookup_when_no_candidate_matches() {
        let tmp = tempfile::tempdir().unwrap();
        let bindir = tmp.path().join("bin");
        std::fs::create_dir_all(&bindir).unwrap();
        std::fs::write(bindir.join("docker"), "").unwrap();
        let resolved = resolve_docker(&[], Some(OsStr::new(bindir.to_str().unwrap())));
        assert_eq!(resolved, Some(bindir.join("docker")));
    }

    #[test]
    fn nothing_found_anywhere_is_none() {
        let tmp = tempfile::tempdir().unwrap();
        let resolved = resolve_docker(&[], Some(OsStr::new(tmp.path().to_str().unwrap())));
        assert_eq!(resolved, None);
    }
}
