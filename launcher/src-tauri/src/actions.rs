//! Run, Stop, Update, and the health-gated wait that stands between "up"
//! and "point the browser at it" -- see the design spec's "Actions" and
//! "Browser handoff" sections.
//!
//! Each of these is deliberately thin: the state machine already defines
//! what Running/Stopped mean, so an action's job is just to call the right
//! `DockerBackend` method and translate its outcome into one of the named
//! error states the spec requires ("None of them is a generic failure
//! dialog").

use crate::docker::{ActionResult, DockerBackend};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunOutcome {
    /// Containers are up and the API healthcheck passed within the wait
    /// window -- the caller can now do the browser handoff.
    Running,
    /// `docker compose up` itself failed (spec: port in use, disk full,
    /// daemon died mid-command). The raw compose output is kept so the UI
    /// can show it rather than a generic dialog.
    ComposeFailed { output: String },
    /// `up` succeeded but health never passed within the wait window. This
    /// is distinct from ComposeFailed: the command worked, something inside
    /// the stack didn't come up healthy in time.
    NeverBecameHealthy,
}

/// Runs `up`, then polls `health` until it passes or `max_attempts` polls
/// elapse. `sleep` is injected so tests do not depend on real wall-clock
/// time -- the same pattern as `state::start_daemon_and_wait`.
pub fn run<D: DockerBackend>(
    docker: &D,
    install_dir: &str,
    max_attempts: u32,
    mut sleep: impl FnMut(),
) -> RunOutcome {
    match docker.up(install_dir) {
        ActionResult::Ok => {}
        ActionResult::Failed { output } => return RunOutcome::ComposeFailed { output },
    }

    for attempt in 0..max_attempts {
        if docker.health(install_dir) {
            return RunOutcome::Running;
        }
        if attempt + 1 < max_attempts {
            sleep();
        }
    }

    RunOutcome::NeverBecameHealthy
}

/// Outcome of `run_node` — starts only the worker service via
/// `docker compose up -d --no-deps worker`. No healthcheck gate
/// (the worker heartbeats into Redis, which takes a few seconds).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NodeRunOutcome {
    /// Worker container is up.
    Running,
    /// `docker compose up` itself failed.
    ComposeFailed { output: String },
}

/// Starts only the worker service (no mongo, redis, api, or web) — used
/// for compute-node installs where the database lives on the primary
/// machine. No healthcheck polling because there is no API to probe;
/// the worker reports its own availability through Redis heartbeats.
pub fn run_node<D: DockerBackend>(docker: &D, install_dir: &str) -> NodeRunOutcome {
    match docker.up_node(install_dir) {
        ActionResult::Ok => NodeRunOutcome::Running,
        ActionResult::Failed { output } => NodeRunOutcome::ComposeFailed { output },
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StopOutcome {
    Stopped,
    Failed { output: String },
}

pub fn stop<D: DockerBackend>(docker: &D, install_dir: &str) -> StopOutcome {
    match docker.down(install_dir) {
        ActionResult::Ok => StopOutcome::Stopped,
        ActionResult::Failed { output } => StopOutcome::Failed { output },
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UpdateOutcome {
    Updated,
    /// The spec requires Update to be pull-then-recreate, and only ever on
    /// an explicit click -- never invoked automatically. Distinguishing
    /// which half failed matters for the message shown: a pull failure
    /// means "couldn't reach the registry," a recreate failure means the
    /// new image was fetched but starting it broke.
    PullFailed { output: String },
    RecreateFailed { output: String },
}

/// Update is `docker compose pull` followed by a recreate. This function is
/// only ever called from an explicit UI action -- there is no automatic or
/// scheduled path to it anywhere in this module.
pub fn update<D: DockerBackend>(docker: &D, install_dir: &str) -> UpdateOutcome {
    match docker.pull(install_dir) {
        ActionResult::Ok => {}
        ActionResult::Failed { output } => return UpdateOutcome::PullFailed { output },
    }

    match docker.up(install_dir) {
        ActionResult::Ok => UpdateOutcome::Updated,
        ActionResult::Failed { output } => UpdateOutcome::RecreateFailed { output },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::docker::FakeDocker;

    #[test]
    fn run_reaches_running_once_health_passes() {
        let docker = FakeDocker::new();
        // Health flips true on the second poll, simulating a cold start.
        let mut polls = 0u32;
        docker.healthy.set(false);

        let outcome = run(&docker, "/tmp/install", 5, || {
            polls += 1;
            if polls == 1 {
                docker.healthy.set(true);
            }
        });

        assert_eq!(outcome, RunOutcome::Running);
    }

    #[test]
    fn run_reports_compose_failure_without_waiting_for_health() {
        let docker = FakeDocker::new();
        *docker.up_result.borrow_mut() = ActionResult::Failed {
            output: "port already in use".to_string(),
        };

        let mut sleeps = 0u32;
        let outcome = run(&docker, "/tmp/install", 5, || sleeps += 1);

        assert_eq!(
            outcome,
            RunOutcome::ComposeFailed {
                output: "port already in use".to_string()
            }
        );
        assert_eq!(sleeps, 0, "a failed `up` must not wait for health at all");
    }

    #[test]
    fn run_gives_up_after_max_attempts_if_never_healthy() {
        let docker = FakeDocker::new();
        docker.healthy.set(false);

        let mut sleeps = 0u32;
        let outcome = run(&docker, "/tmp/install", 3, || sleeps += 1);

        assert_eq!(outcome, RunOutcome::NeverBecameHealthy);
        assert_eq!(sleeps, 2, "sleeps between polls, not after the last one");
    }

    #[test]
    fn stop_reports_down_failure_with_output() {
        let docker = FakeDocker::new();
        *docker.down_result.borrow_mut() = ActionResult::Failed {
            output: "container busy".to_string(),
        };

        assert_eq!(
            stop(&docker, "/tmp/install"),
            StopOutcome::Failed {
                output: "container busy".to_string()
            }
        );
    }

    #[test]
    fn update_pulls_then_recreates_in_order() {
        let docker = FakeDocker::new();
        assert_eq!(update(&docker, "/tmp/install"), UpdateOutcome::Updated);
    }

    #[test]
    fn update_reports_pull_failure_distinctly_from_recreate_failure() {
        let docker = FakeDocker::new();
        *docker.pull_result.borrow_mut() = ActionResult::Failed {
            output: "offline".to_string(),
        };

        assert_eq!(
            update(&docker, "/tmp/install"),
            UpdateOutcome::PullFailed {
                output: "offline".to_string()
            }
        );
    }

    #[test]
    fn update_reports_recreate_failure_when_pull_succeeds_but_up_fails() {
        let docker = FakeDocker::new();
        *docker.up_result.borrow_mut() = ActionResult::Failed {
            output: "disk full".to_string(),
        };

        assert_eq!(
            update(&docker, "/tmp/install"),
            UpdateOutcome::RecreateFailed {
                output: "disk full".to_string()
            }
        );
    }
}
