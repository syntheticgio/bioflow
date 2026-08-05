//! First-run prefetch of optional pipeline tools -- task 9 of
//! docs/superpowers/plans/2026-08-05-optional-tool-delivery.md, closing
//! issue #40.
//!
//! The list of optional tools is deliberately not hardcoded here (#40's
//! first acceptance criterion, and the reason this was cut from #28 in the
//! first place): `GET /pipelines/tools` on the just-started stack is the one
//! source of truth, built from `TOOL_META` in `backend/app/pipelines/
//! tools.py`, which changes as tools are added. Hardcoding a list in the
//! launcher would recreate exactly the drift problem that made the launcher
//! ship `docker-compose.yml` verbatim instead of generating it.
//!
//! The actual download does **not** go through the stack's own
//! `POST /pipelines/tools/{name}/install` endpoint, even though that is
//! what the Settings > Tools page (task 5) uses. That endpoint requires a
//! resolved profile (`OwnerDep`), and on a genuinely fresh install no
//! profile exists yet -- profile creation happens inside the web app's own
//! onboarding, which the launcher never drives and has no business driving
//! on the user's behalf. So this module fetches the *manifest* over HTTP
//! (`GET /pipelines/tools` is deliberately unscoped -- it describes the
//! image, not anyone's library) and then pulls the chosen images directly
//! with `DockerBackend::pull_image`, the same way every other Docker action
//! in this launcher works. The tradeoff: a launcher-initiated pull creates
//! no queue job and no Activity-tab entry, only the image landing in the
//! shared Docker daemon -- which is what makes it read as "already
//! installed" the moment a profile does exist and someone opens
//! Settings > Tools.

use std::time::Duration;

use serde::{Deserialize, Serialize};

/// One row of `GET /pipelines/tools`, reduced to what the prefetch screen
/// needs. `#[serde(rename_all = "snake_case")]` on the fields we read
/// matches the backend's own JSON key names (`tool_with_meta` in
/// `pipelines/tools.py`) without needing a manual rename per field.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct OptionalTool {
    pub name: String,
    pub image: Option<String>,
    pub download_bytes: Option<u64>,
    pub available: bool,
}

#[derive(Debug, Deserialize)]
struct ToolsResponse {
    tools: Vec<ToolRow>,
}

#[derive(Debug, Deserialize)]
struct ToolRow {
    name: String,
    delivery: String,
    image: Option<String>,
    download_bytes: Option<u64>,
    available: bool,
}

/// The seam between this module's filtering and the network. A real
/// implementation hits the just-started stack's own API; a fake lets the
/// prefetch screen's logic be tested without a running stack.
pub trait ToolsClient {
    /// The full `GET /pipelines/tools` response, or `None` if it could not
    /// be reached or parsed. `None` collapses every failure mode the same
    /// way `RegistryClient::remote_digest` does in `update_check.rs` --
    /// offline, a slow stack, a malformed response -- because the caller
    /// cannot tell them apart and must not try to; the prefetch screen's
    /// only correct response to `None` is to skip itself, not show an error
    /// for a step that was always optional.
    fn list_tools(&self, port: u16) -> Option<Vec<OptionalTool>>;
}

/// A real `ToolsClient` reaching the just-started stack through its own
/// published port -- `localhost:{port}/api/v1/pipelines/tools`, the same
/// path the web frontend's own `api.pipelineTools()` calls, proxied by the
/// `web` service's nginx to the `api` service (see `frontend/nginx.conf`'s
/// `location /api/`). This is the one place in the launcher that talks HTTP
/// to the stack itself rather than shelling out to `docker` -- the health
/// check deliberately avoids this (see `ShellDocker::health`'s own comment
/// on why), but there is no `docker compose` equivalent of "what does the
/// API's tools endpoint say," so this seam is unavoidable for exactly this
/// one read.
pub struct StackToolsClient {
    timeout: Duration,
}

impl StackToolsClient {
    pub fn new(timeout: Duration) -> Self {
        Self { timeout }
    }
}

impl Default for StackToolsClient {
    fn default() -> Self {
        // Generous relative to GhcrClient's 3s: this call happens once,
        // immediately after the stack has already proven itself healthy
        // (RunOutcome::Running), not on a poll loop, so a slightly slower
        // response here costs nothing repeatedly the way a registry check
        // would.
        Self::new(Duration::from_secs(5))
    }
}

impl ToolsClient for StackToolsClient {
    fn list_tools(&self, port: u16) -> Option<Vec<OptionalTool>> {
        let agent = ureq::AgentBuilder::new().timeout(self.timeout).build();
        let url = format!("http://localhost:{port}/api/v1/pipelines/tools");
        let response = agent.get(&url).call().ok()?;
        let body: ToolsResponse = response.into_json().ok()?;
        Some(on_demand_tools(body.tools))
    }
}

fn on_demand_tools(rows: Vec<ToolRow>) -> Vec<OptionalTool> {
    rows.into_iter()
        .filter(|t| t.delivery == "on_demand")
        .map(|t| OptionalTool {
            name: t.name,
            image: t.image,
            download_bytes: t.download_bytes,
            available: t.available,
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(name: &str, delivery: &str, available: bool) -> ToolRow {
        ToolRow {
            name: name.to_string(),
            delivery: delivery.to_string(),
            image: Some(format!("example.invalid/{name}:latest")),
            download_bytes: Some(1_000_000_000),
            available,
        }
    }

    #[test]
    fn filters_to_on_demand_tools_only() {
        let rows = vec![
            row("fastp", "bundled", true),
            row("deepvariant", "on_demand", false),
            row("samtools", "bundled", true),
        ];

        let result = on_demand_tools(rows);

        assert_eq!(result.len(), 1);
        assert_eq!(result[0].name, "deepvariant");
    }

    #[test]
    fn an_empty_response_yields_no_tools() {
        assert!(on_demand_tools(Vec::new()).is_empty());
    }

    #[test]
    fn an_installed_on_demand_tool_still_appears_with_available_true() {
        // The prefetch screen needs to know a tool is already installed so
        // it can skip offering it, not just which tools exist at all.
        let rows = vec![row("deepvariant", "on_demand", true)];

        let result = on_demand_tools(rows);

        assert_eq!(result.len(), 1);
        assert!(result[0].available);
    }

    #[test]
    fn every_bundled_tool_is_excluded_even_when_unavailable() {
        // A bundled tool that failed its probe is still not something this
        // screen offers to "install" -- there is no image for it to pull.
        let rows = vec![row("clair3", "bundled", false)];

        assert!(on_demand_tools(rows).is_empty());
    }
}
