"""The narrative feature routes through its own slot.

The settings page renders one row per TaskSlot member, so the enum is what
makes a feature independently routable.
"""

from app.models.ai import TaskSlot


def test_provenance_narrative_slot_exists():
    assert TaskSlot.PROVENANCE_NARRATIVE.value == "provenance_narrative"


def test_every_slot_has_a_label():
    for slot in TaskSlot:
        assert slot.label
