"""The two external MCP servers must be wired into every agent spawn.

The wiring lives in docker-compose.override.yml: AGENT_EXTRA_MCP_SERVERS is
merged into every spawned agent's --mcp-config by AgentService. If someone
edits that env value (renames a server, removes one, breaks the JSON), the
agent silently loses the server -- nothing else fails. This test pins the
wiring to the two servers Task 1 installed.

The commands are the absolute console-script paths inside the api image:
Task 1 installed the servers into an isolated venv (/opt/agent-mcp/venv)
because the backend pins mcp>=2,<3 while both servers require mcp 1.x. See
backend/Dockerfile and the Task 1 report.

The test reads the compose file from the repo checkout and skips when it is
not visible (e.g. a test container that does not mount it) -- the skip is
explicit rather than a silent pass.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERRIDE = REPO_ROOT / "docker-compose.override.yml"


@pytest.fixture(scope="module")
def override_text() -> str:
    if not OVERRIDE.exists():
        pytest.skip(f"{OVERRIDE} not mounted in this test environment")
    return OVERRIDE.read_text()


def _extra_servers(override_text: str) -> dict:
    """Extract the AGENT_EXTRA_MCP_SERVERS value from the override's api
    environment and parse it as JSON."""
    m = re.search(r"AGENT_EXTRA_MCP_SERVERS:\s*'(\{.*\})'", override_text, re.DOTALL)
    assert m, "AGENT_EXTRA_MCP_SERVERS missing from docker-compose.override.yml"
    return json.loads(m.group(1))


def test_extra_servers_are_the_two_expected(override_text):
    servers = _extra_servers(override_text)
    assert servers["mcpServers"].keys() == {"fetch", "datasets"}


def test_fetch_server_uses_the_installed_console_script(override_text):
    servers = _extra_servers(override_text)
    assert servers["mcpServers"]["fetch"]["command"] == "/opt/agent-mcp/venv/bin/mcp-server-fetch"


def test_datasets_server_uses_the_installed_console_script(override_text):
    servers = _extra_servers(override_text)
    assert servers["mcpServers"]["datasets"]["command"] == "/opt/agent-mcp/venv/bin/ncbi-datasets-mcp"
