"""End-to-end proof that `?profile=` reaches a tool through the real mount.

`test_mount.py` only proves a route exists at the right path -- it says
nothing about whether a request actually reaches a tool function with the
right owner. That gap is exactly the shape of failure CLAUDE.md warns about
under "Check a rule against the real database, not only its unit tests": a
mount that looks right in isolated tool tests but is wired wrong end-to-end.

This drives `app.mcp.server.mount_mcp_app` -- the exact function `app.main`'s
`create_app()` calls -- mounted onto a bare `FastAPI()` test app, through a
real MCP session: `initialize`, `notifications/initialized`, then
`tools/call`, speaking real JSON-RPC over the ASGI app directly -- the same
sequence a real MCP client performs.

Two deliberate departures from the obvious approach, both forced by real
failures reproduced while writing this test:

1. This does *not* drive `app.main.app` itself. `app.main.app`'s own lifespan
   calls `connect_to_mongo()`, which connects to the real `mongo_db` database
   (`biopipe`) independently of this test suite's `beanie_models` fixture,
   which connects its own separate client to the throwaway `biopipe_test`
   database and never touches `app.db.client`'s module-level `_client`.
   Driving the real app's lifespan inside a test reconnects Beanie to the
   wrong database out from under every other fixture in this suite --
   reproduced directly: a profile created through the fixture's connection
   came back "Unknown profile" when read back through the real app's
   independently-connected one. Building a bare `FastAPI()` and calling
   `mount_mcp_app` on it directly exercises the same mount + lifespan-
   chaining code path while staying on the fixture's `biopipe_test`
   connection.

2. This does not use `starlette.testclient.TestClient`, despite `TestClient`
   being what the task's own probe script used successfully in isolation.
   `TestClient` runs the app on its own event loop via a blocking portal
   thread, while `beanie_models` holds a Motor connection bound to the
   *test's* loop -- mixing the two fails with "Task ... attached to a
   different loop" the moment a tool queries Mongo, before any assertion
   runs. This is a documented constraint elsewhere in this suite already
   (see the module docstring of tests/api/test_object_download.py, which hit
   the identical error for the identical reason). `httpx.AsyncClient` with
   `httpx.ASGITransport` runs the whole request on the *current* event loop
   with no portal thread, which is what actually lets this compose with
   `beanie_models`. The one cost: `ASGITransport` does not run the app's
   lifespan for you either, so it's entered explicitly below.

Confirmed by direct reproduction that this combination is what would have
caught two real defects found only by running actual code against the
actual installed `mcp==2.0.0`:

- `app.mount()` alone does not start the streamable-HTTP session manager,
  because Starlette does not chain a mounted sub-app's lifespan into the
  parent's; every call 500s with "Task group is not initialized" until
  `mount_mcp_app` explicitly enters the sub-app's lifespan too.
- The outer app's lifespan must actually be *entered* (context-managed) for
  the same reason -- a `mount()` with no lifespan running behind it fails
  the same way.
"""

import json

import httpx
import pytest
from fastapi import FastAPI

from app.mcp.server import mount_mcp_app
from app.services import profile_service

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]

_JSONRPC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _build_test_app() -> FastAPI:
    """A bare FastAPI app with only the MCP server mounted -- no Mongo/Redis
    lifespan of its own, so it rides the `beanie_models` fixture's existing
    connection instead of opening a second, wrongly-scoped one."""
    test_app = FastAPI()
    mount_mcp_app(test_app)
    return test_app


def _extract_result(sse_body: str) -> dict:
    """Pull the JSON payload out of a `text/event-stream` response body.

    The streamable-HTTP transport replies to a JSON-RPC POST with a single
    SSE `data:` line rather than a bare JSON body.
    """
    for line in sse_body.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise AssertionError(f"No data: line in SSE body: {sse_body!r}")


async def test_profile_query_param_reaches_the_tool_through_the_real_mount():
    profile = await profile_service.create_profile(username="mcp-live-e2e")
    owner = profile.owner_id()

    test_app = _build_test_app()
    mcp_url = f"/api/v1/mcp/mcp?profile={owner}"

    async with test_app.router.lifespan_context(test_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            init_resp = await client.post(
                mcp_url,
                headers=_JSONRPC_HEADERS,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "0.0.1"},
                    },
                },
            )
            assert init_resp.status_code == 200, init_resp.text
            session_id = init_resp.headers["mcp-session-id"]
            session_headers = {**_JSONRPC_HEADERS, "mcp-session-id": session_id}

            notify_resp = await client.post(
                mcp_url,
                headers=session_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert notify_resp.status_code == 202, notify_resp.text

            call_resp = await client.post(
                mcp_url,
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "bioflow_whoami", "arguments": {}},
                },
            )
            assert call_resp.status_code == 200, call_resp.text

            payload = _extract_result(call_resp.text)
            assert "error" not in payload, payload
            assert payload["result"]["isError"] is False, payload

            # tools/call wraps the tool's return value as a JSON string
            # inside a text content block -- unwrap it to see what
            # bioflow_whoami actually returned.
            whoami_result = json.loads(payload["result"]["content"][0]["text"])

    assert whoami_result["owner"] == owner
    assert whoami_result["username"] == "mcp-live-e2e"


async def test_ambiguous_profile_is_a_clean_tool_error_not_a_500():
    """The most likely real-world error path: two profiles exist and the
    connection URL omits `?profile=`.

    `context.owner_for` raises `ProfileUnresolvedError`, a plain `AppError`
    the `mcp` library knows nothing about. Every other test here only
    exercises the success path, so whether that exception turns into a
    clean `tools/call` error the calling agent can read, or an unhandled
    500 through the streamable-HTTP transport, was unverified -- and two
    profiles is a completely ordinary state for this app, not an edge case.
    """
    await profile_service.create_profile(username="mcp-live-ambiguous-a")
    await profile_service.create_profile(username="mcp-live-ambiguous-b")

    test_app = _build_test_app()
    mcp_url = "/api/v1/mcp/mcp"  # no ?profile=

    async with test_app.router.lifespan_context(test_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            init_resp = await client.post(
                mcp_url,
                headers=_JSONRPC_HEADERS,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "0.0.1"},
                    },
                },
            )
            assert init_resp.status_code == 200, init_resp.text
            session_id = init_resp.headers["mcp-session-id"]
            session_headers = {**_JSONRPC_HEADERS, "mcp-session-id": session_id}

            await client.post(
                mcp_url,
                headers=session_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )

            call_resp = await client.post(
                mcp_url,
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "bioflow_whoami", "arguments": {}},
                },
            )

    # A clean JSON-RPC response, not a transport-level failure: the
    # streamable-HTTP endpoint itself must still answer 200, with the error
    # carried inside the tool result rather than as an HTTP-level fault.
    assert call_resp.status_code == 200, call_resp.text
    payload = _extract_result(call_resp.text)

    assert "error" not in payload, payload
    assert payload["result"]["isError"] is True, payload

    message = payload["result"]["content"][0]["text"]
    assert "?profile=" in message


async def test_resources_list_includes_the_derived_and_guide_resources():
    test_app = _build_test_app()
    mcp_url = "/api/v1/mcp/mcp"

    async with test_app.router.lifespan_context(test_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            init_resp = await client.post(
                mcp_url,
                headers=_JSONRPC_HEADERS,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "0.0.1"},
                    },
                },
            )
            session_id = init_resp.headers["mcp-session-id"]
            session_headers = {**_JSONRPC_HEADERS, "mcp-session-id": session_id}

            await client.post(
                mcp_url,
                headers=session_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )

            list_resp = await client.post(
                mcp_url,
                headers=session_headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
            )
            payload = _extract_result(list_resp.text)

    uris = {r["uri"] for r in payload["result"]["resources"]}

    assert "bioflow://jobs/types" in uris
    assert "bioflow://tools/installed" in uris
    assert "bioflow://sources" in uris
    assert "bioflow://guides/getting-started" in uris
