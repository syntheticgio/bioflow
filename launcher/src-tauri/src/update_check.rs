//! The cheap registry manifest check behind the Update button -- see the
//! design spec's "Update checking" section.
//!
//! `docker compose up` only pulls an image when it is absent locally, so
//! once `:latest` is cached it is reused indefinitely and never
//! self-updates. The launcher therefore never downloads images unasked (an
//! explicit Update click is the only path to `docker compose pull`, in
//! `actions::update`); what this module adds is a few kilobytes of registry
//! metadata on launch, purely to decide whether the Update button should
//! even appear. That is a categorically different thing from a multi-
//! gigabyte pull, which is why only the pull needs the user's consent and
//! this check does not.
//!
//! Non-blocking and fails silently: a hung or slow registry must never delay
//! the window, and being offline is not an error state here, just "no update
//! to offer."

use std::time::Duration;

/// The seam between this module's comparison logic and the network. A real
/// implementation hits the registry's OCI Distribution API; a fake lets the
/// comparison logic be tested without a network call at all.
pub trait RegistryClient {
    /// The registry's current digest for `image:tag`, or `None` if the
    /// check could not complete for any reason (offline, DNS failure,
    /// timeout, non-2xx response, malformed header). Every failure mode
    /// collapses to `None` on purpose -- the caller cannot tell network
    /// trouble from "no update," and per the spec it must not try to.
    fn remote_digest(&self, image: &str, tag: &str) -> Option<String>;

    /// The set of tags the registry reports for `image`, or an empty `Vec`
    /// when the call could not complete for any reason (offline, DNS failure,
    /// timeout, non-2xx, malformed body). Every failure mode collapses to
    /// empty on purpose -- the only caller uses this to populate a dropdown,
    /// and a registry hiccup must not surface as an error to the user, just
    /// as "no options to offer."
    fn tags_for(&self, image: &str) -> Vec<String>;
}

/// The local seam: asks the Docker daemon (already running, so this is a
/// local call, not a network one) what digest the cached `:latest` actually
/// has.
pub trait LocalImageInspector {
    fn local_digest(&self, image: &str, tag: &str) -> Option<String>;
}

/// `None` here means "no update to offer" -- either because the check could
/// not complete, or because there is genuinely no newer image. The caller
/// (the UI) shows the Update button only on `Some(true)`.
pub fn update_available<R: RegistryClient, L: LocalImageInspector>(
    registry: &R,
    local: &L,
    image: &str,
    tag: &str,
) -> Option<bool> {
    let remote = registry.remote_digest(image, tag)?;
    let local_digest = local.local_digest(image, tag)?;
    Some(remote != local_digest)
}

/// Which tag, if any, an update check should compare against -- the single
/// rule behind "Release tracks a moving `latest` and gets an update check;
/// every other mode is pinned or local and does not."
///
/// `None` means no check is meaningful for this mode, and the caller must
/// skip the registry call entirely rather than falling back to `"latest"`:
///
/// - **Developer** (`developer_repo` set): the stack runs locally-built
///   `:local` images. There is no registry counterpart, so any comparison is
///   against an unrelated image. Checked first, so a hand-edited `.env`
///   carrying both lines resolves the same way `current_settings` does.
/// - **Alpha/Beta** (a pinned stage tag): stage tags are immutable --
///   `classify_version_options` picks the highest `X.Y.Z-alpha` published --
///   so a digest check against the user's own pinned tag can only fire if a
///   tag were re-published under the same name, which the release process
///   does not do. Noticing that a *newer* stage tag exists is a different
///   mechanism (tag-list comparison) and a separate feature; see #324.
/// - **Release** (`latest`): the only moving target, and the only mode where
///   a digest check answers a real question.
pub fn checkable_tag(bioflow_tag: &str, developer_repo: Option<&str>) -> Option<String> {
    if developer_repo.is_some() {
        return None;
    }
    if bioflow_tag == "latest" {
        return Some("latest".to_string());
    }
    None
}

/// Replaces the `BIOFLOW_TAG=` line in `.env` content with a new value,
/// preserving all other lines and their ordering. Appends the line if not
/// found as a safety net.
fn set_bioflow_tag(contents: &str, new_tag: &str) -> String {
    let mut found = false;
    let result: Vec<String> = contents
        .lines()
        .map(|line| {
            if line.starts_with("BIOFLOW_TAG=") {
                found = true;
                format!("BIOFLOW_TAG={}", new_tag)
            } else {
                line.to_string()
            }
        })
        .collect();

    if !found {
        let mut result = result;
        result.push(format!("BIOFLOW_TAG={}", new_tag));
        result.join("\n")
    } else {
        result.join("\n")
    }
}

/// Compares the user's current pinned tag against available version options
/// and returns the best forward-compatible stage tag (if any).
///
/// "Forward" means a strictly greater (major, minor, patch, stage_rank) tuple,
/// where stage rank is alpha=0, beta=1. Release mode ("latest") is excluded —
/// it has its own digest-based update path.
///
/// Returns `None` when no forward-compatible tag exists or the current tag
/// cannot be parsed as a stage tag.
pub fn check_stage_update(current_tag: &str, options: &VersionOptions) -> Option<String> {
    if current_tag == "latest" {
        return None;
    }

    let current_ver = version_tuple(current_tag, "alpha")
        .map(|v| (v, 0u8))
        .or_else(|| version_tuple(current_tag, "beta").map(|v| (v, 1u8)))?;

    let candidates = [options.alpha.as_deref(), options.beta.as_deref()];

    candidates
        .into_iter()
        .flatten()
        .filter_map(|candidate| {
            let cv = version_tuple(candidate, "alpha")
                .map(|v| (v, 0u8))
                .or_else(|| version_tuple(candidate, "beta").map(|v| (v, 1u8)))?;
            if (cv.0, cv.1) > current_ver {
                Some((candidate.to_string(), cv))
            } else {
                None
            }
        })
        .max_by_key(|(_, (v, rank))| (*v, *rank))
        .map(|(tag, _)| tag)
}

/// The version choices the Settings dialog offers for `BIOFLOW_TAG`.
///
/// `release` is always present (resolves to the `:latest` tag the published
/// images carry) and is the dropdown's default. `alpha`/`beta` are `Some` only
/// when the registry actually reports a tag for that stage -- a stage with no
/// published image yet renders as a disabled option rather than a phantom
/// tag. `latest` itself is never emitted as a choice here; "Release" maps to
/// it implicitly.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct VersionOptions {
    pub release: String,
    pub alpha: Option<String>,
    pub beta: Option<String>,
}

impl VersionOptions {
    /// No network available (or the registry could not be reached) -- just the
    /// always-present release option. The UI falls back to this so the
    /// dropdown never goes gray; only its pre-release rows disable.
    pub fn offline() -> Self {
        Self {
            release: "latest".to_string(),
            alpha: None,
            beta: None,
        }
    }
}

/// Splits a tag like `0.3.0-alpha` into a `(major, minor, patch)` triple for
/// ordering, returning `None` unless `tag` ends in `-{suffix}` with a parseable
/// semver-ish stem before it. `sha-*` and bare `latest` never match -- they
/// are not versioned stage releases.
fn version_tuple(tag: &str, suffix: &str) -> Option<(u32, u32, u32)> {
    let stem = tag.strip_suffix(&format!("-{}", suffix))?;
    let mut parts = stem.split('.');
    let major: u32 = parts.next()?.parse().ok()?;
    let minor: u32 = parts.next()?.parse().ok()?;
    let patch: u32 = parts.next()?.parse().ok()?;
    if parts.next().is_some() {
        return None;
    }
    Some((major, minor, patch))
}

/// Classifies a registry tag list into `VersionOptions`. Pure and
/// side-effect-free so the classification logic is testable without hitting
/// GHCR -- the `tags_for` call that produced the list is exercised by the
/// ignored real-registry test below.
pub fn classify_version_options(tags: &[String]) -> VersionOptions {
    let pick_stage = |suffix: &str| -> Option<String> {
        tags.iter()
            .filter_map(|t| version_tuple(t, suffix).map(|v| (v, t)))
            .max_by_key(|(v, _)| *v)
            .map(|(_, t)| t.clone())
    };

    VersionOptions {
        release: "latest".to_string(),
        alpha: pick_stage("alpha"),
        beta: pick_stage("beta"),
    }
}

/// Builds the dropdown choices for a given image by fetching its tag list
/// from the registry and classifying it. The tag fetch is delegated to
/// `RegistryClient::tags_for`, keeping this pure classification in
/// `classify_version_options` so it is testable without network.
pub fn list_version_options<R: RegistryClient>(registry: &R, image: &str) -> VersionOptions {
    let tags = registry.tags_for(image);
    classify_version_options(&tags)
}

/// A real `RegistryClient` for GHCR (`ghcr.io`), the registry named in the
/// design spec's "Changes required in this repository" section
/// (`ghcr.io/syntheticgio/bioflow-{backend,web}`). Talks the plain OCI
/// Distribution v2 API -- `HEAD /v2/<name>/manifests/<tag>` -- which needs no
/// authentication for a public image and no Docker CLI experimental flags,
/// unlike `docker manifest inspect`.
pub struct GhcrClient {
    timeout: Duration,
}

impl GhcrClient {
    /// A short timeout is the point: this check must never delay the
    /// window, so a registry that does not answer promptly is treated
    /// exactly like one that is unreachable.
    pub fn new(timeout: Duration) -> Self {
        Self { timeout }
    }
}

impl Default for GhcrClient {
    fn default() -> Self {
        Self::new(Duration::from_secs(3))
    }
}

impl RegistryClient for GhcrClient {
    fn remote_digest(&self, image: &str, tag: &str) -> Option<String> {
        let agent = ureq::AgentBuilder::new().timeout(self.timeout).build();

        // GHCR requires a bearer token even for a public, anonymous read --
        // unlike Docker Hub, there is no unauthenticated path to the
        // manifest endpoint. The token exchange itself needs no
        // credentials; it is standard OCI Distribution Spec auth, not a
        // BioFlow-specific login.
        let token_url = format!(
            "https://ghcr.io/token?service=ghcr.io&scope=repository:{image}:pull"
        );
        let token_response = agent.get(&token_url).call().ok()?;
        let token_body: serde_json::Value = token_response.into_json().ok()?;
        let token = token_body.get("token")?.as_str()?;

        let manifest_url = format!("https://ghcr.io/v2/{image}/manifests/{tag}");
        let response = agent
            .head(&manifest_url)
            .set("Authorization", &format!("Bearer {token}"))
            .set(
                "Accept",
                "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
            )
            .call()
            .ok()?;
        response.header("Docker-Content-Digest").map(str::to_string)
    }

    fn tags_for(&self, image: &str) -> Vec<String> {
        let agent = ureq::AgentBuilder::new().timeout(self.timeout).build();

        // GHCR requires the same anonymous bearer token that `remote_digest`
        // fetches -- a failed exchange is the "could not complete" case this
        // method collapses to an empty list.
        let token_url = format!(
            "https://ghcr.io/token?service=ghcr.io&scope=repository:{image}:pull"
        );
        let token_response = match agent.get(&token_url).call() {
            Ok(r) => r,
            Err(_) => return Vec::new(),
        };
        let token_body: serde_json::Value = match token_response.into_json() {
            Ok(v) => v,
            Err(_) => return Vec::new(),
        };
        let token = match token_body.get("token").and_then(|t| t.as_str()) {
            Some(t) => t.to_string(),
            None => return Vec::new(),
        };

        // `GET /v2/<name>/tags/list` returns {"name": "...", "tags": [...]}.
        // 404 (image not found, or a name GHCR does not recognize) is the
        // "no tags" case here, not an error -- degrade to empty.
        let tags_url = format!("https://ghcr.io/v2/{image}/tags/list");
        let response = match agent
            .get(&tags_url)
            .set("Authorization", &format!("Bearer {token}"))
            .call()
        {
            Ok(r) => r,
            Err(_) => return Vec::new(),
        };
        if response.status() != 200 {
            return Vec::new();
        }
        let body: serde_json::Value = match response.into_json() {
            Ok(v) => v,
            Err(_) => return Vec::new(),
        };
        body.get("tags")
            .and_then(|t| t.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|t| t.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default()
    }
}

/// A real `LocalImageInspector` reading the digest Docker already recorded
/// for the cached image -- `docker image inspect`, not a network call.
pub struct DockerImageInspector;

impl LocalImageInspector for DockerImageInspector {
    fn local_digest(&self, image: &str, tag: &str) -> Option<String> {
        let output = std::process::Command::new("docker")
            .arg("image")
            .arg("inspect")
            .arg(format!("ghcr.io/{image}:{tag}"))
            .arg("--format")
            .arg("{{index .RepoDigests 0}}")
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        let text = String::from_utf8_lossy(&output.stdout);
        // RepoDigests entries look like "ghcr.io/org/name@sha256:...";
        // only the part after '@' is the digest.
        text.trim().rsplit('@').next().map(str::to_string)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    struct FakeRegistry(Option<&'static str>);
    impl RegistryClient for FakeRegistry {
        fn remote_digest(&self, _image: &str, _tag: &str) -> Option<String> {
            self.0.map(str::to_string)
        }

        fn tags_for(&self, _image: &str) -> Vec<String> {
            Vec::new()
        }
    }

    struct FakeLocal(Option<&'static str>);
    impl LocalImageInspector for FakeLocal {
        fn local_digest(&self, _image: &str, _tag: &str) -> Option<String> {
            self.0.map(str::to_string)
        }
    }

    #[test]
    fn same_digest_means_no_update() {
        let registry = FakeRegistry(Some("sha256:aaa"));
        let local = FakeLocal(Some("sha256:aaa"));
        assert_eq!(update_available(&registry, &local, "org/img", "latest"), Some(false));
    }

    #[test]
    fn different_digest_means_update_available() {
        let registry = FakeRegistry(Some("sha256:new"));
        let local = FakeLocal(Some("sha256:old"));
        assert_eq!(update_available(&registry, &local, "org/img", "latest"), Some(true));
    }

    #[test]
    fn registry_unreachable_yields_none_not_an_error() {
        let registry = FakeRegistry(None);
        let local = FakeLocal(Some("sha256:old"));
        assert_eq!(update_available(&registry, &local, "org/img", "latest"), None);
    }

    #[test]
    fn no_local_image_yields_none() {
        // Nothing cached locally yet (e.g. before the first pull ever
        // happened) -- there is no local digest to compare against, so
        // there is nothing to offer an update over.
        let registry = FakeRegistry(Some("sha256:aaa"));
        let local = FakeLocal(None);
        assert_eq!(update_available(&registry, &local, "org/img", "latest"), None);
    }

    /// A registry whose call takes arbitrarily long. This exercises that
    /// `update_available` itself does not add any waiting of its own on
    /// top of whatever the client already decided to do -- the client is
    /// solely responsible for its own timeout (GhcrClient's `timeout`
    /// field), and this function must never retry or block further.
    struct SlowRegistry {
        calls: Cell<u32>,
    }
    impl RegistryClient for SlowRegistry {
        fn remote_digest(&self, _image: &str, _tag: &str) -> Option<String> {
            self.calls.set(self.calls.get() + 1);
            None
        }

        fn tags_for(&self, _image: &str) -> Vec<String> {
            Vec::new()
        }
    }

    #[test]
    fn a_client_that_gives_up_is_called_exactly_once_never_retried() {
        let registry = SlowRegistry { calls: Cell::new(0) };
        let local = FakeLocal(Some("sha256:old"));
        let result = update_available(&registry, &local, "org/img", "latest");
        assert_eq!(result, None);
        assert_eq!(registry.calls.get(), 1);
    }

    #[test]
    fn classify_picks_the_highest_alpha_and_leaves_beta_null() {
        // A mixed bag: versioned release + a sha handle + `latest`, the
        // kind of tag set GHCR actually returns for the bioflow images.
        let tags: Vec<String> =
            vec!["0.2.6", "0.3.0-alpha", "0.1.0", "sha-3055f0e", "latest"]
                .into_iter()
                .map(|s| s.to_string())
                .collect();
        let opts = classify_version_options(&tags);
        assert_eq!(opts.release, "latest");
        assert_eq!(opts.alpha, Some("0.3.0-alpha".to_string()));
        assert_eq!(opts.beta, None);
    }

    #[test]
    fn classify_picks_the_highest_beta_when_present() {
        let tags: Vec<String> =
            vec!["0.4.0-alpha", "0.4.0-beta", "0.4.0", "0.5.0-alpha"]
                .into_iter()
                .map(|s| s.to_string())
                .collect();
        let opts = classify_version_options(&tags);
        assert_eq!(opts.alpha, Some("0.5.0-alpha".to_string()));
        assert_eq!(opts.beta, Some("0.4.0-beta".to_string()));
    }

    #[test]
    fn classify_without_any_stage_tag_disables_only_that_stage() {
        let tags: Vec<String> = vec!["0.2.6", "latest"]
            .into_iter()
            .map(|s| s.to_string())
            .collect();
        let opts = classify_version_options(&tags);
        assert_eq!(opts.release, "latest");
        assert_eq!(opts.alpha, None);
        assert_eq!(opts.beta, None);
    }

    #[test]
    fn classify_on_empty_tag_list_is_release_only() {
        let opts = classify_version_options(&[]);
        assert_eq!(opts.release, "latest");
        assert_eq!(opts.alpha, None);
        assert_eq!(opts.beta, None);
    }

    /// Hits the real GHCR registry to prove `tags_for`'s HTTP mechanics
    /// (token exchange, `GET /v2/<name>/tags/list`, JSON body shape) actually
    /// work against a real OCI registry, not only against FakeRegistry. Uses a
    /// well-known public package rather than BioFlow's own image (which does
    /// not exist yet -- blocked on #37). Ignored by default; run explicitly
    /// with `cargo test -- --ignored`.
    #[test]
    #[ignore]
    fn ghcr_client_lists_real_tags_from_a_public_image() {
        let client = GhcrClient::default();
        let tags = client.tags_for("homebrew/core/rust");
        assert!(
            !tags.is_empty(),
            "expected at least one tag from a real public GHCR image, got {tags:?}"
        );
    }

    /// Hits the real GHCR registry -- not BioFlow's own image, which does
    /// not exist yet (blocked on #37), but a well-known public one, purely
    /// to prove the HTTP mechanics (URL shape, Accept header, digest header
    /// name) actually work against a real OCI registry rather than only
    /// against FakeRegistry. Ignored by default since normal `cargo test`
    /// runs should not depend on network access; run explicitly with
    /// `cargo test -- --ignored`.
    #[test]
    #[ignore]
    fn ghcr_client_reads_a_real_digest_from_a_public_image() {
        // homebrew/core/rust is a real, versioned-tag GHCR package (no
        // `latest` tag, hence pinning one); this only proves the auth and
        // manifest-fetch mechanics work against a real registry, not
        // anything about BioFlow's own not-yet-published images.
        let client = GhcrClient::default();
        let digest = client.remote_digest("homebrew/core/rust", "1.97.1");
        assert!(
            digest.as_deref().is_some_and(|d| d.starts_with("sha256:")),
            "expected a sha256 digest from a real public GHCR image, got {digest:?}"
        );
    }

    #[test]
    fn release_is_the_only_mode_that_checks() {
        assert_eq!(checkable_tag("latest", None), Some("latest".to_string()));
    }

    #[test]
    fn developer_mode_never_checks() {
        // A local :local build has no registry counterpart to compare against.
        assert_eq!(checkable_tag("latest", Some("/home/me/bioflow")), None);
    }

    #[test]
    fn a_pinned_alpha_never_checks() {
        // Stage tags are immutable, so a digest check against the pinned tag
        // would be near-permanently silent even after 0.4.0-alpha publishes.
        assert_eq!(checkable_tag("0.3.0-alpha", None), None);
    }

    #[test]
    fn a_pinned_beta_never_checks() {
        assert_eq!(checkable_tag("0.4.0-beta", None), None);
    }

    #[test]
    fn developer_mode_wins_when_env_somehow_carries_both() {
        // .env is hand-editable (see parse_bioflow_tag's docstring), so both
        // lines can coexist. current_settings already resolves the pair with
        // developer taking precedence; match it rather than inventing a
        // second answer.
        assert_eq!(checkable_tag("0.3.0-alpha", Some("/home/me/bioflow")), None);
    }
}
