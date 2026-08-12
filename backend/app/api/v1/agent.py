"""The in-app agent's HTTP surface: ask, stream, stop, restart.

The drawer talks to pi through two channels. `POST /ask` is fire-and-forget:
it spawns the agent process on first use and returns "accepted" immediately
-- everything after that arrives on the SSE stream, including rejections
(no model configured) and hangs. `GET /events` is the stream, opened by the
drawer via `EventSource`, which is why the profile travels as a query
parameter here rather than in a header.

The stream is the re-attaching kind: it is opened before any process exists
(the drawer opens first; the user's first prompt spawns pi), and it must
survive the process being stopped or dying, or the drawer would freeze while
the agent silently restarts behind it. The loop waits for a process, attaches
to it, forwards its translated events, and re-attaches when that process
ends (stop, restart, crash) -- matching `agent.events()`'s `__stop__`
sentinel, which `AgentProcess.stop()` and the crash watcher both put.

Agent processes are keyed by (profile, project) and scoped accordingly: the
MCP connection embedded in the spawned config carries this profile's id, so
the agent can only reach what the MCP server would give that profile.

Conversation persistence is handled by Pi's own session layer
(--session-id / --session-dir). The agent's memory survives process death,
idle reaping, and container restarts. The visible transcript is held in the
drawer's in-memory state only; on reopen the panel says "Resuming an earlier
conversation" because the agent remembers but the scrollback is lost.
"""

import asyncio
import json

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from app.api.deps import LinkableOwnerDep, OwnerDep, ProfileIdDep
from app.services import project_service, search_service
from app.services.agent_service import agent_service

router = APIRouter(prefix="/projects/{project_id}/agent", tags=["agent"])

# Without traffic, proxies and browsers eventually drop an idle stream; the
# same value the /events router uses for its keepalive.
KEEPALIVE_SECONDS = 20

# How often the stream re-checks for a process while waiting for the user's
# first /ask to spawn one.
WAIT_POLL_SECONDS = 0.5


class AgentAskRequest(BaseModel):
    message: str = Field(min_length=1)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class AgentAskResponse(BaseModel):
    status: str


async def _build_project_context(project) -> str:
    """Generate a compact summary of the project for the agent's context.

    Called once per process spawn (lazy, on first /ask). The summary is injected
    into the system prompt so the agent can answer the first question without
    burning a tool call on discovery.

    Async because both services below are: called synchronously they return
    coroutines that are never awaited, and the first attribute access on one
    ("'coroutine' object has no attribute 'values'") turns every /ask and
    /restart into a 500.
    """
    kinds = await search_service.count_by_kind(project.id, owner=project.owner)
    recent = await project_service.recent_jobs(project.id, limit=5)

    lines = [
        f"Project: {project.name} (id: {project.id})",
        f"Objects: {sum(kinds.values())} total — "
        + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items())),
    ]
    if recent:
        lines.append(f"Recent jobs: {len(recent)} completed")
    return "\n".join(lines)


async def _system_prompt(project) -> str:
    """Project context for the agent, set at spawn time only.

    The default block is infrastructure grounding -- which project this is and
    that MCP tools exist -- so it is always present and always owned by this
    code. A project's `agent_system_prompt` is appended to it rather than
    replacing it: a user asking for a different tone must not be able to
    silently discard tool awareness, and a stored copy of the grounding text
    would freeze at whatever it said the day it was edited.

    The project context summary is appended after the custom prompt so the agent
    has a picture of the project's contents from the first turn.
    """
    base = (
        "You are a bioinformatics coding agent inside BioFlow, a local "
        f"bioinformatics data manager. You are working on the project "
        f"{project.name!r} (id {project.id}). You have MCP tools to read this "
        "project's data, run QC/trim/align/assemble pipelines, and inspect "
        "jobs. Prefer running a tool over describing what you would do, and "
        "keep answers concrete and short."
    )
    context = await _build_project_context(project)
    custom = (project.agent_system_prompt or "").strip()

    parts = [base, f"Project snapshot:\n{context}"]
    if custom:
        parts.append(f"Additional instructions from the user:\n{custom}")
    return "\n\n".join(parts)


@router.post("/ask", response_model=AgentAskResponse)
async def ask_agent(
    project_id: PydanticObjectId,
    body: AgentAskRequest,
    owner: OwnerDep,
    profile_id: ProfileIdDep,
) -> AgentAskResponse:
    """Accept one prompt; outcomes arrive on the /events stream.

    The ownership-scoped project lookup is what 404s an unknown or foreign
    project, and its name goes into the spawn-time system prompt. Spawning is
    lazy, so the first ask pays for the pi subprocess.
    """
    project = await project_service.get_project(project_id, owner=owner)
    agent = await agent_service.get_or_create(
        profile_id,
        str(project_id),
        system_prompt=await _system_prompt(project),
    )
    await agent.send_prompt(body.message)
    return AgentAskResponse(status="accepted")


@router.get("/events")
async def agent_events(
    project_id: PydanticObjectId,
    profile_id: ProfileIdDep,
    owner: LinkableOwnerDep,
):
    """The drawer's SSE stream; resolves the profile before the 200, like the
    /events router, so a stale profile id is a 400 rather than a dead stream.
    """
    running = _live_process(profile_id, str(project_id)) is not None

    async def generator():
        async for item in _agent_stream(profile_id, str(project_id), running):
            yield item

    return EventSourceResponse(generator())


async def _agent_stream(profile_id: str, project_id: str, running: bool):
    """Yield SSE events from the Pi agent process.

    The keepalive ping and re-attach loop from the original implementation
    are preserved unchanged. No Mongo persistence -- Pi's session layer is
    the source of truth for conversation history.
    """
    yield {"event": "agent_status", "data": json.dumps({"running": running})}

    while True:
        agent = _live_process(profile_id, project_id)
        if agent is None:
            await asyncio.sleep(WAIT_POLL_SECONDS)
            continue

        events = agent.events()

        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        events.__anext__(), timeout=KEEPALIVE_SECONDS
                    )
                except TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue

                yield {"event": event.type, "data": json.dumps(event.data)}
        except StopAsyncIteration:
            # The process was stopped or died; attach to its replacement.
            continue


def _live_process(profile_id: str, project_id: str):
    """The process for this pair, or None when absent or dead."""
    agent = agent_service.get(profile_id, project_id)
    if agent is None or agent.process is None:
        return None
    return agent


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def stop_agent(project_id: PydanticObjectId, profile_id: ProfileIdDep) -> None:
    """Stop the agent process (and the drawer's stream ends; EventSource and
    the re-attaching loop above both recover on the next ask)."""
    await agent_service.stop_agent(profile_id, str(project_id))


@router.post("/restart", response_model=AgentAskResponse)
async def restart_agent(
    project_id: PydanticObjectId, owner: OwnerDep, profile_id: ProfileIdDep
) -> AgentAskResponse:
    """Stop and respawn, keeping the project's composed prompt.

    The ownership-scoped lookup is here for the prompt, not just the 404: a
    respawn that forwarded nothing would drop the project grounding, which is
    what this endpoint used to do.
    """
    project = await project_service.get_project(project_id, owner=owner)
    await agent_service.restart_agent(
        profile_id, str(project_id), system_prompt=await _system_prompt(project)
    )
    return AgentAskResponse(status="restarting")


@router.post("/new-session", response_model=AgentAskResponse)
async def new_agent_session(
    project_id: PydanticObjectId, profile_id: ProfileIdDep
) -> AgentAskResponse:
    """Clear the conversation: stop the process and delete its session file.

    Distinct from /restart, which respawns against the same session and so
    keeps the agent's memory.
    """
    await agent_service.new_session(profile_id, str(project_id))
    return AgentAskResponse(status="cleared")
