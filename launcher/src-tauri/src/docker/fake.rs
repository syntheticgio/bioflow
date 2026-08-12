//! A fully scriptable `DockerBackend` for testing the state machine without a
//! real daemon. Every method reads from `Cell`/`RefCell` fields a test sets up
//! front, so a test can move the fake through exactly the sequence of states
//! a real run would produce (e.g. daemon up -> containers up but unhealthy ->
//! healthy -> daemon dies).

use std::cell::{Cell, RefCell};

use super::{ActionResult, DockerBackend, DockerPresence, OtherStack, ServiceStatus};

pub struct FakeDocker {
    pub presence: Cell<DockerPresence>,
    pub services: RefCell<Vec<ServiceStatus>>,
    pub healthy: Cell<bool>,
    pub build_result: RefCell<ActionResult>,
    pub up_result: RefCell<ActionResult>,
    pub up_node_result: RefCell<ActionResult>,
    pub down_result: RefCell<ActionResult>,
    pub pull_result: RefCell<ActionResult>,
    pub pull_image_result: RefCell<ActionResult>,
    /// Every image name `pull_image` was called with, in order -- lets a
    /// test assert *which* tools were actually pulled, not merely that
    /// pulling succeeded.
    pub pull_image_calls: RefCell<Vec<String>>,
    pub manifest_differs: Cell<Option<bool>>,
    pub daemon_start_calls: Cell<u32>,
    /// If set, `probe` returns this value the Nth time it is called (1-indexed)
    /// after `daemon_start_calls` was last incremented, letting a test model
    /// the daemon coming up after a delay rather than instantly or never.
    pub probe_after_start_sequence: RefCell<Vec<DockerPresence>>,
    /// What `discover_running_project_dir` returns for any project name,
    /// or `None` to model "nothing found."
    pub discovered_project_dir: RefCell<Option<String>>,
    /// What `other_stacks` returns -- the worktree stacks running alongside
    /// the launcher's own. Empty models the ordinary single-stack machine.
    pub other_stacks: RefCell<Vec<OtherStack>>,
}

impl Default for FakeDocker {
    fn default() -> Self {
        Self {
            presence: Cell::new(DockerPresence::InstalledDaemonUp),
            services: RefCell::new(Vec::new()),
            healthy: Cell::new(false),
            build_result: RefCell::new(ActionResult::Ok),
            up_result: RefCell::new(ActionResult::Ok),
            up_node_result: RefCell::new(ActionResult::Ok),
            down_result: RefCell::new(ActionResult::Ok),
            pull_result: RefCell::new(ActionResult::Ok),
            pull_image_result: RefCell::new(ActionResult::Ok),
            pull_image_calls: RefCell::new(Vec::new()),
            manifest_differs: Cell::new(None),
            daemon_start_calls: Cell::new(0),
            probe_after_start_sequence: RefCell::new(Vec::new()),
            discovered_project_dir: RefCell::new(None),
            other_stacks: RefCell::new(Vec::new()),
        }
    }
}

impl FakeDocker {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_presence(presence: DockerPresence) -> Self {
        Self {
            presence: Cell::new(presence),
            ..Self::default()
        }
    }

    pub fn set_running(&self, name: &str) {
        self.services.borrow_mut().push(ServiceStatus {
            name: name.to_string(),
            running: true,
        });
    }

    pub fn set_all_down(&self) {
        self.services.borrow_mut().clear();
    }
}

impl DockerBackend for FakeDocker {
    fn probe(&self) -> DockerPresence {
        // If a scripted post-start sequence is set, consume one entry per
        // call to model the daemon taking several polls to come up.
        let mut seq = self.probe_after_start_sequence.borrow_mut();
        if !seq.is_empty() {
            let next = seq.remove(0);
            self.presence.set(next);
            return next;
        }
        self.presence.get()
    }

    fn up(&self, _install_dir: &str) -> ActionResult {
        self.up_result.borrow().clone()
    }

    fn build(&self, _install_dir: &str) -> ActionResult {
        self.build_result.borrow().clone()
    }

    fn up_node(&self, _install_dir: &str) -> ActionResult {
        self.up_node_result.borrow().clone()
    }

    fn down(&self, _install_dir: &str) -> ActionResult {
        self.services.borrow_mut().clear();
        self.down_result.borrow().clone()
    }

    fn ps(&self, _install_dir: &str) -> Vec<ServiceStatus> {
        self.services.borrow().clone()
    }

    fn pull(&self, _install_dir: &str) -> ActionResult {
        self.pull_result.borrow().clone()
    }

    fn pull_image(&self, image: &str) -> ActionResult {
        self.pull_image_calls.borrow_mut().push(image.to_string());
        self.pull_image_result.borrow().clone()
    }

    fn health(&self, _install_dir: &str) -> bool {
        self.healthy.get()
    }

    fn manifest_digest_differs(&self, _install_dir: &str) -> Option<bool> {
        self.manifest_differs.get()
    }

    fn attempt_daemon_start(&self) {
        self.daemon_start_calls.set(self.daemon_start_calls.get() + 1);
    }

    fn discover_running_project_dir(&self, _project_name: &str) -> Option<String> {
        self.discovered_project_dir.borrow().clone()
    }

    fn other_stacks(&self) -> Vec<OtherStack> {
        self.other_stacks.borrow().clone()
    }
}
