"""The in-app Pi coding agent: one subprocess per (profile, project).

The backend spawns `pi --mode rpc` as a subprocess and speaks its JSONL
protocol over stdin/stdout (see pi's docs/rpc.md for the exact shapes --
they are the contract this module translates to SSE events, which is why the
pi version is pinned in the Dockerfile). The agent reaches BioFlow's data
through the existing MCP server via pi-mcp-adapter's `--mcp-config` flag: a
per-process temp file points at `http://localhost:8000/api/v1/mcp?profile=...`.

Design notes, from docs/superpowers/specs/2026-08-09-ai-agent-harness-design.md:

- Processes are per (profile, project) -- the MCP connection is profile-scoped
  and the conversation is project-scoped, so sharing a process across either
  boundary would leak context between users or projects.
- Spawning is lazy: nothing starts until the first prompt.
- `streamingBehavior: "steer"` is sent with every prompt. Pi ignores it when
  idle and requires it when streaming (otherwise the prompt command is
  rejected), so sending it unconditionally removes a race between the backend
  tracking "is it streaming" and the agent's actual state.
- Tool calls through pi-mcp-adapter arrive as `toolName: "mcp"` with the real
  bioflow_* tool nested in `args.tool` -- the translation layer unwraps that
  so the UI shows `bioflow_get_job`, not `mcp`.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from asyncio import create_subprocess_exec
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.errors import AgentUnavailableError
from app.logging import get_logger

log = get_logger(__name__)

# The api service listens on 8000 inside the container (see
# docker-compose.yml); the MCP server is mounted on the same FastAPI app at
# /api/v1/mcp, so the agent can reach it over loopback with no proxy config.
_MCP_BASE_URL = "http://localhost:8000"

# Commands whose responses must be surfaced to the UI even on success:false.
# Everything else that fails is a service problem, not a user-facing one.
_ACK_COMMANDS = frozenset({"prompt"})


@dataclass
class AgentEvent:
    """One event destined for the drawer's SSE stream, already translated."""

    type: str
    data: dict = field(default_factory=dict)


class AgentProcess:
    """One Pi subprocess for one (profile, project) pair."""

    def __init__(
        self,
        *,
        profile_id: str,
        project_id: str,
        mcp_config: dict,
        pi_path: str,
        response_timeout: float,
        sessions_dir: Path,
        system_prompt: str | None = None,
    ) -> None:
        self.profile_id = profile_id
        self.project_id = project_id
        self._mcp_config = mcp_config
        self._pi_path = pi_path
        self._response_timeout = response_timeout
        self._sessions_dir = sessions_dir
        self._system_prompt = system_prompt
        # Read once, at spawn: it becomes an argv element in start(). The
        # service compares against this to decide whether a live process is
        # still running the caller's prompt.
        self.spawned_with_prompt = system_prompt

        self.process: asyncio.subprocess.Process | None = None
        self._queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._mcp_config_file: str | None = None
        self._stopping = False
        self._busy = False
        self._run_started_at: float | None = None
        self._last_activity = time.monotonic()
        self._readers: list[asyncio.Task] = []
        self._watchdog_task: asyncio.Task | None = None
        self._spawn_error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Write the MCP config to a temp file and spawn pi.

        `create_subprocess_exec` is imported at module level so tests can
        patch it; the process's stdin/stdout are the JSONL protocol pipes.
        """
        fd, path = tempfile.mkstemp(prefix="bioflow-agent-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self._mcp_config, fh)
        except BaseException:
            os.unlink(path)
            raise
        self._mcp_config_file = path

        # Sessions replace --no-session: pi persists the conversation itself,
        # keyed by (profile, project), so a process lost to the idle reaper,
        # a crash, or an api restart reloads its memory on respawn.
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._pi_path,
            "--mode",
            "rpc",
            "--session-dir",
            str(self._sessions_dir),
            "--session-id",
            session_id_for(self.profile_id, self.project_id),
            "--mcp-config",
            path,
        ]
        if self._system_prompt:
            cmd += ["--system-prompt", self._system_prompt]

        try:
            self.process = await create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            self._spawn_error = (
                f"pi binary not found at {self._pi_path} -- is it installed in the "
                "api image? (backend/Dockerfile)"
            )
            log.error("agent_spawn_failed", error=self._spawn_error)
            raise AgentUnavailableError(self._spawn_error) from None
        except OSError as e:
            self._spawn_error = f"failed to spawn pi: {e}"
            log.error("agent_spawn_failed", error=self._spawn_error)
            raise AgentUnavailableError(self._spawn_error) from e

        self._readers = [
            asyncio.create_task(self._read_stdout(), name=f"agent-{self.project_id}-stdout"),
            asyncio.create_task(self._read_stderr(), name=f"agent-{self.project_id}-stderr"),
            asyncio.create_task(self._watch_process(), name=f"agent-{self.project_id}-watch"),
        ]

    async def stop(self) -> None:
        """Terminate the subprocess, drain the readers, remove the temp file."""
        self._stopping = True
        proc = self.process
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        for task in self._readers:
            task.cancel()
        await asyncio.gather(*self._readers, return_exceptions=True)
        self._readers = []
        self.process = None
        await self._put_stop()
        if self._mcp_config_file:
            try:
                os.unlink(self._mcp_config_file)
            except OSError:
                pass
            self._mcp_config_file = None

    async def restart(self) -> None:
        await self.stop()
        self._stopping = False
        await self.start()

    # --- interaction -------------------------------------------------------

    async def send_prompt(self, message: str) -> None:
        """Write one `prompt` command and return; outcomes arrive as events.

        The command is fire-and-forget by design (the router already answered
        "accepted" to the browser): a rejection (`success: false`, e.g. no
        model configured) surfaces as an SSE error event via the stdout
        reader, and the response watchdog is armed by the reader on
        acceptance.
        """
        if self.process is None or self.process.stdin is None:
            raise AgentUnavailableError("Agent is not running -- restart it and try again.")

        line = json.dumps(
            {
                "type": "prompt",
                "message": message,
                "streamingBehavior": "steer",
            }
        )
        self.process.stdin.write((line + "\n").encode())
        await self.process.stdin.drain()

    async def events(self) -> AsyncIterator[AgentEvent]:
        """Yield translated events until the process stops."""
        while True:
            event = await self._queue.get()
            if event.type == "__stop__":
                return
            yield event

    # --- stdout protocol ---------------------------------------------------

    async def _read_stdout(self) -> None:
        """Parse pi's JSONL output: `response` lines are command acks
        (rejections surface as error events); everything else is an event to
        translate."""
        assert self.process is not None and self.process.stdout is not None
        while True:
            raw = await self.process.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip("\r\n")
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                log.warning("agent_bad_json", line=line[:200])
                continue
            self._last_activity = time.monotonic()
            if payload.get("type") == "response":
                self._handle_response(payload)
            else:
                await self._translate(payload)

    def _handle_response(self, payload: dict) -> None:
        """A `response` line: rejections become error events, and a prompt
        acceptance arms the response watchdog for the run it starts."""
        command = payload.get("command")
        if payload.get("success") is False:
            if command in _ACK_COMMANDS:
                message = payload.get("message") or f"Agent rejected the {command} command."
                asyncio.create_task(self._put_event("error", {"message": message}))
            return
        if command == "prompt":
            # Accepted: the run starts now; if it neither starts nor settles
            # within the timeout, the watchdog reports the hang.
            self._run_started_at = time.monotonic()
            self._watchdog_task = asyncio.create_task(
                self._response_watchdog(), name=f"agent-{self.project_id}-watchdog"
            )

    async def _translate(self, payload: dict) -> None:
        """Map one pi RPC event to one (or zero) SSE events for the drawer."""
        etype = payload.get("type")

        if etype == "agent_start":
            self._busy = True
            await self._put_event("agent_start", {})
        elif etype == "agent_settled":
            self._busy = False
            self._run_started_at = None
            await self._put_event("done", {})
        elif etype == "message_update":
            update = payload.get("assistantMessageEvent") or {}
            kind = update.get("type")
            if kind == "text_delta":
                await self._put_event(
                    "message_delta",
                    {
                        "kind": "text",
                        "contentIndex": update.get("contentIndex", 0),
                        "delta": update.get("delta", ""),
                    },
                )
            elif kind == "thinking_delta":
                await self._put_event(
                    "message_delta",
                    {"kind": "thinking", "delta": update.get("delta", "")},
                )
            # text_start/text_end/thinking_start/thinking_end/toolcall_* are
            # framing the drawer does not need for the first slice.
        elif etype == "tool_execution_start":
            await self._put_event("tool_call", _tool_call_payload(payload))
        elif etype == "tool_execution_end":
            data = _tool_call_payload(payload)
            data["ok"] = not payload.get("isError", False)
            result = payload.get("result") or {}
            content = result.get("content") or []
            text = "".join(
                block.get("text", "") for block in content if block.get("type") == "text"
            )
            if text:
                data["summary"] = text[:300]
            await self._put_event("tool_result", data)
        elif etype == "turn_end":
            # A provider failure lands here, not in an error event of its own:
            # without this the drawer sees agent_start -> done with no text and
            # reads a hard failure as a successful empty response.
            message = payload.get("message") or {}
            error_message = message.get("errorMessage")
            if error_message:
                await self._put_event("error", {"message": error_message})
        elif etype == "extension_error":
            await self._put_event(
                "error", {"message": f"Agent extension error: {payload.get('message')}"}
            )

    # --- supervision -------------------------------------------------------

    async def _read_stderr(self) -> None:
        """pi's stderr is diagnostics only; log it, never surface it."""
        assert self.process is not None and self.process.stderr is not None
        while True:
            raw = await self.process.stderr.readline()
            if not raw:
                break
            log.warning("agent_stderr", line=raw.decode(errors="replace").rstrip())

    async def _watch_process(self) -> None:
        """Emit an error event if pi dies on its own (not via stop())."""
        assert self.process is not None
        returncode = await self.process.wait()
        self.process = None
        # Retire any armed watchdog: the death message below is the reason,
        # and a later "did not finish" would just be noise on top of it.
        self._run_started_at = None
        if not self._stopping:
            await self._put_event(
                "error",
                {"message": f"Agent process exited unexpectedly (code {returncode})."},
            )
            # End the events stream so the router's re-attaching loop can
            # pick up the replacement process (spawned by the next /ask).
            await self._put_stop()

    async def _response_watchdog(self) -> None:
        """If a run neither starts nor settles within the timeout, say so."""
        started = self._run_started_at
        if started is None:
            return
        elapsed = time.monotonic() - started
        if elapsed < self._response_timeout:
            await asyncio.sleep(self._response_timeout - elapsed)
        # agent_settled clears _run_started_at, so a settled run is done; a
        # second prompt re-arms it, retiring this watchdog. Anything else
        # still outstanding after the timeout is a hang worth reporting --
        # including a run that never even started.
        if self._stopping or self._run_started_at != started:
            return
        self._run_started_at = None
        await self._put_event(
            "error",
            {"message": f"Agent did not finish within {int(self._response_timeout)}s."},
        )

    # --- helpers -----------------------------------------------------------

    async def _put_event(self, etype: str, data: dict) -> None:
        await self._queue.put(AgentEvent(type=etype, data=data))

    async def _put_stop(self) -> None:
        await self._queue.put(AgentEvent(type="__stop__"))


def session_id_for(profile_id: str, project_id: str) -> str:
    """The pi session id for one (profile, project) pair.

    Stable across respawns -- that is what lets a reaped or crashed process
    reload the conversation -- and distinct across both axes, since sharing
    an id between profiles would leak one user's conversation into another's.
    """
    return f"bioflow-{profile_id}-{project_id}"


def _tool_call_payload(payload: dict) -> dict:
    """Translate one tool event; unwrap the mcp proxy's nested tool name."""
    name = payload.get("toolName") or "unknown"
    args = payload.get("args") or {}
    if name == "mcp" and isinstance(args, dict) and args.get("tool"):
        # pi-mcp-adapter exposes every MCP server through one proxy tool:
        # toolName is "mcp" and the real call is nested in args.
        return {
            "id": payload.get("toolCallId"),
            "name": args["tool"],
            "args": args.get("args") or {},
        }
    return {"id": payload.get("toolCallId"), "name": name, "args": args}


class AgentService:
    """One AgentProcess per (profile, project); lazy spawn on first prompt."""

    def __init__(
        self,
        *,
        pi_path: str | None = None,
        mcp_base_url: str = _MCP_BASE_URL,
        extra_mcp_servers: dict | None = None,
        response_timeout: float | None = None,
        idle_timeout: float | None = None,
        sessions_dir: Path | None = None,
    ) -> None:
        self._pi_path = pi_path or settings.pi_path
        self._mcp_base_url = mcp_base_url
        self._extra_mcp_servers = extra_mcp_servers
        self._response_timeout = response_timeout or settings.agent_response_timeout
        self._idle_timeout = idle_timeout or settings.agent_idle_timeout
        self._sessions_dir = sessions_dir or settings.agent_sessions_dir
        self._processes: dict[tuple[str, str], AgentProcess] = {}

    def _key(self, profile_id: str, project_id: str) -> tuple[str, str]:
        return (profile_id, str(project_id))

    def get(self, profile_id: str, project_id: str) -> AgentProcess | None:
        return self._processes.get(self._key(profile_id, project_id))

    async def get_or_create(
        self,
        profile_id: str,
        project_id: str,
        *,
        system_prompt: str | None = None,
    ) -> AgentProcess:
        """Return the existing process or spawn a fresh one (lazy: only ever
        called from /ask, never from opening the drawer)."""
        key = self._key(profile_id, project_id)
        proc = self._processes.get(key)
        if proc is not None and proc.process is not None:
            if proc.spawned_with_prompt == system_prompt:
                return proc
            if proc._busy:
                # A prompt change can't reach a running process, but this one
                # is mid-response -- killing it would silently discard
                # whatever it's generating. Let it finish under the old
                # prompt; the mismatch will be caught again next time this
                # key is requested, once the process is idle.
                return proc
            # pi takes its system prompt as an argv element, so a changed
            # prompt cannot reach a running process. Replace it rather than
            # answering the next message under the old instructions.
            log.info("agent_prompt_changed", profile=profile_id, project=str(project_id))
            await self.stop_agent(profile_id, project_id)
            proc = None
        if proc is not None:
            # Dead process from a crashed pi; reap it and start over.
            self._processes.pop(key, None)

        proc = AgentProcess(
            profile_id=profile_id,
            project_id=str(project_id),
            mcp_config=self._build_mcp_config(profile_id),
            pi_path=self._pi_path,
            response_timeout=self._response_timeout,
            sessions_dir=self._sessions_dir,
            system_prompt=system_prompt,
        )
        await proc.start()
        self._processes[key] = proc
        log.info("agent_spawned", profile=profile_id, project=str(project_id))
        return proc

    async def send_message(self, profile_id: str, project_id: str, message: str) -> None:
        proc = self.get(profile_id, project_id)
        if proc is None:
            raise AgentUnavailableError("Agent is not running -- send a prompt first.")
        await proc.send_prompt(message)

    async def stop_agent(self, profile_id: str, project_id: str) -> None:
        proc = self._processes.pop(self._key(profile_id, project_id), None)
        if proc is not None:
            await proc.stop()
            log.info("agent_stopped", profile=profile_id, project=str(project_id))

    async def restart_agent(
        self, profile_id: str, project_id: str, *, system_prompt: str | None = None
    ) -> AgentProcess:
        """Stop and respawn.

        `system_prompt` is forwarded because the respawned process gets its
        prompt only from here -- omitting it (as this method used to) drops
        the project grounding, leaving a restarted agent with no idea which
        project it is in.
        """
        await self.stop_agent(profile_id, project_id)
        return await self.get_or_create(profile_id, project_id, system_prompt=system_prompt)

    async def new_session(self, profile_id: str, project_id: str) -> None:
        """Stop the process and forget the conversation.

        The counterpart to restart_agent, which keeps the session file: this
        is the only way to clear a context that has gone wrong. pi names
        session files "{timestamp}_{session-id}.jsonl", so matching is a
        suffix glob, not a prefix one -- a prefix match on the session id
        would hit nothing, since the id never starts the filename.
        """
        await self.stop_agent(profile_id, str(project_id))
        sid = session_id_for(profile_id, str(project_id))
        if not self._sessions_dir.exists():
            return
        for path in self._sessions_dir.glob(f"*_{sid}.jsonl"):
            try:
                path.unlink()
            except OSError as e:
                log.warning("agent_session_unlink_failed", path=str(path), error=str(e))

    async def cleanup_idle(self) -> None:
        """Stop processes that have been silent (and not mid-run) past the
        idle timeout. The api's lifespan calls this on a schedule."""
        now = time.monotonic()
        for key, proc in list(self._processes.items()):
            if proc._busy:
                continue
            idle_for = now - proc._last_activity
            if idle_for > self._idle_timeout:
                log.info("agent_idle_reaped", profile=key[0], project=key[1])
                await self.stop_agent(key[0], key[1])

    async def shutdown_all(self) -> None:
        """Stop every process; called from the api lifespan on shutdown."""
        keys = list(self._processes)
        for profile_id, project_id in keys:
            await self.stop_agent(profile_id, project_id)

    def _build_mcp_config(self, profile_id: str) -> dict:
        config = {
            "mcpServers": {
                "bioflow": {"url": f"{self._mcp_base_url}/api/v1/mcp?profile={profile_id}"}
            }
        }
        if self._extra_mcp_servers:
            config["mcpServers"].update(self._extra_mcp_servers)
        return config


# The one instance every router uses; tests construct their own.
agent_service = AgentService()
