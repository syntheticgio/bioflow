from app.models.ai import TaskSlot


def test_de_summary_slot_has_a_label():
    assert TaskSlot.DE_SUMMARY.label == "Differential expression summaries"


def test_variant_summary_slot_has_a_label():
    assert TaskSlot.VARIANT_SUMMARY.label == "Variant call summaries"
