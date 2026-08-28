"""The child-node stack must carry the same worker environment as the primary.

`docker-compose.child-node.yml` is a separate stack (`name: bioflow-child`),
copied to the child machine on its own, so it cannot reference
`docker-compose.yml`'s `x-backend-env` anchor -- every variable the worker reads
is repeated there by hand. A variable left out does not fail at startup: it
fails at the point of use, hours later, in a job. That is how the missing
`BIOINFO_HOME_HOST` and `NCBI_SETTINGS` survived (#880) -- the socket was
mounted specifically so sibling-container tools could run, and every such launch
died in `variant_runner`.

This is the drift detector for that hand-maintained duplication. It reads both
files and requires the child's worker to define everything the anchor does,
minus an explicit list of keys that belong only to the primary -- so adding a
variable to the anchor fails here until it is either propagated or deliberately
excluded.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIMARY = REPO_ROOT / "docker-compose.yml"
CHILD = REPO_ROOT / "docker-compose.child-node.yml"

# Anchor keys a child node legitimately does not need. Each one is here because
# it is consumed by a service the child does not run, not because it was
# inconvenient -- a key added here without that being true reopens #880.
PRIMARY_ONLY = frozenset(
    {
        # Read by the upload routes, which live in `api`. A child runs the
        # worker only.
        "MAX_SIMPLE_UPLOAD_BYTES",
    }
)


@pytest.fixture(scope="module")
def primary() -> dict:
    if not PRIMARY.exists():
        pytest.skip(f"{PRIMARY} not mounted in this test environment")
    return yaml.safe_load(PRIMARY.read_text())


@pytest.fixture(scope="module")
def child() -> dict:
    if not CHILD.exists():
        pytest.skip(f"{CHILD} not mounted in this test environment")
    return yaml.safe_load(CHILD.read_text())


def _child_env(child: dict) -> dict:
    return child["services"]["worker"]["environment"]


def test_the_child_worker_defines_every_shared_variable(primary, child):
    """The drift detector. A variable added to x-backend-env must be added to
    the child stack too, or explicitly listed as primary-only above."""
    anchor = primary["x-backend-env"]
    expected = set(anchor) - PRIMARY_ONLY
    missing = expected - set(_child_env(child))
    assert not missing, (
        f"docker-compose.child-node.yml is missing {sorted(missing)} from the "
        "primary's x-backend-env. Add them to the child worker's environment, "
        "or to PRIMARY_ONLY in this test if a child genuinely does not need them."
    )


def test_primary_only_keys_really_are_absent_from_the_child(primary, child):
    """The other direction, so PRIMARY_ONLY cannot rot into a list of things
    that are in fact present -- which would let it silently excuse a real gap."""
    present = PRIMARY_ONLY & set(_child_env(child))
    assert not present, (
        f"{sorted(present)} are listed as primary-only but the child sets them; "
        "remove them from PRIMARY_ONLY."
    )


def test_primary_only_names_real_anchor_keys(primary):
    """A typo in PRIMARY_ONLY would silently exempt nothing while looking like
    it exempted something."""
    unknown = PRIMARY_ONLY - set(primary["x-backend-env"])
    assert not unknown, f"PRIMARY_ONLY names keys the anchor does not have: {sorted(unknown)}"


def test_the_sibling_container_host_path_is_set(child):
    """The specific gap #880 is about: the socket is mounted so on-demand tools
    can run, and without this every one of those launches dies."""
    assert "BIOINFO_HOME_HOST" in _child_env(child)


def test_the_host_path_matches_the_data_mount(child):
    """BIOINFO_HOME_HOST and the /data bind must name the same host directory.
    Two different expressions would translate a sibling's mount to somewhere
    the file is not -- the failure variant_runner's docstring describes."""
    env_value = _child_env(child)["BIOINFO_HOME_HOST"]
    mounts = child["services"]["worker"]["volumes"]
    data_mount = next(m for m in mounts if m.endswith(":/data"))
    assert data_mount == f"{env_value}:/data"


def test_the_sra_settings_path_agrees_with_the_primary(primary, child):
    """Both must match Settings.ncbi_settings_path, which derives it from
    BIOINFO_HOME. A child on a different value fails with the SRA Toolkit's
    "cannot open configuration", which names nothing useful."""
    assert _child_env(child)["NCBI_SETTINGS"] == primary["x-backend-env"]["NCBI_SETTINGS"]
