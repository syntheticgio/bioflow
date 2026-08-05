//! The Docker-facing seam. Everything the launcher's state machine needs to
//! know about the daemon and the compose stack goes through this trait, so
//! the state machine itself can be tested against `FakeDocker` with no Docker
//! present -- see the design spec's "Testing" section.

pub mod fake;
pub mod shell;

pub use fake::FakeDocker;
pub use shell::ShellDocker;

/// Whether the `docker` binary exists on PATH at all, distinct from whether
/// its daemon is reachable. The two need different screens: one points at a
/// download link, the other at "waiting for Docker to start."
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DockerPresence {
    NotInstalled,
    InstalledDaemonDown,
    InstalledDaemonUp,
}

/// One row of `docker compose ps`, reduced to what the state machine needs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceStatus {
    pub name: String,
    pub running: bool,
}

/// Outcome of a `docker compose` action the launcher shells out for. Actions
/// never panic on failure -- a failed `up` or `pull` is still information the
/// state machine and the UI need to show, not something to unwrap away.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ActionResult {
    Ok,
    Failed { output: String },
}

/// The Docker interface the design spec names: probe, up, down, ps, pull,
/// plus health (the API healthcheck the "Running" state gates on) and
/// manifest_digest (the cheap registry check behind the Update button).
///
/// All of it shells out to the user's own `docker` binary with
/// `--project-directory` set to the install directory -- no Docker API client
/// library, no bundled Docker, per the spec's "Actions" section.
pub trait DockerBackend {
    /// Distinguishes "not installed" from "installed but daemon down" from
    /// "daemon reachable."
    fn probe(&self) -> DockerPresence;

    /// `docker compose up -d`.
    fn up(&self, install_dir: &str) -> ActionResult;

    /// `docker compose down`.
    fn down(&self, install_dir: &str) -> ActionResult;

    /// `docker compose ps`, reduced to per-service running/not-running.
    fn ps(&self, install_dir: &str) -> Vec<ServiceStatus>;

    /// `docker compose pull`, only ever called on an explicit Update click.
    fn pull(&self, install_dir: &str) -> ActionResult;

    /// The API healthcheck -- what makes "Running" mean the API answered,
    /// not merely that containers exist.
    fn health(&self, install_dir: &str) -> bool;

    /// Compares the local `:latest` digest against the registry's. `None`
    /// means the check could not complete (offline, registry unreachable);
    /// callers must treat that as "no update to offer," never as an error,
    /// since this check is explicitly non-blocking and fails silently.
    fn manifest_digest_differs(&self, install_dir: &str) -> Option<bool>;

    /// Attempts to start the Docker daemon (`open -a Docker` on macOS,
    /// launching Docker Desktop on Windows, `systemctl --user start docker`
    /// on Linux). Returns immediately; the caller polls `probe` afterward.
    fn attempt_daemon_start(&self);
}
