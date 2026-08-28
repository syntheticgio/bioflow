//! The Release notes dialog's data source -- the GitHub Releases API for
//! this repository, read anonymously.
//!
//! The notes themselves are already written: `.github/release.yml`
//! generates each release body from merged PR titles at tag time (see
//! CLAUDE.md, "Release notes come from PR titles"), so this module never
//! composes prose. It fetches what is published, decides which release the
//! running stack corresponds to, and hands both to the UI.
//!
//! Like `update_check`, every failure mode collapses to "nothing to show"
//! rather than an error: the repository is public, so the call needs no
//! credentials, but being offline is an ordinary state for a desktop
//! launcher and must not surface as a fault. The UI renders a link to
//! GitHub in that case.

use std::time::Duration;

/// Tags matching this prefix are the launcher's *own* releases
/// (`launcher-v0.2.0`), published from the same repository as BioFlow's.
/// They carry launcher bundles, not the app's release notes, and the
/// version the dialog reasons about is always BioFlow's -- so they are
/// filtered out rather than offered as choices the user cannot map to
/// anything in Settings.
const LAUNCHER_TAG_PREFIX: &str = "launcher-v";

/// One published release, as the dialog needs it. Field names are
/// camelCase over IPC to match the rest of the launcher's DTOs.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Release {
    /// The git tag, `v`-prefixed as GitHub reports it: `v0.6.0-beta`.
    pub tag: String,
    /// The release's display title: "BioFlow 0.6.0-beta".
    pub name: String,
    /// ISO-8601 as GitHub returns it; the UI formats it for display.
    pub published_at: String,
    /// The release body, GitHub-flavoured markdown.
    pub body: String,
    /// Whether GitHub marks this an alpha/beta pre-release.
    pub prerelease: bool,
}

/// The seam between this module's selection logic and the network, mirroring
/// `update_check::RegistryClient`. A real implementation hits the GitHub
/// Releases API; a fake lets every decision below be tested without a
/// network call.
pub trait ReleaseNotesSource {
    /// Published releases, newest first, or an empty `Vec` when the call
    /// could not complete for any reason (offline, DNS failure, timeout,
    /// non-2xx, malformed body). Every failure collapses to empty on
    /// purpose -- the caller cannot tell network trouble from "nothing
    /// published," and must not try to.
    fn list_releases(&self) -> Vec<Release>;
}

/// Drops the launcher's own releases and anything GitHub still marks a
/// draft, preserving the input order (GitHub returns newest first, which is
/// the order the dropdown wants).
pub fn bioflow_releases(releases: &[Release]) -> Vec<Release> {
    releases
        .iter()
        .filter(|r| !r.tag.starts_with(LAUNCHER_TAG_PREFIX))
        .cloned()
        .collect()
}

/// Which release the running stack corresponds to -- the one the dialog
/// opens on.
///
/// The launcher knows its stack only as a `BIOFLOW_TAG` plus an optional
/// developer repo, so the mapping is:
///
/// - **Developer** (`developer_repo` set): locally-built images have no
///   published release at all. Falls back to the newest release of any
///   kind, so the dialog still opens on something current rather than
///   empty. Checked first, so a hand-edited `.env` carrying both lines
///   resolves the way `current_settings` resolves it.
/// - **Release** (`latest`): the newest *stable* release, matching what
///   `:latest` points at. A pre-release is never the answer here even when
///   it is newer -- `:latest` does not move to an alpha.
/// - **Alpha/Beta** (a pinned stage tag): the release whose tag is that tag
///   `v`-prefixed. `None` if it was never published under that name.
///
/// `None` also covers "nothing published yet", leaving the dialog to open
/// on whatever the list's first entry is.
pub fn release_for_stack<'a>(
    releases: &'a [Release],
    bioflow_tag: &str,
    developer_repo: Option<&str>,
) -> Option<&'a Release> {
    if developer_repo.is_some() {
        return releases.first();
    }
    if bioflow_tag == "latest" {
        return releases.iter().find(|r| !r.prerelease);
    }
    let wanted = format!("v{bioflow_tag}");
    releases.iter().find(|r| r.tag == wanted)
}

/// A real `ReleaseNotesSource` for GitHub's REST API. The repository is
/// public, so the list endpoint needs no token -- unlike GHCR, which
/// requires an anonymous bearer exchange even for a public read.
pub struct GitHubReleasesClient {
    repo: String,
    timeout: Duration,
    /// How many releases to fetch. The dialog offers a history dropdown,
    /// not an archive: a page of 30 covers well over a year of this
    /// project's cadence, and paginating would trade a real cost (more
    /// requests on every open) for entries nobody scrolls to.
    per_page: u32,
}

impl GitHubReleasesClient {
    pub fn new(repo: &str, timeout: Duration) -> Self {
        Self {
            repo: repo.to_string(),
            timeout,
            per_page: 30,
        }
    }
}

impl Default for GitHubReleasesClient {
    /// The same short timeout `update_check::GhcrClient` uses, for the same
    /// reason: a slow API is treated exactly like an unreachable one.
    fn default() -> Self {
        Self::new("syntheticgio/bioflow", Duration::from_secs(3))
    }
}

impl ReleaseNotesSource for GitHubReleasesClient {
    fn list_releases(&self) -> Vec<Release> {
        let agent = ureq::AgentBuilder::new().timeout(self.timeout).build();
        let url = format!(
            "https://api.github.com/repos/{}/releases?per_page={}",
            self.repo, self.per_page
        );

        // GitHub rejects requests with no User-Agent outright, and serves
        // the stable v3 schema only when asked for it by name.
        let response = match agent
            .get(&url)
            .set("User-Agent", "bioflow-launcher")
            .set("Accept", "application/vnd.github+json")
            .call()
        {
            Ok(r) => r,
            Err(_) => return Vec::new(),
        };
        let body: serde_json::Value = match response.into_json() {
            Ok(v) => v,
            Err(_) => return Vec::new(),
        };
        let Some(entries) = body.as_array() else {
            return Vec::new();
        };

        entries
            .iter()
            .filter(|e| !e.get("draft").and_then(|d| d.as_bool()).unwrap_or(false))
            .filter_map(parse_release)
            .collect()
    }
}

/// Reads one API entry into a `Release`, skipping it when a field the
/// dialog cannot do without is absent or the wrong type. `body` is the
/// exception: GitHub returns `null` for a release published with no notes,
/// which is a real (if unhelpful) release, not a malformed one.
fn parse_release(entry: &serde_json::Value) -> Option<Release> {
    let tag = entry.get("tag_name")?.as_str()?.to_string();
    let name = entry
        .get("name")
        .and_then(|n| n.as_str())
        .filter(|n| !n.is_empty())
        .unwrap_or(&tag)
        .to_string();
    Some(Release {
        published_at: entry
            .get("published_at")
            .and_then(|p| p.as_str())
            .unwrap_or_default()
            .to_string(),
        body: entry
            .get("body")
            .and_then(|b| b.as_str())
            .unwrap_or_default()
            .to_string(),
        prerelease: entry
            .get("prerelease")
            .and_then(|p| p.as_bool())
            .unwrap_or(false),
        tag,
        name,
    })
}

/// Fetches and filters in one call -- what the Tauri command wants.
pub fn list_release_notes<S: ReleaseNotesSource>(source: &S) -> Vec<Release> {
    bioflow_releases(&source.list_releases())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn release(tag: &str, prerelease: bool) -> Release {
        Release {
            tag: tag.to_string(),
            name: format!("BioFlow {}", tag.trim_start_matches('v')),
            published_at: "2026-08-26T14:42:58Z".to_string(),
            body: format!("notes for {tag}"),
            prerelease,
        }
    }

    /// Newest first, the order the API returns and the dropdown wants.
    fn sample() -> Vec<Release> {
        vec![
            release("v0.6.0-beta", true),
            release("v0.6.0-alpha", true),
            release("v0.5.1", false),
            release("launcher-v0.2.0", false),
            release("v0.4.0", false),
        ]
    }

    struct FakeSource(Vec<Release>);
    impl ReleaseNotesSource for FakeSource {
        fn list_releases(&self) -> Vec<Release> {
            self.0.clone()
        }
    }

    // ── bioflow_releases ─────────────────────────────────────────────────

    #[test]
    fn launcher_releases_are_not_offered_as_choices() {
        let kept = bioflow_releases(&sample());
        assert!(
            !kept.iter().any(|r| r.tag.starts_with("launcher-v")),
            "launcher's own releases must not appear: {:?}",
            kept.iter().map(|r| &r.tag).collect::<Vec<_>>()
        );
        assert_eq!(kept.len(), 4);
    }

    #[test]
    fn filtering_preserves_newest_first_order() {
        let kept = bioflow_releases(&sample());
        let tags: Vec<&str> = kept.iter().map(|r| r.tag.as_str()).collect();
        assert_eq!(tags, vec!["v0.6.0-beta", "v0.6.0-alpha", "v0.5.1", "v0.4.0"]);
    }

    #[test]
    fn nothing_published_yields_no_choices() {
        assert!(bioflow_releases(&[]).is_empty());
    }

    // ── release_for_stack ────────────────────────────────────────────────

    #[test]
    fn release_mode_opens_on_the_newest_stable_not_a_newer_prerelease() {
        // The whole point of the mapping: 0.6.0-beta is newer, but `:latest`
        // points at 0.5.1, and that is what the user is running.
        let releases = bioflow_releases(&sample());
        let found = release_for_stack(&releases, "latest", None);
        assert_eq!(found.map(|r| r.tag.as_str()), Some("v0.5.1"));
    }

    #[test]
    fn a_pinned_beta_opens_on_that_exact_release() {
        let releases = bioflow_releases(&sample());
        let found = release_for_stack(&releases, "0.6.0-beta", None);
        assert_eq!(found.map(|r| r.tag.as_str()), Some("v0.6.0-beta"));
    }

    #[test]
    fn a_pinned_alpha_opens_on_that_exact_release() {
        let releases = bioflow_releases(&sample());
        let found = release_for_stack(&releases, "0.6.0-alpha", None);
        assert_eq!(found.map(|r| r.tag.as_str()), Some("v0.6.0-alpha"));
    }

    #[test]
    fn a_pinned_tag_with_no_published_release_selects_nothing() {
        // An image tag can exist on GHCR before its GitHub release is
        // published; the dialog falls back to the newest entry rather than
        // showing another version's notes under this one's name.
        let releases = bioflow_releases(&sample());
        assert_eq!(release_for_stack(&releases, "0.9.0-alpha", None), None);
    }

    #[test]
    fn developer_mode_opens_on_the_newest_release_of_any_kind() {
        // Locally-built images have no published release; the newest entry
        // is the most useful thing to show rather than an empty dialog.
        let releases = bioflow_releases(&sample());
        let found = release_for_stack(&releases, "latest", Some("/home/me/bioflow"));
        assert_eq!(found.map(|r| r.tag.as_str()), Some("v0.6.0-beta"));
    }

    #[test]
    fn developer_mode_wins_when_env_somehow_carries_both() {
        // .env is hand-editable, and current_settings resolves the pair with
        // developer taking precedence; match it rather than inventing a
        // second answer. Mirrors update_check's test of the same name.
        let releases = bioflow_releases(&sample());
        let found = release_for_stack(&releases, "0.5.1", Some("/home/me/bioflow"));
        assert_eq!(found.map(|r| r.tag.as_str()), Some("v0.6.0-beta"));
    }

    #[test]
    fn release_mode_with_only_prereleases_published_selects_nothing() {
        let only_pre = vec![release("v0.6.0-alpha", true)];
        assert_eq!(release_for_stack(&only_pre, "latest", None), None);
    }

    #[test]
    fn nothing_published_selects_nothing_in_every_mode() {
        assert_eq!(release_for_stack(&[], "latest", None), None);
        assert_eq!(release_for_stack(&[], "0.6.0-beta", None), None);
        assert_eq!(release_for_stack(&[], "latest", Some("/repo")), None);
    }

    // ── parse_release ────────────────────────────────────────────────────

    #[test]
    fn parses_a_real_api_entry() {
        let entry = serde_json::json!({
            "tag_name": "v0.6.0-beta",
            "name": "BioFlow 0.6.0-beta",
            "published_at": "2026-08-26T14:42:58Z",
            "body": "## What's Changed\n* feat: a thing",
            "prerelease": true,
            "draft": false,
        });
        let parsed = parse_release(&entry).expect("a well-formed entry parses");
        assert_eq!(parsed.tag, "v0.6.0-beta");
        assert_eq!(parsed.name, "BioFlow 0.6.0-beta");
        assert!(parsed.prerelease);
        assert!(parsed.body.contains("feat: a thing"));
    }

    #[test]
    fn a_release_published_with_no_notes_is_still_a_release() {
        // GitHub returns body: null for a release created without notes.
        // That is unhelpful, not malformed -- dropping it would hide a
        // version from the dropdown entirely.
        let entry = serde_json::json!({
            "tag_name": "v0.4.0",
            "name": "BioFlow 0.4.0",
            "published_at": "2026-08-12T17:18:35Z",
            "body": serde_json::Value::Null,
            "prerelease": false,
        });
        let parsed = parse_release(&entry).expect("a bodyless release still parses");
        assert_eq!(parsed.tag, "v0.4.0");
        assert_eq!(parsed.body, "");
    }

    #[test]
    fn an_entry_with_no_tag_is_skipped_not_guessed_at() {
        let entry = serde_json::json!({ "name": "mystery", "body": "..." });
        assert_eq!(parse_release(&entry), None);
    }

    #[test]
    fn an_unnamed_release_falls_back_to_its_tag() {
        let entry = serde_json::json!({
            "tag_name": "v0.4.0",
            "name": "",
            "body": "notes",
            "prerelease": false,
        });
        let parsed = parse_release(&entry).expect("an unnamed release parses");
        assert_eq!(parsed.name, "v0.4.0");
    }

    // ── list_release_notes ───────────────────────────────────────────────

    #[test]
    fn an_unreachable_api_yields_no_notes_not_an_error() {
        let source = FakeSource(Vec::new());
        assert!(list_release_notes(&source).is_empty());
    }

    #[test]
    fn list_release_notes_filters_the_launchers_own_releases() {
        let source = FakeSource(sample());
        let notes = list_release_notes(&source);
        assert_eq!(notes.len(), 4);
        assert!(!notes.iter().any(|r| r.tag.starts_with("launcher-v")));
    }

    /// Hits the real GitHub API to prove this client's HTTP mechanics (URL
    /// shape, the User-Agent GitHub insists on, the JSON body shape) work
    /// against the live service, not only against FakeSource. Ignored by
    /// default so ordinary `cargo test` runs need no network; run with
    /// `cargo test -- --ignored`. Mirrors update_check.rs's real-registry
    /// tests.
    #[test]
    #[ignore]
    fn github_client_lists_real_releases() {
        let client = GitHubReleasesClient::default();
        let releases = client.list_releases();
        assert!(
            !releases.is_empty(),
            "expected at least one published release from the real API"
        );
        assert!(
            releases.iter().any(|r| r.tag.starts_with('v')),
            "expected a v-prefixed BioFlow tag, got {:?}",
            releases.iter().map(|r| &r.tag).collect::<Vec<_>>()
        );
    }
}
