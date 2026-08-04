"""Keys must not reach the log or a stored error body.

Some providers echo part of the key back in a 401, and those bodies are now
persisted on the provider document -- so this is not hypothetical.
"""

from app.services.ai import redaction


class TestScrub:
    def test_removes_the_key(self):
        out = redaction.scrub("Bearer sk-ant-secret123 rejected", "sk-ant-secret123")
        assert "sk-ant-secret123" not in out
        assert "[redacted]" in out

    def test_leaves_other_text_intact(self):
        out = redaction.scrub("invalid x-api-key header", "sk-ant-secret123")
        assert out == "invalid x-api-key header"

    def test_handles_no_key(self):
        assert redaction.scrub("some error", None) == "some error"

    def test_ignores_an_empty_key(self):
        """An empty needle would otherwise match everywhere and blank the text."""
        assert redaction.scrub("some error", "") == "some error"

    def test_removes_every_occurrence(self):
        out = redaction.scrub("sk-abc123456789 then sk-abc123456789", "sk-abc123456789")
        assert "sk-abc" not in out

    def test_truncates_long_bodies(self):
        """Upstream error bodies can be an HTML error page; storing it whole
        buys nothing and makes the provider document unreadable."""
        assert len(redaction.scrub("x" * 5000, None)) <= 500
