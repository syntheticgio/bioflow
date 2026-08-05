//! Validation for the three first-run answers. The design spec weights this
//! above the questions themselves: an unshared macOS path or a taken port
//! both succeed silently at first glance and surface much later with a
//! symptom that does not point back at setup.

use std::net::TcpListener;
use std::path::Path;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoragePathValidation {
    Ok,
    NotWritable,
    /// macOS only: the path is outside the user's home directory or any
    /// explicitly shared root, so Docker Desktop will not file-share it.
    /// This is a warning, not a hard block -- Docker Desktop's shared-roots
    /// list is not a stable public API, so an occasional false positive is
    /// the accepted cost of catching the common case (an empty /data with
    /// no explanation) at all.
    NotDockerShared,
}

/// Validates a storage path by creating it if missing and then attempting to
/// write into it, not by `stat`-ing permission bits -- a path can be
/// readable-looking and still reject a write (e.g. a read-only network
/// mount), and the failure the user actually hits is a failed write.
///
/// A nonexistent path is not an error here: this is a first-run setup
/// screen, and both defaults it proposes (`SetupDefaults`) are paths that by
/// definition don't exist yet on a fresh machine. Asking a non-technical
/// user to go create a folder in a terminal before the installer will let
/// them proceed defeats the point of an installer, so this creates it
/// instead -- mirroring what `setup::install` already does for the install
/// directory. A path that can't be created (e.g. no permission on its
/// parent) falls through to the same `NotWritable` outcome as one that
/// exists but rejects a write; the user-facing symptom and remedy are the
/// same either way.
pub fn validate_storage_path(path: &Path, shared_roots: &[std::path::PathBuf]) -> StoragePathValidation {
    if !path.exists() && std::fs::create_dir_all(path).is_err() {
        return StoragePathValidation::NotWritable;
    }

    let probe_file = path.join(".bioflow-write-probe");
    let writable = std::fs::write(&probe_file, b"probe").is_ok();
    let _ = std::fs::remove_file(&probe_file);
    if !writable {
        return StoragePathValidation::NotWritable;
    }

    if cfg!(target_os = "macos") && !is_docker_shared(path, shared_roots) {
        return StoragePathValidation::NotDockerShared;
    }

    StoragePathValidation::Ok
}

fn is_docker_shared(path: &Path, shared_roots: &[std::path::PathBuf]) -> bool {
    shared_roots.iter().any(|root| path.starts_with(root))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PortValidation {
    Ok,
    InUse,
}

/// Verifies a port is free by binding it, not by scanning a process list --
/// binding is the actual thing that will fail at Run time, so it is the only
/// check that cannot disagree with reality.
pub fn validate_port(port: u16) -> PortValidation {
    match TcpListener::bind(("127.0.0.1", port)) {
        Ok(listener) => {
            drop(listener);
            PortValidation::Ok
        }
        Err(_) => PortValidation::InUse,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn a_missing_folder_under_an_existing_parent_is_created_and_ok() {
        let parent = tempfile::tempdir().unwrap();
        let path = parent.path().join("BioFlow");
        assert!(!path.exists());

        let shared_roots = vec![parent.path().to_path_buf()];
        assert_eq!(
            validate_storage_path(&path, &shared_roots),
            StoragePathValidation::Ok
        );
        assert!(path.is_dir(), "the folder should have been created");
    }

    #[test]
    fn a_path_whose_parent_does_not_exist_and_cannot_be_created_is_not_writable() {
        // No real filesystem lets you create a directory under a path that
        // doesn't exist and never will -- /proc is not a writable mountpoint
        // on Linux, so a child under it can never be created.
        let path = PathBuf::from("/proc/this-cannot-be-created/BioFlow");
        assert_eq!(
            validate_storage_path(&path, &[]),
            StoragePathValidation::NotWritable
        );
    }

    #[test]
    fn writable_path_within_a_shared_root_is_ok() {
        let dir = tempfile::tempdir().unwrap();
        let shared_roots = vec![dir.path().to_path_buf()];
        assert_eq!(
            validate_storage_path(dir.path(), &shared_roots),
            StoragePathValidation::Ok
        );
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn writable_path_outside_shared_roots_warns_on_macos() {
        let dir = tempfile::tempdir().unwrap();
        // No shared roots configured, so this path (real temp dir, writable)
        // still trips the Docker Desktop file-sharing warning on macOS.
        assert_eq!(
            validate_storage_path(dir.path(), &[]),
            StoragePathValidation::NotDockerShared
        );
    }

    #[test]
    #[cfg(unix)]
    fn non_writable_path_is_not_writable() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let original = std::fs::metadata(dir.path()).unwrap().permissions();
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o555)).unwrap();

        let shared_roots = vec![dir.path().to_path_buf()];
        let result = validate_storage_path(dir.path(), &shared_roots);

        // Restore permissions so the tempdir can clean itself up.
        std::fs::set_permissions(dir.path(), original).unwrap();

        assert_eq!(result, StoragePathValidation::NotWritable);
    }

    #[test]
    fn a_bound_port_is_reported_in_use() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();

        assert_eq!(validate_port(port), PortValidation::InUse);

        drop(listener);
        assert_eq!(validate_port(port), PortValidation::Ok);
    }
}
