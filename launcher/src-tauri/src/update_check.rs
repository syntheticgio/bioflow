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
    }

    #[test]
    fn a_client_that_gives_up_is_called_exactly_once_never_retried() {
        let registry = SlowRegistry { calls: Cell::new(0) };
        let local = FakeLocal(Some("sha256:old"));
        let result = update_available(&registry, &local, "org/img", "latest");
        assert_eq!(result, None);
        assert_eq!(registry.calls.get(), 1);
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
}
