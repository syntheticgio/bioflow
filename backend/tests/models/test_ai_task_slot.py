from app.models.ai import TaskSlot


def test_failure_explanation_slot_has_a_label():
    assert TaskSlot.FAILURE_EXPLANATION.label == "Job failure explanations"
