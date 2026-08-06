"""Project Q&A: get/clear a project's conversation, and ask a question.

Asking enqueues `answer_project_question` rather than answering
synchronously -- an answer must survive the chat drawer being minimized or
closed, the same reasoning that makes every other AI-using feature here a
queued job rather than an inline await.
"""

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel

from app.api.deps import OwnerDep
from app.models import JobClass
from app.models.conversation import ProjectConversation
from app.queue import queue

router = APIRouter(prefix="/projects/{project_id}/qa", tags=["project-qa"])


class ConversationTurnOut(BaseModel):
    role: str
    content: str


class ConversationOut(BaseModel):
    turns: list[ConversationTurnOut]
    compacted_summary: str | None = None


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    job_id: str


async def _get_or_create(project_id: PydanticObjectId, *, owner: str) -> ProjectConversation:
    convo = await ProjectConversation.find_one(
        ProjectConversation.owner == owner, ProjectConversation.project_id == project_id
    )
    if convo is None:
        convo = ProjectConversation(owner=owner, project_id=project_id)
        await convo.insert()
    return convo


@router.get("/conversation", response_model=ConversationOut)
async def get_conversation(project_id: PydanticObjectId, owner: OwnerDep) -> ConversationOut:
    convo = await _get_or_create(project_id, owner=owner)
    return ConversationOut(
        turns=[ConversationTurnOut(role=t.role, content=t.content) for t in convo.turns],
        compacted_summary=convo.compacted_summary,
    )


@router.post("/ask", response_model=AskResponse)
async def ask(project_id: PydanticObjectId, body: AskRequest, owner: OwnerDep) -> AskResponse:
    convo = await _get_or_create(project_id, owner=owner)
    # No dedup_key: two identical questions asked deliberately in a row
    # ("actually, ask that again") should both run rather than the second
    # silently vanishing.
    job = await queue.enqueue(
        "answer_project_question",
        owner=owner,
        project_id=project_id,
        job_class=JobClass.USER_INTERACTIVE,
        payload={
            "project_id": str(project_id),
            "question": body.question,
            "conversation_id": str(convo.id),
        },
    )
    return AskResponse(job_id=str(job.id))


@router.delete("/conversation", status_code=status.HTTP_204_NO_CONTENT)
async def clear_conversation(project_id: PydanticObjectId, owner: OwnerDep) -> None:
    convo = await ProjectConversation.find_one(
        ProjectConversation.owner == owner, ProjectConversation.project_id == project_id
    )
    if convo is not None:
        convo.turns = []
        convo.compacted_summary = None
        convo.compacted_through = 0
        await convo.save()
