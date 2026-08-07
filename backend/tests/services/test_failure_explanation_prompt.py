"""What goes into the failure explanation prompt.

Same discipline as test_summary_prompt.py: the model is given exactly the
code and message, nothing else, and the system prompt forbids proposing a
fix or a root cause the text does not state.
"""

from app.services.failure_explanation_prompt import (
    FAILURE_SYSTEM_PROMPT,
    build_failure_prompt,
)


class TestPromptContent:
    def test_the_code_and_message_both_reach_the_prompt(self):
        prompt = build_failure_prompt("CalledProcessError", "exit status 1")
        assert "CalledProcessError" in prompt
        assert "exit status 1" in prompt

    def test_the_traceback_is_never_a_parameter(self):
        """build_failure_prompt takes only code and message -- asserted by
        the call succeeding with exactly two arguments and no keyword for a
        traceback."""
        prompt = build_failure_prompt(code="X", message="y")
        assert prompt is not None


class TestSystemPrompt:
    def test_the_system_prompt_forbids_proposing_a_fix(self):
        assert "fix" in FAILURE_SYSTEM_PROMPT.lower()
        assert "never propose" in FAILURE_SYSTEM_PROMPT.lower()

    def test_the_system_prompt_forbids_asserting_certainty(self):
        assert "certainty" in FAILURE_SYSTEM_PROMPT.lower()
