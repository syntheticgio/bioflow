"""node_name and storage_location must never reach a remote shell (#870).

Both are interpolated into commands that run on the provisioned node -- most
consequentially into the body of a quoted heredoc, whose delimiter a newline
can terminate early, turning the rest of the value into shell on that machine.
`node_name` additionally becomes the comment on a generated SSH key appended to
the node's authorized_keys.

These are model-level tests rather than endpoint tests on purpose: validating in
`ProvisionRequest` is what makes the injection unreachable from *every* path out
of the request at once, so the model is the thing worth pinning.
"""

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api.v1.nodes import (
    ProvisionRequest,
    _render_node_env,
    _sanitize_node_name,
)


def _req(**over):
    base = {
        "host": "192.168.1.50",
        "username": "user",
        "password": "pw",
        "node_name": "worker-1",
        "storage_location": "/data/scratch",
    }
    base.update(over)
    return ProvisionRequest(**base)


def test_the_ordinary_request_is_accepted():
    """The defaults and a plain name must keep working -- a validator that
    rejects the normal case is not a fix."""
    r = _req()
    assert r.node_name == "worker-1"
    assert r.storage_location == "/data/scratch"


@pytest.mark.parametrize(
    "node_name",
    [
        # The reported attack: end the heredoc, then run anything.
        "n\nHERMESEOF\nrm -rf /\n",
        "n\nHERMESEOF",
        # Forge an extra authorized_keys line via the key comment.
        "n\nssh-ed25519 AAAAC3NzaC1 attacker",
        # Ordinary shell metacharacters, in case a future call site is unquoted.
        "n; rm -rf /",
        "n$(whoami)",
        "n`id`",
        "n|tee /tmp/x",
        "n&whoami",
        "n with spaces",
        "n/../../etc",
        "",  # empty
        "x" * 65,  # over the length cap
    ],
)
def test_dangerous_node_names_are_refused(node_name):
    with pytest.raises(PydanticValidationError):
        _req(node_name=node_name)


@pytest.mark.parametrize(
    "storage_location",
    [
        "/data\nHERMESEOF\nrm -rf /",
        "/data; rm -rf /",
        "/data$(id)",
        "/data`id`",
        "/data with spaces",
        "relative/path",  # not absolute
        "/data/../../etc",  # traversal
        "/data/scratch/",  # trailing slash: an empty final segment
        "",
    ],
)
def test_dangerous_storage_locations_are_refused(storage_location):
    with pytest.raises(PydanticValidationError):
        _req(storage_location=storage_location)


@pytest.mark.parametrize(
    "storage_location",
    ["/data", "/data/scratch", "/mnt/bioflow-data", "/srv/bio_flow/v1.2", "/a/b/c/d"],
)
def test_ordinary_absolute_paths_are_accepted(storage_location):
    assert _req(storage_location=storage_location).storage_location == storage_location


def test_no_accepted_value_can_break_the_heredoc():
    """The property the regexes exist to guarantee, asserted end to end.

    A validated pair rendered into the .env body can never contain a newline,
    so it can never reach the delimiter check the remote shell performs.
    """
    r = _req(node_name="node-42", storage_location="/mnt/bio_flow-1.2")
    body = _render_node_env(
        mongo_url="mongodb://h:27017/db",
        redis_url="redis://h:6379/0",
        api_url="http://h:8000",
        node_name=r.node_name,
        storage_location=r.storage_location,
        worker_replicas=r.worker_replicas,
    )
    assert "HERMESEOF" not in body
    # Every line is a plain KEY=VALUE: nothing the user supplied added a line.
    assert all("=" in line for line in body.strip().splitlines())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Johns-MacBook-Pro.local-node", "Johns-MacBook-Pro-local-node"),
        ("primary-node", "primary-node"),
        ("....", "compute-node"),  # nothing usable survives
        ("", "compute-node"),
        ("host name.lan-node", "host-name-lan-node"),
    ],
)
def test_sanitize_node_name(raw, expected):
    assert _sanitize_node_name(raw) == expected


def test_the_suggested_name_is_always_a_valid_one():
    """The connection endpoint derives a default from platform.node(), which on
    a Mac is a dotted mDNS name. Offering a default the form then rejects would
    be a worse bug than the one being fixed, so the suggestion is sanitized --
    and this is the test that ties the two ends together.
    """
    for hostname in ["Johns-MacBook-Pro.local", "box.lan", "primary", "a b c"]:
        suggested = _sanitize_node_name(f"{hostname}-node")
        assert _req(node_name=suggested).node_name == suggested
