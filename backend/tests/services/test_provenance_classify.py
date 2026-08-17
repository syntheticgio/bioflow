"""Which ancestors are the specimen lineage and which are materials.

A VCF's parents include the BAM and the reference. Both are real ancestry,
but a methods section renders them differently: the BAM is a step in the
story, the reference is a material the story used. Getting this wrong puts
the reference's NCBI accession at the same level as trim parameters.
"""

from app.models.object import ObjectRole
from app.services.provenance_walker import classify_parent
from beanie import PydanticObjectId

BAM = PydanticObjectId()
REF = PydanticObjectId()
GFF = PydanticObjectId()


def test_reference_named_by_facts_is_supporting():
    facts = {"reference_object_id": str(REF)}
    assert classify_parent(REF, facts=facts, role=None) == "supporting"


def test_annotation_named_by_facts_is_supporting():
    facts = {"annotation_object_id": str(GFF)}
    assert classify_parent(GFF, facts=facts, role=None) == "supporting"


def test_parent_not_named_as_material_is_spine():
    facts = {"reference_object_id": str(REF)}
    assert classify_parent(BAM, facts=facts, role=None) == "spine"


def test_falls_back_to_role_when_facts_are_silent():
    """Objects made before their step had a provenance builder have no
    `*_object_id` keys at all, so role is the only signal left."""
    assert classify_parent(REF, facts={}, role=ObjectRole.REFERENCE) == "supporting"
    assert classify_parent(BAM, facts={}, role=ObjectRole.ALIGNMENT) == "spine"


def test_unknown_role_with_silent_facts_is_spine():
    """Defaulting to spine keeps a step visible. Defaulting to supporting
    would quietly demote a real processing step into a materials list."""
    assert classify_parent(BAM, facts={}, role=None) == "spine"
