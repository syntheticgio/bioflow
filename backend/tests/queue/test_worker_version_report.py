"""The worker reports what image it is running, so the primary can tell a
current node from a stale one.

Mirrors launcher/src-tauri/src/update_check.rs's DockerImageInspector: reads
the digest Docker already recorded against the image reference, the same
RepoDigests field the launcher's own update check compares to GHCR. Reading
`docker inspect <container>` instead would return the container's image ID,
a different value that can never equal a registry digest -- the two sides of
a staleness comparison would silently never agree.
"""

from unittest.mock import patch

from app.queue.worker import _own_image_digest


def test_own_image_digest_reads_repo_digest():
    completed = type("R", (), {
        "returncode": 0,
        "stdout": "ghcr.io/syntheticgio/bioflow-backend@sha256:deadbeef\n",
    })()
    with patch("app.queue.worker.subprocess.run", return_value=completed):
        assert _own_image_digest() == "sha256:deadbeef"


def test_own_image_digest_returns_none_without_docker_client():
    with patch("app.queue.worker.shutil.which", return_value=None):
        assert _own_image_digest() is None


def test_own_image_digest_returns_none_when_inspect_fails():
    """A node whose socket is not mounted reports no digest rather than
    failing to heartbeat (NU-3)."""
    completed = type("R", (), {"returncode": 1, "stdout": ""})()
    with patch("app.queue.worker.subprocess.run", return_value=completed):
        assert _own_image_digest() is None


def test_own_image_digest_returns_none_on_exception():
    with patch("app.queue.worker.subprocess.run", side_effect=OSError("boom")):
        assert _own_image_digest() is None


def test_own_image_digest_returns_none_on_malformed_output():
    """RepoDigests can be empty (locally built image, never pushed/pulled) --
    docker then prints the template literal '<no value>' or an empty line,
    neither of which contains '@'."""
    completed = type("R", (), {"returncode": 0, "stdout": "<no value>\n"})()
    with patch("app.queue.worker.subprocess.run", return_value=completed):
        assert _own_image_digest() is None
