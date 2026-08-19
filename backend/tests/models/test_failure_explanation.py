"""Failure-explanation cache key and TaskSlot metadata.

The cache key for a job failure explanation. Unlike normalize_organism's
human-readable key, this hashes error messages: they are unbounded in length
and character content (embedded paths, quotes, newlines), which makes them
unsuitable as a literal indexed string.
"""

from app.models import normalize_failure
from app.models.ai import TaskSlot


class TestNormalization:
    def test_the_same_code_and_message_produce_the_same_key(self):
        a = normalize_failure("CalledProcessError", "exit status 1")
        b = normalize_failure("CalledProcessError", "exit status 1")
        assert a == b

    def test_a_different_message_with_the_same_code_produces_a_different_key(self):
        a = normalize_failure("CalledProcessError", "exit status 1")
        b = normalize_failure("CalledProcessError", "exit status 2")
        assert a != b

    def test_a_different_code_with_the_same_message_produces_a_different_key(self):
        """The same message text can mean different things depending on which
        code raised it, so code alone must distinguish the key."""
        a = normalize_failure("CalledProcessError", "no such file or directory")
        b = normalize_failure("PermanentError", "no such file or directory")
        assert a != b

    def test_the_key_is_a_fixed_length_hash(self):
        key = normalize_failure("X", "y" * 5000)
        assert len(key) == 32


def test_failure_explanation_slot_has_a_label():
    assert TaskSlot.FAILURE_EXPLANATION.label == "Job failure explanations"

def test_de_summary_slot_has_a_label():
    assert TaskSlot.DE_SUMMARY.label == "Differential expression summaries"

def test_variant_summary_slot_has_a_label():
    assert TaskSlot.VARIANT_SUMMARY.label == "Variant call summaries"
