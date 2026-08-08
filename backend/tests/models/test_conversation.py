"""Shape of the ProjectConversation document."""

import pytest
from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.models.base import utcnow
from app.models.conversation import ConversationTurn, ProjectConversation, ToolCallTurn

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


class TestProjectConversation:
    async def test_defaults_to_empty(self):
        convo = ProjectConversation(owner="local", project_id=PydanticObjectId())
        await convo.insert()
        assert convo.turns == []
        assert convo.compacted_summary is None
        assert convo.compacted_through == 0

    async def test_owner_and_project_id_are_jointly_unique(self):
        project_id = PydanticObjectId()
        await ProjectConversation(owner="pair-a", project_id=project_id).insert()
        with pytest.raises(DuplicateKeyError):
            await ProjectConversation(owner="pair-a", project_id=project_id).insert()

    async def test_two_owners_can_each_have_a_conversation_for_the_same_project(self):
        """Per-owner, not globally unique on project_id alone -- a project's
        Q&A history is per-profile like everything else this app partitions."""
        project_id = PydanticObjectId()
        await ProjectConversation(owner="pair-b1", project_id=project_id).insert()
        await ProjectConversation(owner="pair-b2", project_id=project_id).insert()  # must not raise

    async def test_one_owner_can_have_conversations_for_two_different_projects(self):
        await ProjectConversation(owner="pair-c", project_id=PydanticObjectId()).insert()
        await ProjectConversation(owner="pair-c", project_id=PydanticObjectId()).insert()  # must not raise


class TestConversationTurn:
    def test_requires_role_and_content(self):
        turn = ConversationTurn(role="user", content="hi", created_at=utcnow())
        assert turn.role == "user"
        assert turn.content == "hi"

    def test_role_is_restricted_to_user_or_assistant(self):
        """Unlike adapters.ConversationTurn (4 roles, for in-flight tool-call
        scratch state), the persisted document only ever holds the outer
        user/assistant exchange -- tool-call turns are never saved."""
        with pytest.raises(ValueError):
            ConversationTurn(role="tool_call", content="x", created_at=utcnow())

    def test_tool_calls_defaults_to_none(self):
        """A turn with no tool calls (Q&A or user) leaves tool_calls unset,
        so the persisted document does not grow a redundant null field."""
        turn = ConversationTurn(role="user", content="hi", created_at=utcnow())
        assert turn.tool_calls is None

    def test_assistant_turn_can_carry_tool_calls(self):
        """The agent drawer records each bioflow_* tool call it made."""
        turn = ConversationTurn(
            role="assistant",
            content="I'll run QC for you.",
            created_at=utcnow(),
            tool_calls=[
                ToolCallTurn(
                    id="c1",
                    name="bioflow_run_pipeline",
                    args={"kind": "run_qc"},
                    result="{'ok': true}",
                    ok=True,
                )
            ],
        )
        assert turn.tool_calls[0].name == "bioflow_run_pipeline"
        assert turn.tool_calls[0].ok is True

    def test_tool_call_turn_args_default_to_empty_dict(self):
        turn = ConversationTurn(
            role="assistant",
            content="done",
            created_at=utcnow(),
            tool_calls=[ToolCallTurn(id="c1", name="bioflow_get_job")],
        )
        assert turn.tool_calls[0].args == {}
        assert turn.tool_calls[0].result is None
        assert turn.tool_calls[0].ok is None
