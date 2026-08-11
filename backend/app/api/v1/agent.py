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

Conversation persistence (issue #97): the user's question is saved to
`ProjectConversation` when `/ask` is accepted; the assistant's full
response (text + tool calls) is saved when the SSE stream sees `done`,
accumulating text deltas and tool-call/tool-result pairs between `agent_start`
and `done`. `GET /conversation` loads the transcript on drawer open;
`DELETE /conversation` clears it.
"""

import asyncio
import json
from collections import OrderedDict

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from app.api.deps import LinkableOwnerDep, OwnerDep, ProfileIdDep
from app.models.conversation import ConversationTurn, ProjectConversation, ToolCallTurn
from app.services import project_service
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

# --- Conversation persistence (issue #97) ---

class ToolCallTurnOut(BaseModel):
    id: str
    name: str
    args: dict = Field(default_factory=dict)
    result: str | None = None
    ok: bool | None = None


class ConversationTurnOut(BaseModel):
    role: str
    content: str
    tool_calls: list[ToolCallTurnOut] | None = None


class ConversationOut(BaseModel):
    turns: list[ConversationTurnOut]
    compacted_summary: str | None = None


class ConversationTurnIn(BaseModel):
    role: str
    content: str
    tool_calls: list[ToolCallTurnOut] | None = None


async def _get_or_create_conversation(
    project_id: PydanticObjectId, owner: str
) -> ProjectConversation:
    convo = await ProjectConversation.find_one(
        ProjectConversation.owner == owner,
        ProjectConversation.project_id == project_id,
    )
    if convo is None:
        convo = ProjectConversation(owner=owner, project_id=project_id)
        await convo.insert()
    return convo


async def _append_turn(
    project_id: PydanticObjectId,
    owner: str,
    role: str,
    content: str,
    tool_calls: list[ToolCallTurn] | None = None,
) -> None:
    """Append one turn to the conversation document and save it.

    Used at two points: when /ask accepts a user message (role="user"),
    and when the SSE stream sees `done` (role="assistant", with accumulated
    tool calls). Uses `$push` semantics via Beanie's `find_one + replace` --
    a single writer per (owner, project) makes a naive read-append-save safe
    here because the owner is profile-scoped: only one user can be typing at
    a time.
    """
    from app.models.base import utcnow

    convo = await _get_or_create_conversation(project_id, owner)
    convo.turns.append(
        ConversationTurn(
            role=role,
            content=content,
            created_at=utcnow(),
            tool_calls=tool_calls,
        )
    )
    await convo.save()


def _system_prompt(project) -> str:
    """Project context for the agent, set at spawn time only.

    The default block is infrastructure grounding -- which project this is and
    that MCP tools exist -- so it is always present and always owned by this
    code. A project's `agent_system_prompt` is appended to it rather than
    replacing it: a user asking for a different tone must not be able to
    silently discard tool awareness, and a stored copy of the grounding text
    would freeze at whatever it said the day it was edited.
    """
    base = (
        "You are a bioinformatics coding agent inside BioFlow, a local "
        f"bioinformatics data manager. You are working on the project "
        f"{project.name!r} (id {project.id}). You have MCP tools to read this "
        "project's data, run QC/trim/align/assemble pipelines, and inspect "
        "jobs. Prefer running a tool over describing what you would do, and "
        "keep answers concrete and short."
    )
    custom = (project.agent_system_prompt or "").strip()
    if not custom:
        return base
    return f"{base}\n\nAdditional instructions from the user:\n{custom}"

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

    Also persists the user's message to ProjectConversation so the
    transcript survives closing the drawer (issue #97).
    """
    project = await project_service.get_project(project_id, owner=owner)
    agent = await agent_service.get_or_create(
        profile_id,
        str(project_id),
        system_prompt=_system_prompt(project),
    )
    await agent.send_prompt(body.message)
    # Best-effort persistence: a failure here must not block the actual
    # prompt from reaching the agent. We intentionally do not raise.
    asyncio.create_task(_append_turn(project_id, owner, "user", body.message))
    return AgentAskResponse(status="accepted")

@router.get("/conversation", response_model=ConversationOut)
async def get_conversation(
    project_id: PydanticObjectId, owner: OwnerDep
) -> ConversationOut:
    """Load the saved agent conversation for this (profile, project).

    Creates an empty one on first access, matching the Q&A pattern so the
    drawer always has something to render. Owner-scoped: a different
    profile sees their own conversation, not a leak of another's.
    """
    convo = await _get_or_create_conversation(project_id, owner)
    return ConversationOut(
        turns=[
            ConversationTurnOut(
                role=t.role,
                content=t.content,
                tool_calls=[
                    ToolCallTurnOut(
                        id=tc.id,
                        name=tc.name,
                        args=tc.args,
                        result=tc.result,
                        ok=tc.ok,
                    )
                    for tc in (t.tool_calls or [])
                ] or None,
            )
            for t in convo.turns
        ],
        compacted_summary=convo.compacted_summary,
    )

@router.post(
    "/conversation/turns",
    response_model=ConversationTurnOut,
    status_code=status.HTTP_201_CREATED,
)
async def save_turn(
    project_id: PydanticObjectId,
    body: ConversationTurnIn,
    owner: OwnerDep,
) -> ConversationTurnOut:
    """Append a conversation turn.

    Primarily for the assistant turn: the SSE stream accumulates the full
    response (text + tool calls) between `agent_start` and `done`, then POSTs
    it here. The user turn is normally saved by `/ask`, but this endpoint is
    available for any incremental save the frontend needs.
    """
    tool_calls = (
        [
            ToolCallTurn(
                id=tc.id,
                name=tc.name,
                args=tc.args,
                result=tc.result,
                ok=tc.ok,
            )
            for tc in body.tool_calls
        ]
        if body.tool_calls
        else None
    )
    await _append_turn(project_id, owner, body.role, body.content, tool_calls)
    return ConversationTurnOut(
        role=body.role,
        content=body.content,
        tool_calls=body.tool_calls,
    )

@router.delete("/conversation", status_code=status.HTTP_204_NO_CONTENT)
async def clear_conversation(
    project_id: PydanticObjectId, owner: OwnerDep
) -> None:
    """Clear the agent conversation for this (profile, project).

    Resets turns, compaction state, and updated_at -- same semantics as the
    Q&A clear, on the same document type. Safe to call when no conversation
    exists yet.
    """
    convo = await ProjectConversation.find_one(
        ProjectConversation.owner == owner,
        ProjectConversation.project_id == project_id,
    )
    if convo is not None:
        convo.turns = []
        convo.compacted_summary = None
        convo.compacted_through = 0
        await convo.save()

@router.get("/events")
async def agent_events(
    project_id: PydanticObjectId,
    profile_id: ProfileIdDep,
    owner: LinkableOwnerDep,
):
    """The drawer's SSE stream; resolves the profile before the 200, like the
    /events router, so a stale profile id is a 400 rather than a dead stream.

    Also resolves the owner (via the same query param that EventSource sends)
    so the stream can persist the assistant's completed turn when `done`
    fires (issue #97).
    """
    running = _live_process(profile_id, str(project_id)) is not None

    async def generator():
        async for item in _agent_stream(
            profile_id, str(project_id), owner, running
        ):
            yield item

    return EventSourceResponse(generator())

async def _agent_stream(profile_id: str, project_id: str, owner: str, running: bool):
    """Yield SSE events, accumulating the assistant turn for persistence.

    Between `agent_start` and `done`, text deltas and tool calls are
    accumulated. On `done`, if any content or tool calls were collected,
    a single `ConversationTurn(role="assistant", ...)` is saved to Mongo.

    The keepalive ping and re-attach loop from the original implementation
    are preserved unchanged.
    """
    yield {"event": "agent_status", "data": json.dumps({"running": running})}

    while True:
        agent = _live_process(profile_id, project_id)
        if agent is None:
            await asyncio.sleep(WAIT_POLL_SECONDS)
            continue

        events = agent.events()

        # Per-run accumulation state.
        accumulated_text = ""
        tool_calls: OrderedDict[str, dict] = OrderedDict()
        in_run = False

        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        events.__anext__(), timeout=KEEPALIVE_SECONDS
                    )
                except TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue

                # --- accumulate for persistence ---
                if event.type == "agent_start":
                    in_run = True
                    accumulated_text = ""
                    tool_calls = OrderedDict()
                elif event.type == "message_delta" and event.data.get("kind") == "text":
                    if in_run:
                        accumulated_text += event.data.get("delta", "")
                elif event.type == "tool_call":
                    if in_run:
                        tc_id = event.data.get("id")
                        tool_calls[tc_id] = {
                            "id": tc_id,
                            "name": event.data.get("name", "unknown"),
                            "args": event.data.get("args", {}),
                            "result": None,
                            "ok": None,
                        }
                elif event.type == "tool_result":
                    if in_run:
                        tc_id = event.data.get("id")
                        if tc_id in tool_calls:
                            tool_calls[tc_id]["result"] = event.data.get("summary")
                            tool_calls[tc_id]["ok"] = event.data.get("ok")
                elif event.type == "done":
                    if in_run:
                        in_run = False
                        if accumulated_text or tool_calls:
                            turns_list = list(tool_calls.values()) if tool_calls else None
                            asyncio.create_task(
                                _append_turn(
                                    PydanticObjectId(project_id),
                                    owner,
                                    "assistant",
                                    accumulated_text,
                                    [
                                        ToolCallTurn(
                                            id=tc["id"],
                                            name=tc["name"],
                                            args=tc["args"],
                                            result=tc["result"],
                                            ok=tc["ok"],
                                        )
                                        for tc in turns_list
                                    ]
                                    if turns_list
                                    else None,
                                )
                            )

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
        profile_id, str(project_id), system_prompt=_system_prompt(project)
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
