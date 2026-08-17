"""Raw JSON-RPC client for BioFlow's Streamable HTTP MCP server.

Speaks the protocol directly (initialize -> notifications/initialized ->
tools/call) rather than via the ``mcp`` SDK client, mirroring the proven
pattern in ``backend/tests/mcp/test_server_live.py``. Tool names are prefixed
``bioflow_`` and ``tools/call`` returns the tool's value as a JSON string in
``result.content[0].text``.
"""

from __future__ import annotations

import json

import httpx


class McpError(RuntimeError):
    pass


def _extract_json(resp) -> dict:
    """Extract the JSON payload from an MCP Streamable-HTTP response.

    The server may reply with a plain JSON body or an SSE stream
    (``event: message\\ndata: {...}\\n\\n``). Parse both.
    """
    body = resp.text
    if "data:" in body:
        data = "\n".join(
            line[5:].strip() for line in body.splitlines() if line.startswith("data:")
        )
        return json.loads(data)
    return json.loads(body)


class McpClient:
    def __init__(self, base_url: str, profile: str = ""):
        self._url = f"{base_url.rstrip('/')}/api/v1/mcp/"
        if profile:
            self._url += f"?profile={profile}"
        self._session_id: str | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(600.0))
        self._id = 0

    async def __aenter__(self) -> McpClient:
        await self._init()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _post(self, payload: dict, expect_202: bool = False):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        resp = await self._client.post(self._url, headers=headers, json=payload)
        if expect_202 and resp.status_code == 202:
            return resp
        if resp.status_code != 200:
            raise McpError(f"MCP request failed ({resp.status_code}): {resp.text[:500]}")
        return resp

    async def _init(self) -> None:
        resp = await self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "bioflow-e2e", "version": "0.1.0"},
            },
        })
        self._session_id = resp.headers.get("mcp-session-id")
        if not self._session_id:
            raise McpError("MCP initialize returned no mcp-session-id header")
        await self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_202=True
        )

    async def call_tool(self, name: str, arguments: dict) -> dict:
        resp = await self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": f"bioflow_{name}", "arguments": arguments},
        })
        payload = _extract_json(resp)
        if "error" in payload:
            raise McpError(f"tools/call error: {payload['error']}")
        result = payload.get("result", {})
        if result.get("isError"):
            raise McpError(f"tool {name} returned isError: {result}")
        content = result.get("content", [])
        text = content[0].get("text", "") if content else ""
        return json.loads(text) if text else {}
