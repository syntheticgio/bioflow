//! Per-OS defaults for the three first-run questions. Each is a starting
//! point the user can override, never a forced choice.

use std::path::PathBuf;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SetupDefaults {
    pub storage_location: PathBuf,
    pub install_dir: PathBuf,
    pub port: u16,
}

impl SetupDefaults {
    pub fn for_this_os() -> Self {
        Self::for_home(dirs::home_dir())
    }

    /// Split out from `for_this_os` so tests can supply a fake home
    /// directory instead of depending on the environment the test runs in.
    pub fn for_home(home: Option<PathBuf>) -> Self {
        let home = home.unwrap_or_else(|| PathBuf::from("."));
        Self {
            storage_location: home.join("BioFlow"),
            install_dir: home.join(".bioflow"),
            port: 5173,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_under_the_given_home() {
        let home = PathBuf::from("/Users/test");
        let defaults = SetupDefaults::for_home(Some(home.clone()));
        assert_eq!(defaults.storage_location, home.join("BioFlow"));
        assert_eq!(defaults.install_dir, home.join(".bioflow"));
        assert_eq!(defaults.port, 5173);
    }

    #[test]
    fn falls_back_to_a_relative_path_when_home_is_unknown() {
        let defaults = SetupDefaults::for_home(None);
        assert_eq!(defaults.storage_location, PathBuf::from("./BioFlow"));
    }
}
