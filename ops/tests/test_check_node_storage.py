"""`ops/check-node-storage.sh`: the guards, and the report it formats.

What the header comment promises is that the script refuses before it sends
anything when the stack is down or the API is not answering, and that it ends
by printing the next command. Those are the contract, so those are what is
tested here.

The sweep itself is not exercised -- it belongs to the API and is covered by
`backend/tests/api/test_node_storage_check.py`. What is tested is the wrapper:
its argument parsing, its preconditions, and the report it formats from a
response it is handed.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "ops" / "check-node-storage.sh"


def _run(args=(), env=None):
    """Run the script with `docker` and the API stubbed out.

    A stub `docker` earlier on PATH than the real one decides the stack
    precondition. The API is stubbed by pointing BIOFLOW_API_URL at the
    throwaway HTTP server in the `api` fixture; the precondition tests never
    get that far and do not need one.
    """
    bin_dir = env["_STUB_BIN"]
    full_env = dict(os.environ)
    full_env.update(env)
    full_env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=full_env,
        timeout=60,
    )


@pytest.fixture
def stub_bin(tmp_path):
    """A directory shadowing `docker` on PATH, with the stack reported up."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/usr/bin/env bash\necho container-id\n")
    docker.chmod(0o755)
    return bin_dir


@pytest.fixture
def stub_bin_stack_down(tmp_path):
    """`docker compose ps -q api` printing nothing: the stack is not running."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 0\n")
    docker.chmod(0o755)
    return bin_dir


@pytest.fixture
def api(tmp_path):
    """A throwaway HTTP server standing in for the API.

    Returns a factory: call it with the JSON the sweep should return, get
    back the base URL to point the script at.
    """
    import http.server
    import threading

    servers = []

    def start(payload, healthz=200, status=200):
        body = json.dumps(payload).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(healthz)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                Handler.last_body = self.rfile.read(length)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_port}", Handler

    yield start
    for s in servers:
        s.shutdown()


# ---- preconditions: refuse before sending anything -----------------------


def test_stack_down_exits_nonzero_with_the_remedy(stub_bin_stack_down):
    result = _run(env={"_STUB_BIN": str(stub_bin_stack_down)})
    assert result.returncode != 0
    assert "not running" in result.stderr
    # The header promises the literal command to fix it.
    assert "docker compose up -d" in result.stderr


def test_api_unreachable_exits_nonzero_with_the_remedy(stub_bin):
    # The container is up (stub docker prints an id) but nothing answers on
    # the port -- the case where logs are what the operator needs.
    result = _run(
        env={
            "_STUB_BIN": str(stub_bin),
            # Port 1 is reserved and nothing listens on it.
            "BIOFLOW_API_URL": "http://127.0.0.1:1",
        }
    )
    assert result.returncode != 0
    assert "did not answer" in result.stderr
    assert "docker compose logs" in result.stderr


# ---- argument parsing ----------------------------------------------------


def test_malformed_argument_is_refused_not_ignored(stub_bin):
    """A typo'd pair must not silently become "supplied nothing"."""
    result = _run(["lab-node-1"], env={"_STUB_BIN": str(stub_bin)})
    assert result.returncode != 0
    assert "node-id" in result.stderr


def test_help_prints_usage_without_touching_anything(stub_bin):
    result = _run(["--help"], env={"_STUB_BIN": str(stub_bin)})
    assert result.returncode == 0
    assert "Usage:" in result.stdout


# ---- the report ----------------------------------------------------------


def test_supplied_paths_reach_the_request(stub_bin, api):
    base, handler = api({"nodes": [], "checked": 0, "total": 0})
    result = _run(
        ["lab-node-1=/data/scratch"],
        env={"_STUB_BIN": str(stub_bin), "BIOFLOW_API_URL": base},
    )
    assert result.returncode == 0, result.stderr
    sent = json.loads(handler.last_body)
    assert sent["storage_locations"] == {"lab-node-1": "/data/scratch"}


def test_nodes_needing_a_path_end_with_the_next_command(stub_bin, api):
    """The first real run's outcome, and the one that must not read as failure."""
    base, _ = api(
        {
            "nodes": [
                {
                    "node_id": "lab-node-1",
                    "outcome": "no_recorded_path",
                    "storage_shared": None,
                    "storage_location": None,
                    "detail": "Cannot check -- no record of where storage is.",
                }
            ],
            "checked": 0,
            "total": 1,
        }
    )
    result = _run(env={"_STUB_BIN": str(stub_bin), "BIOFLOW_API_URL": base})
    assert "NEEDS A PATH" in result.stdout
    assert "./ops/check-node-storage.sh lab-node-1=" in result.stdout
    # Questions outstanding, so a caller in a script can tell.
    assert result.returncode == 4


def test_not_shared_node_gets_a_remedy_naming_its_own_path(stub_bin, api):
    base, _ = api(
        {
            "nodes": [
                {
                    "node_id": "lab-node-2",
                    "outcome": "not_shared",
                    "storage_shared": False,
                    "storage_location": "/srv/bioflow",
                    "detail": "The node has its own storage.",
                }
            ],
            "checked": 1,
            "total": 1,
        }
    )
    result = _run(env={"_STUB_BIN": str(stub_bin), "BIOFLOW_API_URL": base})
    assert "NOT SHARED" in result.stdout
    assert "/srv/bioflow" in result.stdout
    assert result.returncode == 4


def test_fully_shared_fleet_exits_zero(stub_bin, api):
    base, _ = api(
        {
            "nodes": [
                {
                    "node_id": "lab-node-1",
                    "outcome": "shared",
                    "storage_shared": True,
                    "storage_location": "/data/scratch",
                    "detail": "The node read the sentinel.",
                }
            ],
            "checked": 1,
            "total": 1,
        }
    )
    result = _run(env={"_STUB_BIN": str(stub_bin), "BIOFLOW_API_URL": base})
    assert "SHARED" in result.stdout
    assert result.returncode == 0


def test_unreachable_and_self_enrolled_both_say_cannot_check(stub_bin, api):
    """Neither is a verified negative, and the report must not imply one."""
    base, _ = api(
        {
            "nodes": [
                {
                    "node_id": "off",
                    "outcome": "unreachable",
                    "storage_shared": None,
                    "storage_location": "/data/scratch",
                    "detail": "Cannot check -- did not answer.",
                },
                {
                    "node_id": "self",
                    "outcome": "not_probeable",
                    "storage_shared": None,
                    "storage_location": "/data/scratch",
                    "detail": "Cannot check -- no SSH key.",
                },
            ],
            "checked": 0,
            "total": 2,
        }
    )
    result = _run(env={"_STUB_BIN": str(stub_bin), "BIOFLOW_API_URL": base})
    assert result.stdout.count("CANNOT CHECK") == 2
    assert "not shared" not in result.stdout.lower()
    # Nothing was established, so this must not read as an all-clear -- and
    # the exit code must not say so either. A sweep that probed nothing and
    # found no problems is a vacuous pass.
    assert "nothing was established" in result.stdout
    assert result.returncode == 4


def test_zero_probed_never_reads_as_an_all_clear(stub_bin, api):
    """The failure mode this whole feature exists to remove, in miniature.

    Found running the script against a live stack: every node unreachable
    printed "Every node that could be probed reads the primary's storage",
    which is true and useless -- nothing was probed. A report nobody can
    distinguish from success is worse than no report.
    """
    base, _ = api(
        {
            "nodes": [
                {
                    "node_id": "off",
                    "outcome": "unreachable",
                    "storage_shared": None,
                    "storage_location": "/data/scratch",
                    "detail": "Cannot check -- did not answer.",
                }
            ],
            "checked": 0,
            "total": 1,
        }
    )
    result = _run(env={"_STUB_BIN": str(stub_bin), "BIOFLOW_API_URL": base})
    assert "reads the primary's storage" not in result.stdout
    assert "still unverified" in result.stdout
    assert result.returncode == 4


def test_api_error_is_reported_not_swallowed(stub_bin, api):
    base, _ = api({"detail": "boom"}, status=500)
    result = _run(env={"_STUB_BIN": str(stub_bin), "BIOFLOW_API_URL": base})
    assert result.returncode != 0
    assert "failed" in result.stderr.lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
