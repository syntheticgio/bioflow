//! Remote machine operations over SSH for compute-node installation.
//!
//! Uses the system's `ssh`, `scp`, and (optionally) `sshpass` commands
//! rather than a Rust SSH library. The launcher already shells out to
//! `docker compose` via `ShellDocker` — this module follows the same
//! pattern and reuses the same `std::process::Command` conventions.
//!
//! ## Auth
//!
//! Two modes, matching the two setup paths the frontend offers:
//!
//! - **Key-based** (SSH agent or default key): plain `ssh user@host`.
//! - **Password-based**: `sshpass -p <password> ssh user@host` when
//!   `sshpass` is available on the PATH. Falls back to a clear error
//!   rather than silently failing.

use std::process::Command;

/// Credentials for connecting to a remote machine over SSH.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SshCreds {
    pub host: String,
    pub user: String,
    /// `None` means use key-based auth (SSH agent or `~/.ssh/id_*`).
    /// `Some` means use password auth via `sshpass`.
    pub password: Option<String>,
    /// SSH port. Defaults to 22.
    pub port: u16,
}

impl SshCreds {
    /// For display only — never logged with the password in it.
    pub fn connection_string(&self) -> String {
        if self.port == 22 {
            format!("{}@{}", self.user, self.host)
        } else {
            format!("{}@{}:{}", self.user, self.host, self.port)
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SshResult {
    Ok(String),
    Failed {
        output: String,
    },
    /// Password auth was requested but `sshpass` is not installed —
    /// the frontend can show a "please install sshpass" message.
    SshpassNotFound,
}

/// Outcome of `test_connection`: what we learned about the remote machine.
#[derive(Debug, Clone)]
pub struct RemoteInfo {
    /// The remote machine's hostname.
    pub hostname: String,
    /// OS + arch, e.g. "Darwin arm64" or "Linux x86_64".
    pub os_arch: String,
    /// Whether Docker is installed and the daemon is reachable.
    pub docker_ready: bool,
}

// ── Helpers ────────────────────────────────────────────────────────────

/// Builds a `Command` for `ssh` with common flags (no strict host key
/// checking on first connect, batch mode when using keys, port).
fn ssh_command(creds: &SshCreds) -> Command {
    let mut cmd = Command::new("ssh");
    cmd.arg("-o").arg("StrictHostKeyChecking=accept-new")
       .arg("-o").arg("ConnectTimeout=10");
    if creds.password.is_none() {
        // Key-based: use batch mode so it fails fast instead of falling
        // back to an interactive password prompt.
        cmd.arg("-o").arg("BatchMode=yes");
    }
    if creds.port != 22 {
        cmd.arg("-p").arg(creds.port.to_string());
    }
    cmd.arg(format!("{}@{}", creds.user, creds.host));
    cmd
}

/// Runs a command on the remote machine via SSH. Returns stdout on success,
/// or stderr + status on failure.
fn ssh_exec(creds: &SshCreds, remote_cmd: &str) -> SshResult {
    if let Some(ref password) = creds.password {
        ssh_exec_with_password(password, creds, remote_cmd)
    } else {
        ssh_exec_key(creds, remote_cmd)
    }
}

fn ssh_exec_key(creds: &SshCreds, remote_cmd: &str) -> SshResult {
    let output = ssh_command(creds).arg(remote_cmd).output();
    process_output(output)
}

fn ssh_exec_with_password(password: &str, creds: &SshCreds, remote_cmd: &str) -> SshResult {
    // Check if sshpass is available.
    let which = Command::new("which").arg("sshpass").output();
    match which {
        Ok(o) if o.status.success() => {}
        _ => return SshResult::SshpassNotFound,
    }

    let mut cmd = Command::new("sshpass");
    cmd.arg("-p").arg(password);
    // Inherit the SSH args by passing `ssh` as the command to run.
    let ssh_args = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
    ];
    cmd.args(ssh_args);
    if creds.port != 22 {
        cmd.arg("-p").arg(creds.port.to_string());
    }
    cmd.arg(format!("{}@{}", creds.user, creds.host));
    cmd.arg(remote_cmd);

    let output = cmd.output();
    process_output(output)
}

fn process_output(output: std::io::Result<std::process::Output>) -> SshResult {
    match output {
        Ok(out) if out.status.success() => {
            SshResult::Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
        }
        Ok(out) => {
            let stderr = String::from_utf8_lossy(&out.stderr);
            let stdout = String::from_utf8_lossy(&out.stdout);
            let mut msg = if stderr.is_empty() { stdout.into_owned() } else { stderr.into_owned() };
            if msg.is_empty() {
                msg = format!("ssh exited with status {}", out.status);
            }
            SshResult::Failed { output: msg }
        }
        Err(e) => SshResult::Failed {
            output: format!("failed to run ssh: {e}"),
        },
    }
}

// ── Public API ─────────────────────────────────────────────────────────

/// Test whether we can reach and authenticate with the remote machine.
/// Also gathers basic info: hostname, OS/arch, and whether Docker is
/// available.
pub fn test_connection(creds: &SshCreds) -> SshResult {
    let script = "echo HOSTNAME=$(hostname); echo OS_ARCH=$(uname) $(uname -m); docker info >/dev/null 2>&1 && echo DOCKER_READY=yes || echo DOCKER_READY=no";
    ssh_exec(creds, script)
}

/// Parse the output of `test_connection` into a `RemoteInfo`.
pub fn parse_remote_info(output: &str) -> Option<RemoteInfo> {
    let mut hostname = String::new();
    let mut os_arch = String::new();
    let mut docker_ready = false;

    for line in output.lines() {
        if let Some(v) = line.strip_prefix("HOSTNAME=") {
            hostname = v.to_string();
        } else if let Some(v) = line.strip_prefix("OS_ARCH=") {
            os_arch = v.to_string();
        } else if line == "DOCKER_READY=yes" {
            docker_ready = true;
        }
    }

    if hostname.is_empty() || os_arch.is_empty() {
        return None;
    }
    Some(RemoteInfo {
        hostname,
        os_arch,
        docker_ready,
    })
}

/// Run a command on the remote and return its stdout. Used for arbitrary
/// short-lived commands like `mkdir`, `docker compose up`, etc.
pub fn remote_exec(creds: &SshCreds, cmd: &str) -> SshResult {
    ssh_exec(creds, cmd)
}

/// Copy a local file to the remote machine via `scp`.
pub fn remote_copy(creds: &SshCreds, local_path: &str, remote_path: &str) -> SshResult {
    let target = if creds.port == 22 {
        format!("{}@{}:{}", creds.user, creds.host, remote_path)
    } else {
        format!("-P {} {}@{}:{}", creds.port, creds.user, creds.host, remote_path)
    };

    if creds.password.is_some() {
        let which = Command::new("which").arg("sshpass").output();
        match which {
            Ok(o) if o.status.success() => {}
            _ => return SshResult::SshpassNotFound,
        }

        let mut cmd = Command::new("sshpass");
        cmd.arg("-p").arg(creds.password.as_ref().unwrap());
        cmd.args([
            "scp",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
        ]);
        if creds.port != 22 {
            cmd.arg("-P").arg(creds.port.to_string());
        }
        cmd.arg(local_path).arg(target);
        process_output(cmd.output())
    } else {
        let mut cmd = Command::new("scp");
        cmd.arg("-o").arg("StrictHostKeyChecking=accept-new")
           .arg("-o").arg("ConnectTimeout=10");
        if creds.port != 22 {
            cmd.arg("-P").arg(creds.port.to_string());
        }
        cmd.arg(local_path).arg(target);
        process_output(cmd.output())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_remote_info_from_successful_test_output() {
        let output = "HOSTNAME=compute-1\nOS_ARCH=Linux x86_64\nDOCKER_READY=yes";
        let info = parse_remote_info(output).unwrap();
        assert_eq!(info.hostname, "compute-1");
        assert_eq!(info.os_arch, "Linux x86_64");
        assert!(info.docker_ready);
    }

    #[test]
    fn parse_remote_info_without_docker() {
        let output = "HOSTNAME=node2\nOS_ARCH=Darwin arm64\nDOCKER_READY=no";
        let info = parse_remote_info(output).unwrap();
        assert_eq!(info.hostname, "node2");
        assert!(!info.docker_ready);
    }

    #[test]
    fn parse_remote_info_missing_fields_returns_none() {
        assert!(parse_remote_info("HOSTNAME=foo").is_none());
        assert!(parse_remote_info("OS_ARCH=bar").is_none());
        assert!(parse_remote_info("").is_none());
    }

    #[test]
    fn connection_string_with_default_port() {
        let creds = SshCreds {
            host: "192.168.1.50".into(),
            user: "bioflow".into(),
            password: None,
            port: 22,
        };
        assert_eq!(creds.connection_string(), "bioflow@192.168.1.50");
    }

    #[test]
    fn connection_string_with_custom_port() {
        let creds = SshCreds {
            host: "node.local".into(),
            user: "root".into(),
            password: None,
            port: 2222,
        };
        assert_eq!(creds.connection_string(), "root@node.local:2222");
    }
}
