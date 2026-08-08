"""Tests for the in-app Pi agent subprocess service.

Pure unit tests: `create_subprocess_exec` is patched with a fake process
whose stdout is fed scripted JSONL lines, so no pi binary is needed (the
worktree test image predates the pi install). The scripted lines follow the
real RPC shapes in pi's docs/rpc.md -- the translation layer is the thing
under test, so feeding it the documented protocol is the point.
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.errors import AgentUnavailableError
from app.services.agent_service import AgentEvent, AgentProcess, AgentService


class FakeWriter:
    def __init__(self):
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass


class FakeReader:
    """readline() blocks on an asyncio queue; EOF is b'' (the real protocol's
    EOF marker)."""

    def __init__(self):
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()
        self._eof = False

    def feed(self, payload: dict | str | bytes) -> None:
        if isinstance(payload, bytes):
            line = payload
        elif isinstance(payload, str):
            line = payload.encode()
        else:
            line = (json.dumps(payload) + "\n").encode()
        self._lines.put_nowait(line)

    def eof(self) -> None:
        self._lines.put_nowait(b"")

    async def readline(self) -> bytes:
        return await self._lines.get()


class FakeProcess:
    def __init__(self):
        self.stdin = FakeWriter()
        self.stdout = FakeReader()
        self.stderr = FakeReader()
        self.returncode: int | None = None
        self._waiters: list[asyncio.Future] = []

    async def wait(self) -> int:
        if self.returncode is not None:
            return self.returncode
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        return await fut

    def terminate(self) -> None:
        if self.returncode is None:
            self.returncode = -15
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(self.returncode)
        self.stdout.eof()

    async def kill(self) -> None:
        self.terminate()


def make_service(**kwargs) -> AgentService:
    kwargs.setdefault("pi_path", "/usr/local/bin/pi")
    kwargs.setdefault("response_timeout", 60)
    kwargs.setdefault("idle_timeout", 3600)
    return AgentService(**kwargs)


def parse_prompts(fake: FakeProcess) -> list[dict]:
    return [
        json.loads(line)
        for line in fake.stdin.written.decode().strip().splitlines()
        if line.strip()
    ]


def read_config(path: str) -> dict:
    """Sync helper: async tests must not do blocking file I/O inline."""
    return json.loads(Path(path).read_text())


def config_file_exists(path: str) -> bool:
    return Path(path).exists()


async def collect(proc: AgentProcess, n: int) -> list[AgentEvent]:
    """Consume the first n events from the process's stream."""
    out: list[AgentEvent] = []
    async for event in proc.events():
        out.append(event)
        if len(out) >= n:
            break
    return out


@pytest.fixture
def spawn(monkeypatch):
    """Patch the spawn seam; returns a (spawned FakeProcess, args list)."""
    spawned: FakeProcess | None = None
    calls: list[list[str]] = []

    async def fake_spawn(*cmd, **kwargs):
        nonlocal spawned
        calls.append(list(cmd))
        spawned = FakeProcess()
        return spawned

    monkeypatch.setattr(
        "app.services.agent_service.create_subprocess_exec", fake_spawn
    )
    return lambda: (calls, spawned)


class TestBuildMcpConfig:
    def test_bioflow_server_points_at_mcp_with_the_profile(self):
        service = make_service()
        config = service._build_mcp_config("64f1a2b3c4d5e6f7")
        assert config["mcpServers"]["bioflow"] == {
            "url": "http://localhost:8000/api/v1/mcp?profile=64f1a2b3c4d5e6f7"
        }

    def test_extra_servers_are_merged_in(self):
        extra = {"ncbi": {"url": "https://x", "lifecycle": "lazy"}}
        service = make_service(extra_mcp_servers=extra)
        config = service._build_mcp_config("p1")
        assert config["mcpServers"]["ncbi"] == {"url": "https://x", "lifecycle": "lazy"}
        assert "bioflow" in config["mcpServers"]


class TestSpawn:
    async def test_spawns_with_rpc_flags_and_mcp_config(self, spawn):
        service = make_service()
        await service.get_or_create("prof-1", "proj-1")
        calls, fake = spawn()
        assert len(calls) == 1
        expected_head = [
            "/usr/local/bin/pi",
            "--mode",
            "rpc",
            "--no-session",
            "--mcp-config",
        ]
        assert calls[0][:5] == expected_head
        # The temp config file exists and carries the profile URL.
        config = read_config(calls[0][5])
        assert config["mcpServers"]["bioflow"]["url"].endswith("?profile=prof-1")
        assert fake is not None
        await service.stop_agent("prof-1", "proj-1")
        assert not config_file_exists(calls[0][5])

    async def test_system_prompt_is_passed_as_a_flag(self, spawn):
        service = make_service()
        proc = await service.get_or_create("p", "j", system_prompt="You are the agent.")
        calls, _ = spawn()
        assert "--system-prompt" in calls[0]
        assert calls[0][calls[0].index("--system-prompt") + 1] == "You are the agent."
        await proc.stop()

    async def test_spawn_failure_raises_agent_unavailable(self, monkeypatch):
        async def boom(*cmd, **kwargs):
            raise FileNotFoundError

        monkeypatch.setattr("app.services.agent_service.create_subprocess_exec", boom)
        service = make_service()
        with pytest.raises(AgentUnavailableError):
            await service.get_or_create("p", "j")

    async def test_same_profile_project_reuses_the_process(self, spawn):
        service = make_service()
        a = await service.get_or_create("p", "j")
        b = await service.get_or_create("p", "j")
        calls, _ = spawn()
        assert a is b
        assert len(calls) == 1

    async def test_different_projects_get_separate_processes(self, spawn):
        service = make_service()
        a = await service.get_or_create("p", "j1")
        b = await service.get_or_create("p", "j2")
        calls, _ = spawn()
        assert a is not b
        assert len(calls) == 2


class TestPrompt:
    async def test_prompt_line_has_steer_behavior(self, spawn):
        service = make_service()
        proc = await service.get_or_create("p", "j")
        await proc.send_prompt("What can I run?")
        lines = parse_prompts(spawn()[1])
        assert lines[-1]["type"] == "prompt"
        assert lines[-1]["message"] == "What can I run?"
        assert lines[-1]["streamingBehavior"] == "steer"
        await proc.stop()

    async def test_rejected_prompt_surfaces_as_an_error_event(self, spawn):
        service = make_service()
        proc = await service.get_or_create("p", "j")
        fake = spawn()[1]
        await proc.send_prompt("hi")
        fake.stdout.feed(
            {"type": "response", "command": "prompt", "success": False,
             "message": "No model configured."}
        )
        events = await collect(proc, 1)
        assert events[0].type == "error"
        assert "No model" in events[0].data["message"]
        await proc.stop()

    async def test_send_prompt_on_dead_process_raises(self, spawn):
        service = make_service()
        proc = await service.get_or_create("p", "j")
        await proc.stop()
        with pytest.raises(AgentUnavailableError):
            await proc.send_prompt("hi")


class TestEventTranslation:
    async def test_full_run_translates_to_drawer_events(self, spawn):
        service = make_service()
        proc = await service.get_or_create("p", "j")
        fake = spawn()[1]
        await proc.send_prompt("run qc")
        fake.stdout.feed({"type": "response", "command": "prompt", "success": True})
        fake.stdout.feed({"type": "agent_start"})
        fake.stdout.feed(
            {"type": "message_update",
             "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "Hello"}}
        )
        fake.stdout.feed(
            {"type": "message_update",
             "assistantMessageEvent": {"type": "thinking_delta", "delta": "hmm"}}
        )
        fake.stdout.feed(
            {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "mcp",
             "args": {"tool": "bioflow_list_objects", "args": {"project_id": "j"}}}
        )
        fake.stdout.feed(
            {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "mcp",
             "args": {"tool": "bioflow_list_objects", "args": {"project_id": "j"}},
             "result": {"content": [{"type": "text", "text": '{"ok": true}'}], "details": {}},
             "isError": False}
        )
        fake.stdout.feed({"type": "agent_settled"})

        events = await collect(proc, 6)
        types = [e.type for e in events]
        expected = [
            "agent_start", "message_delta", "message_delta",
            "tool_call", "tool_result", "done",
        ]
        assert types == expected
        assert events[1].data == {"kind": "text", "contentIndex": 0, "delta": "Hello"}
        assert events[2].data == {"kind": "thinking", "delta": "hmm"}
        # The mcp proxy tool name is unwrapped to the real bioflow_* tool.
        assert events[3].data["name"] == "bioflow_list_objects"
        assert events[3].data["args"] == {"project_id": "j"}
        assert events[4].data["ok"] is True
        assert events[5].type == "done"
        await proc.stop()

    async def test_failed_tool_is_marked(self, spawn):
        service = make_service()
        proc = await service.get_or_create("p", "j")
        fake = spawn()[1]
        fake.stdout.feed(
            {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bioflow_get_job",
             "args": {}, "result": {"content": [{"type": "text", "text": "boom"}], "details": {}},
             "isError": True}
        )
        events = await collect(proc, 1)
        assert events[0].type == "tool_result"
        assert events[0].data["ok"] is False
        assert events[0].data["name"] == "bioflow_get_job"
        await proc.stop()

    async def test_plain_tools_keep_their_own_name(self, spawn):
        service = make_service()
        proc = await service.get_or_create("p", "j")
        fake = spawn()[1]
        fake.stdout.feed(
            {"type": "tool_execution_start", "toolCallId": "c2", "toolName": "bash",
             "args": {"command": "ls"}}
        )
        events = await collect(proc, 1)
        assert events[0].data["name"] == "bash"
        assert events[0].data["args"] == {"command": "ls"}
        await proc.stop()


class TestLifecycle:
    async def test_unexpected_exit_emits_error(self, spawn):
        service = make_service()
        proc = await service.get_or_create("p", "j")
        fake = spawn()[1]
        fake.returncode = 1
        for fut in fake._waiters:
            if not fut.done():
                fut.set_result(1)
        events = await collect(proc, 1)
        assert events[0].type == "error"
        assert "exited unexpectedly" in events[0].data["message"]
        await proc.stop()

    async def test_response_timeout_emits_error(self, spawn):
        service = make_service(response_timeout=0.05)
        proc = await service.get_or_create("p", "j")
        fake = spawn()[1]
        await proc.send_prompt("hi")
        fake.stdout.feed({"type": "response", "command": "prompt", "success": True})
        # No agent_start ever arrives; the watchdog must report the hang.
        events = await collect(proc, 1)
        assert events[0].type == "error"
        assert "did not finish" in events[0].data["message"]
        await proc.stop()

    async def test_idle_cleanup_reaps_silent_but_not_busy(self, spawn):
        service = make_service(idle_timeout=0.05)
        await service.get_or_create("p", "j1")
        busy_proc = await service.get_or_create("p", "j2")
        spawn()[1].stdout.feed({"type": "agent_start"})
        busy_proc._busy = True
        await asyncio.sleep(0.1)
        await service.cleanup_idle()
        assert service.get("p", "j1") is None
        assert service.get("p", "j2") is busy_proc
        await busy_proc.stop()

    async def test_restart_spawns_a_fresh_process(self, spawn):
        service = make_service()
        first = await service.get_or_create("p", "j")
        await service.restart_agent("p", "j")
        calls, _ = spawn()
        assert len(calls) == 2
        second = service.get("p", "j")
        assert second is not None and second is not first
        await second.stop()

    async def test_shutdown_all_stops_everything(self, spawn):
        service = make_service()
        a = await service.get_or_create("p", "j1")
        b = await service.get_or_create("p", "j2")
        await service.shutdown_all()
        assert service.get("p", "j1") is None
        assert service.get("p", "j2") is None
        assert a.process is None and b.process is None


class TestSessionsDir:
    def test_sessions_dir_derives_from_bioinfo_home(self):
        from app.config import settings

        assert settings.agent_sessions_dir == settings.bioinfo_home / "agent-sessions"
