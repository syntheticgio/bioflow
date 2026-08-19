"""Feedback endpoints: submit and list.

Not owner-scoped, like `schedules.py`: feedback is addressed to whoever runs
the installation, not partitioned per profile, and the list on the Help page
is meant to show everyone's submissions so a user can see theirs went through.

Write-only otherwise -- there is no email or ticketing behind this. Reading
the collection with a shell is the escape hatch until there is a reason to
build more. New submissions are also pushed to a Discord webhook (if
configured) via `app.services.feedback_service` -- best-effort, never
affecting the 201 response.
"""

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.models.feedback import (
    COMMENT_MAX_LENGTH,
    CONTACT_MAX_LENGTH,
    SUBJECT_MAX_LENGTH,
    Feedback,
)
from app.services.feedback_service import notify_feedback_created

router = APIRouter(prefix="/feedback", tags=["feedback"])

# Strong references to in-flight notification tasks: the event loop holds only
# a weak reference to scheduled tasks, so without this a task can be GC'd
# before it runs (RUF006).
_notify_tasks: set[asyncio.Task] = set()


class FeedbackCreate(BaseModel):
    contact: str = Field(min_length=1, max_length=CONTACT_MAX_LENGTH)
    subject: str = Field(min_length=1, max_length=SUBJECT_MAX_LENGTH)
    comment: str = Field(min_length=1, max_length=COMMENT_MAX_LENGTH)


class FeedbackOut(BaseModel):
    id: str
    contact: str
    subject: str
    comment: str
    created_at: str

    @classmethod
    def of(cls, f: Feedback) -> "FeedbackOut":
        return cls(
            id=str(f.id),
            contact=f.contact,
            subject=f.subject,
            comment=f.comment,
            created_at=f.created_at.isoformat(),
        )


@router.post("", status_code=201)
async def submit_feedback(body: FeedbackCreate) -> FeedbackOut:
    feedback = Feedback(contact=body.contact, subject=body.subject, comment=body.comment)
    await feedback.insert()
    # Best-effort notification: fire-and-forget so a slow or failing webhook
    # never stalls the 201 response. The insert above has already committed,
    # so the feedback is never lost; notify_feedback_created catches every
    # error internally and only loses the notification on failure.
    task = asyncio.create_task(
        notify_feedback_created(
            feedback_id=str(feedback.id),
            contact=feedback.contact,
            subject=feedback.subject,
            comment=feedback.comment,
        )
    )
    _notify_tasks.add(task)
    task.add_done_callback(_notify_tasks.discard)
    return FeedbackOut.of(feedback)


@router.get("")
async def list_feedback() -> list[FeedbackOut]:
    items = await Feedback.find_all().sort("-created_at").to_list()
    return [FeedbackOut.of(f) for f in items]
