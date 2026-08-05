//! First-run setup: the three questions the design spec names (storage
//! location, install directory, port), their validation, and the resumable
//! write sequence that turns answers into a running stack.
//!
//! Validation carries more weight than the questions themselves, per the
//! spec: a wrong answer here surfaces much later as an empty `/data` or a
//! port collision, with nothing pointing at the setup step as the cause.

pub mod defaults;
pub mod install;
pub mod validate;

pub use defaults::SetupDefaults;
pub use install::{install, install_exists, InstallError, InstallInputs};
pub use validate::{validate_port, validate_storage_path, PortValidation, StoragePathValidation};
