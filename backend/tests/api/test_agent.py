"""The agent HTTP surface: ask spawns a pi process, events streams the
translated RPC events, stop and restart manage the lifecycle.

The spawn seam is patched with the same FakeProcess the service tests use --
no pi binary in the test image -- and the SSE stream is driven over the ASGI
transport for real: the whole point of these tests is the router's
re-attaching stream loop, which only exists because the drawer opens
/events before any process does.
"""

import asyncio
import json
import socket

import pytest
import pytest_asyncio
import uvicorn
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.v1.agent import router as agent_router
from app.models.base import utcnow
from app.models.conversation import ConversationTurn, ProjectConversation
from app.services import project_service
from app.services.agent_service import agent_service
from tests.services.test_agent_service import FakeProcess

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]

@pytest.fixture
def spawn(monkeypatch):
    """Patch the spawn seam; returns (cmds, spawned fakes) in order."""
    cmds: list[list[str]] = []
    spawned: list[FakeProcess] = []

    async def fake_spawn(*cmd, **kwargs):
        cmds.append(list(cmd))
        proc = FakeProcess()
        spawned.append(proc)
        return proc

    monkeypatch.setattr("app.services.agent_service.create_subprocess_exec", fake_spawn)
    return cmds, spawned

async def _project(owner: str):
    return await project_service.create_project(name="agent-project", owner=owner, parent_id=None)

def _prompt_lines(fake: FakeProcess) -> list[dict]:
    return [
        json.loads(line)
        for line in fake.stdin.written.decode().strip().splitlines()
        if line.strip()
    ]

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_server():
    """The agent router on a real uvicorn server, running on *this* loop.

    httpx's ASGITransport buffers a response body until the app completes, so
    it can never deliver an infinite SSE stream -- the stream tests need a
    real socket. uvicorn's `serve()` runs on the current event loop, which is
    what keeps Beanie's Motor connection (bound to this loop) usable inside
    the request handlers; a portal-thread server would fail with the classic
    "attached to a different loop" error the moment `resolve_owner` queries
    Profile. The bare app mounts only the agent router: the full app's
    lifespan would reconnect Mongo to the real `mongo_db` out from under the
    `beanie_models` fixture.

    Module-scoped on purpose: this repo's sse-starlette fork runs a
    process-global "server is exiting" watcher, so shutting one uvicorn
    server down also kills the streams of the next one started in the same
    process -- two sequential servers in one test run do not compose.
    """
    bare = FastAPI()
    # Production wires agent_router through api_router with /api/v1; the bare
    # app replicates that one prefix so URLs match the real deployment.
    bare.include_router(agent_router, prefix="/api/v1")
    config = uvicorn.Config(bare, host="127.0.0.1", port=_free_port(), log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.01)
    else:
        server.should_exit = True
        raise AssertionError("uvicorn did not start")
    yield f"http://127.0.0.1:{config.port}"
    server.should_exit = True
    await task

async def _sse_events(response):
    """Yield (event_type, data) pairs from an open SSE response.

    httpx streams are single-pass: `aiter_lines` may only be entered once, so
    this drives one loop for the whole response and yields at each blank-line
    event boundary. Callers step it with `anext()` per expected event.
    """
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            block = "\n".join(lines)
            lines = []
            yield _parse_event(block)
        else:
            lines.append(line)

def _parse_event(block: str) -> tuple[str, dict]:
    etype = data = None
    for line in block.splitlines():
        if line.startswith("event:"):
            etype = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:"):].strip())
    assert etype is not None and data is not None, f"malformed SSE block: {block!r}"
    return etype, data

class TestAsk:
    async def test_requires_a_profile(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id())
        response = await client.post(
            f"/api/v1/projects/{project.id}/agent/ask", json={"message": "hi"}
        )
        assert response.status_code == 400
        assert response.json()["code"] == "profile_unresolved"

    async def test_unknown_project_404s(self, client, two_profiles):
        response = await client.post(
            f"/api/v1/projects/{PydanticObjectId()}/agent/ask",
            json={"message": "hi"},
            headers=two_profiles["a_headers"],
        )
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    async def test_accepts_and_spawns_with_project_context(self, client, two_profiles, spawn):
        project = await _project(two_profiles["a"].owner_id())
        response = await client.post(
            f"/api/v1/projects/{project.id}/agent/ask",
            json={"message": "run qc on the reads"},
            headers=two_profiles["a_headers"],
        )
        assert response.status_code == 200
        assert response.json() == {"status": "accepted"}

        cmds, spawned = spawn
        assert len(spawned) == 1
        prompt_arg = cmds[0][cmds[0].index("--system-prompt") + 1]
        assert "agent-project" in prompt_arg

        prompt = _prompt_lines(spawned[0])[-1]
        assert prompt["type"] == "prompt"
        assert prompt["message"] == "run qc on the reads"
        assert prompt["streamingBehavior"] == "steer"

    async def test_second_message_reuses_the_process(self, client, two_profiles, spawn):
        project = await _project(two_profiles["a"].owner_id())
        headers = two_profiles["a_headers"]
        for message in ("first", "second"):
            response = await client.post(
                f"/api/v1/projects/{project.id}/agent/ask",
                json={"message": message},
                headers=headers,
            )
            assert response.status_code == 200
        _, spawned = spawn
        assert len(spawned) == 1
        assert [p["message"] for p in _prompt_lines(spawned[0])] == ["first", "second"]

    async def test_empty_message_is_rejected(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id())
        response = await client.post(
            f"/api/v1/projects/{project.id}/agent/ask",
            json={"message": "   "},
            headers=two_profiles["a_headers"],
        )
        assert response.status_code == 422

    async def test_missing_pi_binary_is_a_503(self, client, two_profiles, monkeypatch):
        async def boom(*cmd, **kwargs):
            raise FileNotFoundError

        monkeypatch.setattr("app.services.agent_service.create_subprocess_exec", boom)
        project = await _project(two_profiles["a"].owner_id())
        response = await client.post(
            f"/api/v1/projects/{project.id}/agent/ask",
            json={"message": "hi"},
            headers=two_profiles["a_headers"],
        )
        assert response.status_code == 503
        assert response.json()["code"] == "agent_unavailable"

class TestEvents:
    async def test_reports_status_then_forwards_translations(
        self, two_profiles, spawn, live_server
    ):
        project = await _project(two_profiles["a"].owner_id())
        headers = two_profiles["a_headers"]
        url = f"{live_server}/api/v1/projects/{project.id}/agent/events"
        profile_id = two_profiles["a"].id

        async with AsyncClient(timeout=None) as http:
            async with http.stream("GET", url, params={"profile": str(profile_id)}) as stream:
                events = _sse_events(stream)
                async with asyncio.timeout(10):
                    etype, data = await anext(events)
                assert etype == "agent_status"
                assert data == {"running": False}

                response = await http.post(
                    f"{live_server}/api/v1/projects/{project.id}/agent/ask",
                    json={"message": "hi"},
                    headers=headers,
                )
                assert response.status_code == 200

                _, spawned = spawn
                fake = spawned[0]
                fake.stdout.feed({"type": "response", "command": "prompt", "success": True})
                fake.stdout.feed({"type": "agent_start"})
                fake.stdout.feed(
                    {"type": "message_update",
                     "assistantMessageEvent": {
                         "type": "text_delta", "contentIndex": 0, "delta": "Hello"
                     }}
                )
                fake.stdout.feed({"type": "agent_settled"})

                async with asyncio.timeout(10):
                    etype, _ = await anext(events)
                assert etype == "agent_start"
                async with asyncio.timeout(10):
                    etype, data = await anext(events)
                assert etype == "message_delta"
                assert data == {"kind": "text", "contentIndex": 0, "delta": "Hello"}
                async with asyncio.timeout(10):
                    etype, data = await anext(events)
                assert etype == "done"

    async def test_reattaches_after_stop(self, two_profiles, spawn, live_server):
        """The stream is opened before any process and must follow the
        process across a stop + re-ask."""
        project = await _project(two_profiles["a"].owner_id())
        headers = two_profiles["a_headers"]
        url = f"{live_server}/api/v1/projects/{project.id}/agent/events"
        profile_id = two_profiles["a"].id

        async with AsyncClient(timeout=None) as http:
            async with http.stream("GET", url, params={"profile": str(profile_id)}) as stream:
                events = _sse_events(stream)
                async with asyncio.timeout(10):
                    etype, _ = await anext(events)
                assert etype == "agent_status"

                await http.post(
                    f"{live_server}/api/v1/projects/{project.id}/agent/ask",
                    json={"message": "first"},
                    headers=headers,
                )
                _, spawned = spawn
                spawned[0].stdout.feed(
                    {"type": "response", "command": "prompt", "success": True}
                )
                spawned[0].stdout.feed({"type": "agent_start"})
                async with asyncio.timeout(10):
                    etype, _ = await anext(events)
                assert etype == "agent_start"

                # Stop the process: the attached stream ends (__stop__), the
                # router loop re-polls, and the next ask's fresh process is
                # picked up -- no browser reconnect involved.
                agent_url = f"{live_server}/api/v1/projects/{project.id}/agent"
                response = await http.delete(agent_url, params={"profile": str(profile_id)})
                assert response.status_code == 204

                await http.post(
                    f"{live_server}/api/v1/projects/{project.id}/agent/ask",
                    json={"message": "second"},
                    headers=headers,
                )
                assert len(spawned) == 2
                spawned[1].stdout.feed(
                    {"type": "response", "command": "prompt", "success": True}
                )
                spawned[1].stdout.feed({"type": "agent_start"})
                async with asyncio.timeout(10):
                    etype, _ = await anext(events)
                assert etype == "agent_start"

class TestLifecycle:
    async def test_delete_stops_the_agent(self, client, two_profiles, spawn):
        project = await _project(two_profiles["a"].owner_id())
        headers = two_profiles["a_headers"]
        url = f"/api/v1/projects/{project.id}/agent"
        await client.post(url + "/ask", json={"message": "hi"}, headers=headers)
        assert agent_service.get(str(two_profiles["a"].id), str(project.id)) is not None

        response = await client.delete(url, headers=headers)
        assert response.status_code == 204
        assert agent_service.get(str(two_profiles["a"].id), str(project.id)) is None

    async def test_restart_spawns_a_fresh_process(self, client, two_profiles, spawn):
        project = await _project(two_profiles["a"].owner_id())
        headers = two_profiles["a_headers"]
        url = f"/api/v1/projects/{project.id}/agent"
        await client.post(url + "/ask", json={"message": "hi"}, headers=headers)
        first = agent_service.get(str(two_profiles["a"].id), str(project.id))

        response = await client.post(url + "/restart", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "restarting"}

        second = agent_service.get(str(two_profiles["a"].id), str(project.id))
        assert second is not None and second is not first
        _, spawned = spawn
        assert len(spawned) == 2


class TestConversationPersistence:
    """Issue #97: conversations survive closing and reopening the drawer."""

    async def test_ask_persists_user_turn(self, client, two_profiles, spawn):
        """POST /ask saves the user message to ProjectConversation."""
        project = await _project(two_profiles["a"].owner_id())
        response = await client.post(
            f"/api/v1/projects/{project.id}/agent/ask",
            json={"message": "run qc"},
            headers=two_profiles["a_headers"],
        )
        assert response.status_code == 200
        # Let the background save task complete.
        await asyncio.sleep(0.1)

        convo = await ProjectConversation.find_one(
            ProjectConversation.owner == two_profiles["a"].owner_id(),
            ProjectConversation.project_id == project.id,
        )
        assert convo is not None
        assert len(convo.turns) == 1
        assert convo.turns[0].role == "user"
        assert convo.turns[0].content == "run qc"
        assert convo.turns[0].tool_calls is None

    async def test_get_conversation_loads_saved_turns(self, client, two_profiles, spawn):
        """GET /conversation returns saved turns for the same owner."""
        project = await _project(two_profiles["a"].owner_id())
        # Pre-seed a conversation with turns.
        convo = ProjectConversation(
            owner=two_profiles["a"].owner_id(),
            project_id=project.id,
            turns=[
                ConversationTurn(role="user", content="question 1", created_at=utcnow()),
                ConversationTurn(
                    role="assistant",
                    content="answer 1",
                    created_at=utcnow(),
                    tool_calls=None,
                ),
            ],
        )
        await convo.insert()

        response = await client.get(
            f"/api/v1/projects/{project.id}/agent/conversation",
            headers=two_profiles["a_headers"],
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["turns"]) == 2
        assert body["turns"][0]["role"] == "user"
        assert body["turns"][0]["content"] == "question 1"
        assert body["turns"][0]["tool_calls"] is None
        assert body["turns"][1]["role"] == "assistant"
        assert body["turns"][1]["content"] == "answer 1"

    async def test_get_conversation_creates_empty_on_first_access(
        self, client, two_profiles
    ):
        """No conversation yet -> an empty one is created, matching Q&A."""
        project = await _project(two_profiles["a"].owner_id())
        response = await client.get(
            f"/api/v1/projects/{project.id}/agent/conversation",
            headers=two_profiles["a_headers"],
        )
        assert response.status_code == 200
        assert response.json()["turns"] == []

    async def test_get_conversation_is_owner_scoped(self, client, two_profiles):
        """Owner B must not see owner A's conversation -- gets its own empty one."""
        project = await _project(two_profiles["a"].owner_id())
        convo = ProjectConversation(
            owner=two_profiles["a"].owner_id(),
            project_id=project.id,
            turns=[ConversationTurn(role="user", content="secret", created_at=utcnow())],
        )
        await convo.insert()

        response = await client.get(
            f"/api/v1/projects/{project.id}/agent/conversation",
            headers=two_profiles["b_headers"],
        )
        assert response.status_code == 200
        assert response.json()["turns"] == []

    async def test_get_conversation_requires_profile(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id())
        response = await client.get(
            f"/api/v1/projects/{project.id}/agent/conversation"
        )
        assert response.status_code == 400

    async def test_save_turn_appends_to_existing(self, client, two_profiles):
        """POST /conversation/turns appends a turn to the document."""
        project = await _project(two_profiles["a"].owner_id())
        # Pre-seed
        convo = ProjectConversation(
            owner=two_profiles["a"].owner_id(),
            project_id=project.id,
            turns=[ConversationTurn(role="user", content="first", created_at=utcnow())],
        )
        await convo.insert()

        response = await client.post(
            f"/api/v1/projects/{project.id}/agent/conversation/turns",
            json={"role": "assistant", "content": "second", "tool_calls": None},
            headers=two_profiles["a_headers"],
        )
        assert response.status_code == 201
        assert response.json()["role"] == "assistant"
        assert response.json()["content"] == "second"

        refreshed = await ProjectConversation.find_one(
            ProjectConversation.owner == two_profiles["a"].owner_id(),
            ProjectConversation.project_id == project.id,
        )
        assert len(refreshed.turns) == 2
        assert refreshed.turns[1].role == "assistant"
        assert refreshed.turns[1].content == "second"

    async def test_clear_conversation(self, client, two_profiles):
        """DELETE resets turns and compaction state."""
        project = await _project(two_profiles["a"].owner_id())
        convo = ProjectConversation(
            owner=two_profiles["a"].owner_id(),
            project_id=project.id,
            turns=[ConversationTurn(role="user", content="hi", created_at=utcnow())],
        )
        await convo.insert()

        response = await client.delete(
            f"/api/v1/projects/{project.id}/agent/conversation",
            headers=two_profiles["a_headers"],
        )
        assert response.status_code == 204

        refreshed = await ProjectConversation.get(convo.id)
        assert refreshed.turns == []
        assert refreshed.compacted_summary is None
        assert refreshed.compacted_through == 0

    async def test_clear_with_no_existing_conversation_is_not_an_error(
        self, client, two_profiles
    ):
        project = await _project(two_profiles["a"].owner_id())
        response = await client.delete(
            f"/api/v1/projects/{project.id}/agent/conversation",
            headers=two_profiles["a_headers"],
        )
        assert response.status_code == 204

    async def test_cannot_clear_another_owners_conversation(self, client, two_profiles):
        project = await _project(two_profiles["a"].owner_id())
        convo = ProjectConversation(
            owner=two_profiles["a"].owner_id(),
            project_id=project.id,
            turns=[ConversationTurn(role="user", content="hi", created_at=utcnow())],
        )
        await convo.insert()

        response = await client.delete(
            f"/api/v1/projects/{project.id}/agent/conversation",
            headers=two_profiles["b_headers"],
        )
        # Owner B's delete hits a different (empty) conversation -- 204, but
        # owner A's data survives.
        assert response.status_code == 204
        refreshed = await ProjectConversation.get(convo.id)
        assert len(refreshed.turns) == 1

    async def test_assistant_turn_saved_on_done_via_sse(
        self, two_profiles, spawn, live_server
    ):
        """The SSE stream saves the assistant's full response when 'done' fires."""
        project = await _project(two_profiles["a"].owner_id())
        headers = two_profiles["a_headers"]
        owner = two_profiles["a"].owner_id()
        profile_id = two_profiles["a"].id
        url = f"{live_server}/api/v1/projects/{project.id}/agent/events"

        async with AsyncClient(timeout=None) as http:
            async with http.stream("GET", url, params={"profile": str(profile_id)}) as stream:
                events = _sse_events(stream)
                async with asyncio.timeout(10):
                    await anext(events)  # agent_status

                # Ask -- triggers the user-turn background save.
                await http.post(
                    f"{live_server}/api/v1/projects/{project.id}/agent/ask",
                    json={"message": "what pipelines can I run?"},
                    headers=headers,
                )
                await asyncio.sleep(0.1)  # let user-turn save complete

                # Verify user turn saved.
                convo = await ProjectConversation.find_one(
                    ProjectConversation.owner == owner,
                    ProjectConversation.project_id == project.id,
                )
                assert convo is not None
                assert len(convo.turns) == 1
                assert convo.turns[0].role == "user"

                _, spawned = spawn
                fake = spawned[0]
                fake.stdout.feed(
                    {"type": "response", "command": "prompt", "success": True}
                )
                fake.stdout.feed({"type": "agent_start"})
                fake.stdout.feed(
                    {"type": "message_update",
                     "assistantMessageEvent": {
                         "type": "text_delta", "contentIndex": 0, "delta": "You can run QC."}}
                )
                fake.stdout.feed(
                    {"type": "tool_execution_start", "toolCallId": "c1",
                     "toolName": "mcp",
                     "args": {"tool": "bioflow_list_objects", "args": {}}}
                )
                fake.stdout.feed(
                    {"type": "tool_execution_end", "toolCallId": "c1",
                     "toolName": "mcp", "isError": False,
                     "args": {"tool": "bioflow_list_objects", "args": {}},
                     "result": {"content": [{"type": "text", "text": "3 files"}]}}
                )
                fake.stdout.feed(
                    {"type": "message_update",
                     "assistantMessageEvent": {
                         "type": "text_delta", "contentIndex": 0, "delta": " Try fastp."}}
                )
                fake.stdout.feed({"type": "agent_settled"})

                # Consume the SSE events up to 'done'.
                async with asyncio.timeout(10):
                    for _ in range(8):
                        etype, _ = await anext(events)
                        if etype == "done":
                            break
                    else:
                        assert False, "did not see 'done' event"

                # Let the assistant-turn background save complete.
                await asyncio.sleep(0.2)

                refreshed = await ProjectConversation.find_one(
                    ProjectConversation.owner == owner,
                    ProjectConversation.project_id == project.id,
                )
                assert refreshed is not None
                assert len(refreshed.turns) == 2
                assistant_turn = refreshed.turns[1]
                assert assistant_turn.role == "assistant"
                assert "You can run QC. Try fastp." in assistant_turn.content
                assert assistant_turn.tool_calls is not None
                assert len(assistant_turn.tool_calls) == 1
                assert assistant_turn.tool_calls[0].name == "bioflow_list_objects"
                assert assistant_turn.tool_calls[0].ok is True
                assert "3 files" in (assistant_turn.tool_calls[0].result or "")

    async def test_save_turn_with_tool_calls_round_trips(
        self, client, two_profiles, spawn
    ):
        """POST /conversation/turns accepts and persists tool_calls in the turn."""
        from app.models.conversation import ToolCallTurn

        project = await _project(two_profiles["a"].owner_id())
        # Pre-seed empty conversation
        convo = ProjectConversation(
            owner=two_profiles["a"].owner_id(),
            project_id=project.id,
        )
        await convo.insert()

        response = await client.post(
            f"/api/v1/projects/{project.id}/agent/conversation/turns",
            json={
                "role": "assistant",
                "content": "I found your files.",
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "bioflow_list_objects",
                        "args": {"project_id": str(project.id)},
                        "result": "3 files",
                        "ok": True,
                    }
                ],
            },
            headers=two_profiles["a_headers"],
        )
        assert response.status_code == 201
        assert response.json()["tool_calls"][0]["name"] == "bioflow_list_objects"
        assert response.json()["tool_calls"][0]["ok"] is True

        refreshed = await ProjectConversation.get(convo.id)
        assert len(refreshed.turns) == 1
        assert refreshed.turns[0].tool_calls is not None
        assert refreshed.turns[0].tool_calls[0].name == "bioflow_list_objects"
        assert refreshed.turns[0].tool_calls[0].args == {"project_id": str(project.id)}
        assert refreshed.turns[0].tool_calls[0].result == "3 files"
        assert refreshed.turns[0].tool_calls[0].ok is True
