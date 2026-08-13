import asyncio
import json

import httpx

from e2e.backend import mcp_client


def _patch(monkeypatch, handler):
    RealClient = httpx.AsyncClient
    monkeypatch.setattr(
        mcp_client.httpx, "AsyncClient",
        lambda *a, **k: RealClient(transport=httpx.MockTransport(handler)),
    )


def test_call_tool_handshake_and_unwrap(monkeypatch):
    seen_headers = []

    def handler(request):
        seen_headers.append(dict(request.headers))
        body = json.loads(request.content)
        method = body["method"]
        if method == "initialize":
            return httpx.Response(
                200, headers={"mcp-session-id": "sess1"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"capabilities": {}}},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            assert body["params"]["name"] == "bioflow_whoami"
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"content": [{"type": "text", "text": '{"owner": "p1"}'}]},
            })
        raise AssertionError(f"unexpected method {method}")

    _patch(monkeypatch, handler)

    async def go():
        async with mcp_client.McpClient("http://bf:8000", "prof1") as c:
            return await c.call_tool("whoami", {})

    result = asyncio.run(go())
    assert result == {"owner": "p1"}
    assert seen_headers[2].get("mcp-session-id") == "sess1"


def test_url_has_profile_query(monkeypatch):
    seen_url = {}

    def handler(request):
        seen_url["url"] = str(request.url)
        return httpx.Response(
            200, headers={"mcp-session-id": "s"},
            json={"jsonrpc": "2.0", "id": 1, "result": {}},
        )

    _patch(monkeypatch, handler)

    async def go():
        async with mcp_client.McpClient("http://bf:8000", "prof1"):
            pass

    asyncio.run(go())
    assert "?profile=prof1" in seen_url["url"]


def test_is_error_raises(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        method = body["method"]
        if method == "initialize":
            return httpx.Response(
                200, headers={"mcp-session-id": "s"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"isError": True, "content": [{"type": "text", "text": "{}"}]},
        })

    _patch(monkeypatch, handler)

    async def go():
        async with mcp_client.McpClient("http://bf:8000") as c:
            await c.call_tool("whoami", {})

    try:
        asyncio.run(go())
        assert False, "expected McpError"
    except mcp_client.McpError as e:
        assert "isError" in str(e)
