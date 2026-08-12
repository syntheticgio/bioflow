"""The worker reports what image it is running, so the primary can tell a
current node from a stale one.

Two docker calls: this container's own image id, then that id's
RepoDigests. Not a single call against a hardcoded tag reference -- a node
whose deployment pins BIOFLOW_TAG has no way to expose that tag to the
process (compose substitutes it into `image:` at parse time, nothing passes
it through as an environment variable), so inspecting a fixed "...:latest"
reference would silently report the wrong image on any pinned node.
Resolving through the container's own image id sidesteps that: whatever
image the container is, is exactly what gets inspected.
"""

from unittest.mock import patch

from app.queue.worker import _own_image_digest, _own_image_id


def test_own_image_id_reads_docker_inspect():
    completed = type("R", (), {"returncode": 0, "stdout": "sha256:localid\n"})()
    with patch("app.queue.worker.subprocess.run", return_value=completed):
        assert _own_image_id() == "sha256:localid"


def test_own_image_id_returns_none_without_docker_client():
    with patch("app.queue.worker.shutil.which", return_value=None):
        assert _own_image_id() is None


def test_own_image_digest_resolves_through_own_image_id():
    completed = type("R", (), {
        "returncode": 0,
        "stdout": "ghcr.io/syntheticgio/bioflow-backend@sha256:deadbeef\n",
    })()
    with patch("app.queue.worker._own_image_id", return_value="sha256:localid"), \
         patch("app.queue.worker.subprocess.run", return_value=completed) as run:
        assert _own_image_digest() == "sha256:deadbeef"
    # The id from the first call is what gets inspected in the second.
    assert "sha256:localid" in run.call_args.args[0]


def test_own_image_digest_returns_none_without_docker_client():
    with patch("app.queue.worker.shutil.which", return_value=None):
        assert _own_image_digest() is None


def test_own_image_digest_returns_none_when_own_id_unavailable():
    """A node whose socket is not mounted reports no digest rather than
    failing to heartbeat (NU-3)."""
    with patch("app.queue.worker._own_image_id", return_value=None):
        assert _own_image_digest() is None


def test_own_image_digest_returns_none_when_inspect_fails():
    with patch("app.queue.worker._own_image_id", return_value="sha256:localid"), \
         patch("app.queue.worker.subprocess.run",
               return_value=type("R", (), {"returncode": 1, "stdout": ""})()):
        assert _own_image_digest() is None


def test_own_image_digest_returns_none_on_exception():
    with patch("app.queue.worker._own_image_id", return_value="sha256:localid"), \
         patch("app.queue.worker.subprocess.run", side_effect=OSError("boom")):
        assert _own_image_digest() is None


def test_own_image_digest_returns_none_on_malformed_output():
    """RepoDigests can be empty (locally built image, never pushed/pulled) --
    docker then prints the template literal '<no value>' or an empty line,
    neither of which contains '@'."""
    completed = type("R", (), {"returncode": 0, "stdout": "<no value>\n"})()
    with patch("app.queue.worker._own_image_id", return_value="sha256:localid"), \
         patch("app.queue.worker.subprocess.run", return_value=completed):
        assert _own_image_digest() is None
