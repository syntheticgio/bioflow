//! The real `DockerBackend`: shells out to the user's own `docker` binary.
//! No Docker API client library, no bundled Docker daemon -- per the design
//! spec's "Actions" section, every action is exactly the command a user would
//! type themselves, with `--project-directory` pointed at the install
//! directory so it never depends on the launcher's own cwd.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;

use super::{ActionResult, DockerBackend, DockerPresence, OtherStack, ServiceStatus};

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

/// Whether a candidate install directory is a git worktree, and therefore
/// must not be adopted as the launcher's install directory.
///
/// A worktree is where `--show-toplevel` differs from the parent of
/// `--git-common-dir`: the common dir is the shared `.git` of the whole
/// repository, so its parent is the main working tree even when the probe
/// runs from a worktree (whose own `.git` is a file, not a directory). This
/// is the same test `ops/worktree-up.sh` and
/// `ops/hooks/block-compose-in-worktree.sh` already use.
///
/// `NotAGitRepository` is deliberately its own variant rather than folded in
/// with the main checkout: it is the *shipped install* case (`~/.bioflow`
/// holds a compose file and an `.env`, no git anywhere), which is the most
/// common real answer and must stay discoverable.
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
enum GitCheckout {
    MainCheckout,
    Worktree,
    NotAGitRepository,
}

/// Pure classification, split out from the `git` invocations so tests can
/// exercise it against fixtures without depending on the host machine's real
/// git state -- the same split `resolve_docker` above already uses.
///
/// Both arguments are the raw stdout of the two `git rev-parse` calls, or
/// `None` where the call failed (which is what a non-repository directory
/// produces, since `rev-parse` exits non-zero outside a repository).
fn classify_checkout(toplevel: Option<&str>, git_common_dir: Option<&str>) -> GitCheckout {
    let (Some(toplevel), Some(common)) = (toplevel, git_common_dir) else {
        return GitCheckout::NotAGitRepository;
    };

    let toplevel = Path::new(toplevel.trim());
    let common = Path::new(common.trim());

    // The parent of the shared `.git` is the main working tree. A bare
    // repository has no parent to compare against; treat that as not-a-
    // checkout rather than guessing, since nothing the launcher wants to
    // adopt lives in one.
    let Some(main_root) = common.parent() else {
        return GitCheckout::NotAGitRepository;
    };

    if toplevel == main_root {
        GitCheckout::MainCheckout
    } else {
        GitCheckout::Worktree
    }
}

/// The Compose project name the launcher itself manages. Anything else
/// beginning `biopipe-wt-` is a worktree stack (#320).
const WORKTREE_PROJECT_PREFIX: &str = "biopipe-wt-";

/// Turns `docker ps` label output into the set of other running stacks.
///
/// Pure, so tests can exercise the parsing and de-duplication against
/// fixtures without a Docker daemon -- the same split `resolve_docker` and
/// `classify_checkout` above already use.
///
/// `rows` is one entry per running container: `(project, service, port)`,
/// where `port` is the host port that container publishes, if any.
///
/// A stack has several containers, so rows collapse by project. The `web`
/// container is the one whose port matters (it is what a human opens), and
/// the others contribute nothing but their existence.
fn collect_other_stacks(rows: &[(String, String, Option<u16>)]) -> Vec<OtherStack> {
    let mut stacks: Vec<OtherStack> = Vec::new();

    for (project, service, port) in rows {
        let Some(slug) = project.strip_prefix(WORKTREE_PROJECT_PREFIX) else {
            // The launcher's own `biopipe`, or something unrelated that
            // happens to be running. Neither belongs in this list.
            continue;
        };
        if slug.is_empty() {
            continue;
        }

        let existing = stacks.iter_mut().find(|s| &s.project == project);
        let entry = match existing {
            Some(entry) => entry,
            None => {
                stacks.push(OtherStack {
                    project: project.clone(),
                    slug: slug.to_string(),
                    web_port: None,
                });
                stacks.last_mut().expect("just pushed")
            }
        };

        // Only `web`'s port is meaningful here. Taking any service's port
        // would show the user an api or mongo port and invite them to open
        // it in a browser.
        if service == "web" && entry.web_port.is_none() {
            entry.web_port = *port;
        }
    }

    // Stable, predictable ordering. The list is read at a glance and its
    // order should not shuffle between polls as Docker reorders containers.
    stacks.sort_by(|a, b| a.project.cmp(&b.project));
    stacks
}

/// Extracts the host port from a `HostConfig.PortBindings` JSON blob.
///
/// Read from `HostConfig` rather than the runtime `NetworkSettings.Ports` or
/// `docker ps`'s `{{.Ports}}`, because those are empty for any container that
/// is not currently running -- including one in a restart loop, which is
/// exactly the state a stack the user most needs to know about can be in.
fn host_port_from_bindings(json: &str) -> Option<u16> {
    let parsed: serde_json::Value = serde_json::from_str(json).ok()?;
    parsed
        .as_object()?
        .values()
        .filter_map(|bindings| bindings.as_array())
        .flatten()
        .filter_map(|b| b.get("HostPort")?.as_str()?.parse::<u16>().ok())
        .next()
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

    /// Runs the two `git rev-parse` calls against `dir` and classifies the
    /// result. A missing `git` binary, or any failure, reads as
    /// `NotAGitRepository` -- which keeps a machine without git working
    /// exactly as it did before this check existed.
    fn classify_dir(dir: &str) -> GitCheckout {
        let rev_parse = |args: &[&str]| -> Option<String> {
            let output = Command::new("git")
                .arg("-C")
                .arg(dir)
                .args(args)
                .output()
                .ok()?;
            if !output.status.success() {
                return None;
            }
            Some(String::from_utf8_lossy(&output.stdout).into_owned())
        };

        let toplevel = rev_parse(&["rev-parse", "--show-toplevel"]);
        let common = rev_parse(&["rev-parse", "--path-format=absolute", "--git-common-dir"]);
        classify_checkout(toplevel.as_deref(), common.as_deref())
    }

    /// The host port a container publishes, read from `HostConfig` so that a
    /// container which is not currently running (or is restart-looping) still
    /// reports the port it is configured for.
    fn web_host_port(&self, container: &str) -> Option<u16> {
        let output = Command::new(docker_binary())
            .arg("inspect")
            .arg("--format")
            .arg("{{json .HostConfig.PortBindings}}")
            .arg(container)
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        host_port_from_bindings(String::from_utf8_lossy(&output.stdout).trim())
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

    fn build(&self, install_dir: &str) -> ActionResult {
        // `docker compose build` builds every service that declares a `build:`
        // stanza. The shipped docker-compose.yml declares none, so in normal
        // operation this is a no-op; the developer-mode override
        // (docker-compose.override.yml) is what adds `build:` to api/worker/web.
        let mut cmd = Self::compose(install_dir);
        cmd.arg("build");
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
        let discovered = if inspect_output.status.success() {
            let working_dir = String::from_utf8_lossy(&inspect_output.stdout)
                .trim()
                .to_string();
            if working_dir.is_empty() {
                None
            } else {
                Some(working_dir)
            }
        } else {
            None
        };

        // Fall back to the directory containing the first compose file if
        // the label lookup above failed for any reason (e.g. no `mongo`
        // service, an unexpected container naming scheme) -- still better
        // than reporting nothing, since a discovered project with any
        // running container at all should be usable.
        let discovered = discovered.or_else(|| {
            std::path::Path::new(first_compose_file)
                .parent()
                .map(|p| p.to_string_lossy().into_owned())
        })?;

        // Refuse a git worktree, whichever of the two routes above produced
        // it. `docker-compose.yml` pins `name: biopipe` and the override's
        // bind mounts are relative, so `docker compose up` from a worktree
        // recreates *the* stack pointing there -- at which point this
        // discovery would hand the launcher a worktree as its install
        // directory, and every later `--project-directory` call would
        // address it. `ops/hooks/block-compose-in-worktree.sh` makes that
        // repoint unlikely but does not seal it: the guard steps aside for
        // an explicit `-p biopipe`, and it only hooks agent-issued Bash, not
        // a human's own terminal.
        //
        // Adoption is worse than the stale-code problem the repoint already
        // causes. A worktree has no `.env` at all -- `.env` is gitignored,
        // which is exactly why `ops/worktree-up.sh` passes
        // `--env-file <main checkout>/.env` -- so Settings and storage
        // migration would be reading and writing against a directory that is
        // not an install and has nothing for them to read.
        //
        // Returning `None` reports NotInstalled, which is already this
        // method's answer when discovery finds nothing. That is confusing
        // while a stack is visibly running, but silence beats adopting the
        // wrong directory: the stack keeps running either way, and the fix
        // (rebuild from the main checkout) is documented in CLAUDE.md.
        match Self::classify_dir(&discovered) {
            // The shipped-install case: `~/.bioflow` has no git anywhere.
            // Must stay discoverable -- it is the common real answer.
            GitCheckout::NotAGitRepository => Some(discovered),
            // The documented debug/dev case this method exists for.
            GitCheckout::MainCheckout => Some(discovered),
            GitCheckout::Worktree => None,
        }
    }

    fn other_stacks(&self) -> Vec<OtherStack> {
        // One row per running container, carrying its Compose project and
        // service labels. `docker ps` rather than `docker compose ls`: the
        // project listing gives a name and a status but no service breakdown
        // and no ports, and the port is the part that makes an entry
        // actionable.
        //
        // No `-a`: a stopped worktree stack costs nothing and is not what the
        // user needs warning about.
        let output = Command::new(docker_binary())
            .arg("ps")
            .arg("--filter")
            .arg("label=com.docker.compose.project")
            .arg("--format")
            .arg("{{.Names}}\t{{.Label \"com.docker.compose.project\"}}\t{{.Label \"com.docker.compose.service\"}}")
            .output();

        let Ok(output) = output else {
            return Vec::new();
        };
        if !output.status.success() {
            return Vec::new();
        }

        let text = String::from_utf8_lossy(&output.stdout);
        let rows: Vec<(String, String, Option<u16>)> = text
            .lines()
            .filter_map(|line| {
                let mut parts = line.split('\t');
                let name = parts.next()?.trim().to_string();
                let project = parts.next()?.trim().to_string();
                let service = parts.next()?.trim().to_string();
                if project.is_empty() {
                    return None;
                }

                // Only `web` needs a port lookup, and each one is a separate
                // `docker inspect`; skipping the rest keeps this to one extra
                // call per stack rather than one per container.
                let port = if service == "web" {
                    self.web_host_port(&name)
                } else {
                    None
                };
                Some((project, service, port))
            })
            .collect();

        collect_other_stacks(&rows)
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

    // `classify_checkout` -- the guard that keeps a worktree from being
    // adopted as the install directory (#319).

    #[test]
    fn a_main_checkout_is_a_main_checkout() {
        // toplevel == the parent of the shared .git
        assert_eq!(
            classify_checkout(Some("/repo"), Some("/repo/.git")),
            GitCheckout::MainCheckout
        );
    }

    #[test]
    fn a_worktree_is_recognized_by_its_shared_git_dir() {
        // A worktree's --git-common-dir still points at the main checkout's
        // .git, which is what makes the two disagree.
        assert_eq!(
            classify_checkout(Some("/repo/.claude/worktrees/feature-x"), Some("/repo/.git")),
            GitCheckout::Worktree
        );
    }

    #[test]
    fn trailing_newlines_from_git_stdout_do_not_defeat_the_comparison() {
        // `git rev-parse` output is newline-terminated; comparing unrimmed
        // would classify every main checkout as a worktree, which would
        // break the documented dev-trunk discovery case rather than the
        // thing this guard is for.
        assert_eq!(
            classify_checkout(Some("/repo\n"), Some("/repo/.git\n")),
            GitCheckout::MainCheckout
        );
    }

    #[test]
    fn a_directory_outside_git_is_not_a_repository() {
        // The shipped-install case: ~/.bioflow has no git anywhere, and
        // `rev-parse` exits non-zero there. Must stay discoverable.
        assert_eq!(
            classify_checkout(None, None),
            GitCheckout::NotAGitRepository
        );
    }

    #[test]
    fn a_partial_git_answer_is_not_a_repository() {
        // Neither call succeeding on its own is enough to classify; failing
        // closed to NotAGitRepository keeps a machine without git working
        // exactly as it did before this check existed.
        assert_eq!(
            classify_checkout(Some("/repo"), None),
            GitCheckout::NotAGitRepository
        );
        assert_eq!(
            classify_checkout(None, Some("/repo/.git")),
            GitCheckout::NotAGitRepository
        );
    }

    // `collect_other_stacks` -- turning docker ps rows into the read-only
    // inventory of worktree stacks (#320).

    fn row(project: &str, service: &str, port: Option<u16>) -> (String, String, Option<u16>) {
        (project.to_string(), service.to_string(), port)
    }

    #[test]
    fn the_launchers_own_project_is_never_listed() {
        // The whole point is *other* stacks. Listing `biopipe` would be
        // telling the user about the stack they are already looking at.
        let rows = vec![
            row("biopipe", "web", Some(5173)),
            row("biopipe", "api", None),
        ];
        assert_eq!(collect_other_stacks(&rows), Vec::new());
    }

    #[test]
    fn unrelated_compose_projects_are_never_listed() {
        // Someone else's postgres is not a BioFlow stack.
        let rows = vec![row("some-other-app", "web", Some(3000))];
        assert_eq!(collect_other_stacks(&rows), Vec::new());
    }

    #[test]
    fn a_worktree_stacks_containers_collapse_into_one_entry() {
        // A stack is five-plus containers; the user wants one line per stack.
        let rows = vec![
            row("biopipe-wt-feat-x", "mongo", None),
            row("biopipe-wt-feat-x", "web", Some(5273)),
            row("biopipe-wt-feat-x", "api", None),
            row("biopipe-wt-feat-x", "worker", None),
        ];
        assert_eq!(
            collect_other_stacks(&rows),
            vec![OtherStack {
                project: "biopipe-wt-feat-x".to_string(),
                slug: "feat-x".to_string(),
                web_port: Some(5273),
            }]
        );
    }

    #[test]
    fn only_the_web_services_port_is_reported() {
        // Taking any service's port would show an api or mongo port and
        // invite the user to open it in a browser.
        let rows = vec![
            row("biopipe-wt-feat-x", "api", Some(8100)),
            row("biopipe-wt-feat-x", "web", Some(5273)),
        ];
        assert_eq!(collect_other_stacks(&rows)[0].web_port, Some(5273));
    }

    #[test]
    fn a_stack_with_no_readable_web_port_still_appears() {
        // Knowing a stack is up matters more than knowing its port; dropping
        // it for a missing port would hide exactly the broken stack (no web
        // container, or a restart loop) most worth surfacing.
        let rows = vec![row("biopipe-wt-feat-x", "mongo", None)];
        assert_eq!(
            collect_other_stacks(&rows),
            vec![OtherStack {
                project: "biopipe-wt-feat-x".to_string(),
                slug: "feat-x".to_string(),
                web_port: None,
            }]
        );
    }

    #[test]
    fn several_stacks_are_listed_in_a_stable_order() {
        // The list is read at a glance; its order must not shuffle between
        // polls as Docker reorders containers.
        let rows = vec![
            row("biopipe-wt-zebra", "web", Some(5290)),
            row("biopipe-wt-alpha", "web", Some(5210)),
            row("biopipe", "web", Some(5173)),
        ];
        let stacks = collect_other_stacks(&rows);
        assert_eq!(
            stacks.iter().map(|s| s.slug.as_str()).collect::<Vec<_>>(),
            vec!["alpha", "zebra"]
        );
    }

    #[test]
    fn a_bare_prefix_with_no_slug_is_not_a_stack() {
        // `biopipe-wt-` alone encodes no branch; treating it as a stack would
        // put a nameless row in front of the user.
        let rows = vec![row("biopipe-wt-", "web", Some(5273))];
        assert_eq!(collect_other_stacks(&rows), Vec::new());
    }

    // `host_port_from_bindings` -- reading the port out of docker inspect.

    #[test]
    fn a_published_port_is_read_from_host_config() {
        // Real shape, copied from `docker inspect` on this machine.
        let json = r#"{"80/tcp":[{"HostIp":"0.0.0.0","HostPort":"5173"}]}"#;
        assert_eq!(host_port_from_bindings(json), Some(5173));
    }

    #[test]
    fn an_empty_host_ip_still_yields_the_port() {
        // How a worktree stack's binding actually looks, since
        // ops/docker-compose.worktree.yml publishes on ${BIND_ADDRESS:-127.0.0.1}.
        let json = r#"{"80/tcp":[{"HostIp":"","HostPort":"5273"}]}"#;
        assert_eq!(host_port_from_bindings(json), Some(5273));
    }

    #[test]
    fn a_container_publishing_nothing_has_no_port() {
        assert_eq!(host_port_from_bindings("{}"), None);
        assert_eq!(host_port_from_bindings("null"), None);
    }

    #[test]
    fn malformed_inspect_output_is_not_a_port() {
        // `docker inspect` failing or changing shape must not panic a poll.
        assert_eq!(host_port_from_bindings(""), None);
        assert_eq!(host_port_from_bindings("not json"), None);
        assert_eq!(
            host_port_from_bindings(r#"{"80/tcp":[{"HostPort":"not-a-number"}]}"#),
            None
        );
    }

    /// Exercises the real `git` invocations against a real repository and a
    /// real worktree, not just the pure classifier.
    ///
    /// This is the direction that fails when the seam breaks: the fixture
    /// tests above would still pass if `classify_dir` passed the wrong flags
    /// (or none) to `git rev-parse`, since they never run git at all.
    #[test]
    fn classify_dir_tells_a_real_worktree_from_its_main_checkout() {
        let tmp = tempfile::tempdir().unwrap();
        let main = tmp.path().join("main");
        std::fs::create_dir_all(&main).unwrap();

        let git = |args: &[&str], cwd: &std::path::Path| {
            let status = Command::new("git")
                .arg("-C")
                .arg(cwd)
                .args(args)
                .output()
                .expect("git should be available in the test environment");
            assert!(
                status.status.success(),
                "git {args:?} failed: {}",
                String::from_utf8_lossy(&status.stderr)
            );
        };

        git(&["init", "-q"], &main);
        git(&["config", "user.email", "test@example.com"], &main);
        git(&["config", "user.name", "Test"], &main);
        // A worktree cannot be added from a repository with no commits.
        std::fs::write(main.join("README"), "x").unwrap();
        git(&["add", "README"], &main);
        git(&["commit", "-qm", "init"], &main);

        let worktree = tmp.path().join("wt");
        git(
            &[
                "worktree",
                "add",
                "-q",
                "-b",
                "feature",
                worktree.to_str().unwrap(),
            ],
            &main,
        );

        assert_eq!(
            ShellDocker::classify_dir(main.to_str().unwrap()),
            GitCheckout::MainCheckout
        );
        assert_eq!(
            ShellDocker::classify_dir(worktree.to_str().unwrap()),
            GitCheckout::Worktree
        );

        // And a plain directory outside any repository -- the shipped
        // install. Uses the tempdir's parent-level sibling so it is not
        // inside the repo created above.
        let plain = tmp.path().join("plain");
        std::fs::create_dir_all(&plain).unwrap();
        assert_eq!(
            ShellDocker::classify_dir(plain.to_str().unwrap()),
            GitCheckout::NotAGitRepository
        );
    }
}
