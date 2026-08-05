//! The launcher's state machine, evaluated at every launch and on a status
//! poll -- see the design spec's "Runtime contract / States" section.
//!
//! `Running` is health-gated, not container-gated, on purpose: containers up
//! but the API healthcheck failing is still `Stopped` from the user's point
//! of view. That is the transition most likely to be got wrong, which is why
//! it has its own tests below rather than being folded into a general case.

use crate::docker::{DockerBackend, DockerPresence};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LauncherState {
    /// No install directory or no `.env`.
    NotInstalled,
    /// An install exists but the daemon is unreachable. `installed`
    /// distinguishes "no docker binary" from "binary present, daemon down" --
    /// they need different screens (spec: download link vs. "waiting for
    /// Docker...").
    DockerUnavailable { installed: bool },
    /// Daemon reachable, no BioFlow containers running (or containers exist
    /// but the API healthcheck is not yet passing).
    Stopped,
    /// Containers up AND the API healthcheck passing.
    Running,
}

/// Whether an install exists at all -- the one input this module does not
/// get from `DockerBackend`, since it is a filesystem fact about the install
/// directory rather than something Docker knows.
pub struct InstallInfo<'a> {
    pub install_dir: Option<&'a str>,
}

/// Derives the current `LauncherState` from a fresh probe. Called at every
/// launch and on each status poll, never cached across calls -- a daemon
/// that dies while running must flip the very next poll to
/// `DockerUnavailable` rather than showing a stale `Running`.
pub fn evaluate<D: DockerBackend>(docker: &D, install: &InstallInfo) -> LauncherState {
    let Some(install_dir) = install.install_dir else {
        return LauncherState::NotInstalled;
    };

    match docker.probe() {
        DockerPresence::NotInstalled => {
            return LauncherState::DockerUnavailable { installed: false }
        }
        DockerPresence::InstalledDaemonDown => {
            return LauncherState::DockerUnavailable { installed: true }
        }
        DockerPresence::InstalledDaemonUp => {}
    }

    let services = docker.ps(install_dir);
    let any_running = services.iter().any(|s| s.running);
    if !any_running {
        return LauncherState::Stopped;
    }

    if docker.health(install_dir) {
        LauncherState::Running
    } else {
        // Containers exist but the API has not answered yet -- still Stopped
        // from the user's point of view, per the spec.
        LauncherState::Stopped
    }
}

/// Outcome of `start_daemon_and_wait`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DaemonStartOutcome {
    Started,
    TimedOut,
}

/// Attempts to start the Docker daemon and polls `probe` until it reports
/// up, an attempt limit is reached, or `elapsed` exceeds `timeout`. The
/// 60-second timeout is a spec requirement, not a safety net: daemon startup
/// can fail without reporting anything, so waiting indefinitely just hangs.
///
/// `sleep` and `elapsed` are injected so tests can simulate the passage of
/// time without a real 60-second wait; the shipped caller passes
/// `std::thread::sleep` and a real clock.
pub fn start_daemon_and_wait<D: DockerBackend>(
    docker: &D,
    timeout: std::time::Duration,
    poll_interval: std::time::Duration,
    mut sleep: impl FnMut(std::time::Duration),
    mut elapsed: impl FnMut() -> std::time::Duration,
) -> DaemonStartOutcome {
    docker.attempt_daemon_start();

    loop {
        if docker.probe() == DockerPresence::InstalledDaemonUp {
            return DaemonStartOutcome::Started;
        }
        if elapsed() >= timeout {
            return DaemonStartOutcome::TimedOut;
        }
        sleep(poll_interval);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::docker::FakeDocker;
    use std::cell::Cell;
    use std::time::Duration;

    fn installed(dir: &str) -> InstallInfo<'_> {
        InstallInfo {
            install_dir: Some(dir),
        }
    }

    #[test]
    fn no_install_dir_is_not_installed() {
        let docker = FakeDocker::new();
        let info = InstallInfo { install_dir: None };
        assert_eq!(evaluate(&docker, &info), LauncherState::NotInstalled);
    }

    #[test]
    fn missing_docker_binary_is_docker_unavailable_not_installed() {
        let docker = FakeDocker::with_presence(DockerPresence::NotInstalled);
        assert_eq!(
            evaluate(&docker, &installed("/tmp/bioflow")),
            LauncherState::DockerUnavailable { installed: false }
        );
    }

    #[test]
    fn installed_but_daemon_down_is_docker_unavailable_installed() {
        let docker = FakeDocker::with_presence(DockerPresence::InstalledDaemonDown);
        assert_eq!(
            evaluate(&docker, &installed("/tmp/bioflow")),
            LauncherState::DockerUnavailable { installed: true }
        );
    }

    #[test]
    fn daemon_up_no_containers_is_stopped() {
        let docker = FakeDocker::with_presence(DockerPresence::InstalledDaemonUp);
        assert_eq!(
            evaluate(&docker, &installed("/tmp/bioflow")),
            LauncherState::Stopped
        );
    }

    #[test]
    fn containers_up_but_unhealthy_is_stopped_not_running() {
        // This is the transition the spec calls out as most likely to be
        // got wrong: "Running" must mean the API answered, not merely that
        // containers exist.
        let docker = FakeDocker::with_presence(DockerPresence::InstalledDaemonUp);
        docker.set_running("api");
        docker.set_running("web");
        docker.healthy.set(false);
        assert_eq!(
            evaluate(&docker, &installed("/tmp/bioflow")),
            LauncherState::Stopped
        );
    }

    #[test]
    fn containers_up_and_healthy_is_running() {
        let docker = FakeDocker::with_presence(DockerPresence::InstalledDaemonUp);
        docker.set_running("api");
        docker.healthy.set(true);
        assert_eq!(
            evaluate(&docker, &installed("/tmp/bioflow")),
            LauncherState::Running
        );
    }

    #[test]
    fn daemon_dying_while_running_flips_to_docker_unavailable_not_stale_running() {
        let docker = FakeDocker::with_presence(DockerPresence::InstalledDaemonUp);
        docker.set_running("api");
        docker.healthy.set(true);
        assert_eq!(
            evaluate(&docker, &installed("/tmp/bioflow")),
            LauncherState::Running
        );

        // The daemon dies between polls. A fresh evaluate() must re-probe
        // rather than trust the previous Running result.
        docker.presence.set(DockerPresence::InstalledDaemonDown);
        assert_eq!(
            evaluate(&docker, &installed("/tmp/bioflow")),
            LauncherState::DockerUnavailable { installed: true }
        );
    }

    #[test]
    fn daemon_start_succeeds_when_probe_flips_up_before_timeout() {
        let docker = FakeDocker::with_presence(DockerPresence::InstalledDaemonDown);
        *docker.probe_after_start_sequence.borrow_mut() = vec![
            DockerPresence::InstalledDaemonDown,
            DockerPresence::InstalledDaemonDown,
            DockerPresence::InstalledDaemonUp,
        ];

        let elapsed_secs = Cell::new(0u64);
        let outcome = start_daemon_and_wait(
            &docker,
            Duration::from_secs(60),
            Duration::from_secs(1),
            |_| elapsed_secs.set(elapsed_secs.get() + 1),
            || Duration::from_secs(elapsed_secs.get()),
        );

        assert_eq!(outcome, DaemonStartOutcome::Started);
        assert_eq!(docker.daemon_start_calls.get(), 1);
    }

    #[test]
    fn daemon_start_times_out_after_60_seconds_and_falls_back() {
        // The daemon never comes up. The timeout is part of the design, not
        // a safety net -- this asserts it actually fires rather than hanging.
        let docker = FakeDocker::with_presence(DockerPresence::InstalledDaemonDown);

        let elapsed_secs = Cell::new(0u64);
        let outcome = start_daemon_and_wait(
            &docker,
            Duration::from_secs(60),
            Duration::from_secs(5),
            |step| elapsed_secs.set(elapsed_secs.get() + step.as_secs()),
            || Duration::from_secs(elapsed_secs.get()),
        );

        assert_eq!(outcome, DaemonStartOutcome::TimedOut);
        assert_eq!(docker.daemon_start_calls.get(), 1);
    }
}
