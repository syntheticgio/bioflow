"""ProjectUpdate's validation rules.

The 4000-character cap on agent_system_prompt is not cosmetic: the value
becomes an argv element when pi is spawned (agent_service.start), so an
unbounded string risks ARG_MAX and a spawn failure that surfaces only as
"agent unavailable", with nothing pointing at the prompt as the cause.
"""

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api.v1.schemas import ProjectUpdate


class TestAgentSystemPromptLimit:
    def test_accepts_4000_characters(self):
        body = ProjectUpdate(agent_system_prompt="x" * 4000)
        assert body.agent_system_prompt is not None
        assert len(body.agent_system_prompt) == 4000

    def test_rejects_4001_characters(self):
        with pytest.raises(PydanticValidationError):
            ProjectUpdate(agent_system_prompt="x" * 4001)

    def test_omitting_the_field_is_none(self):
        assert ProjectUpdate().agent_system_prompt is None
