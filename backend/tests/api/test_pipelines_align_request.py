"""The AlignRequest transport contract for additional read sets.

The align schema endpoint tests live in test_pipelines_align_schema.py; this
module covers the request model itself, which is where additional read sets
enter the API. The full validation of what those ids resolve to is the
service layer's job (test_align_launch.py); here the contract is only that
the request carries an ordered list of sets with optional mates.
"""

import pytest
from app.api.v1.pipelines import AlignRequest
from beanie import PydanticObjectId
from pydantic import ValidationError

PRIMARY = PydanticObjectId()
REFERENCE = PydanticObjectId()
EXTRA_R1 = PydanticObjectId()
EXTRA_R2 = PydanticObjectId()
EXTRA_R1B = PydanticObjectId()


def _request(**overrides):
    body = {
        "object_id": PRIMARY,
        "reference_id": REFERENCE,
        "additional_read_sets": [
            {"object_id": EXTRA_R1, "mate_object_id": EXTRA_R2},
            {"object_id": EXTRA_R1B},
        ],
    }
    body.update(overrides)
    return AlignRequest.model_validate(body)


class TestAdditionalReadSets:
    def test_accepts_ordered_additional_read_sets_with_optional_mates(self):
        request = _request()
        sets = request.additional_read_sets
        assert [entry.object_id for entry in sets] == [EXTRA_R1, EXTRA_R1B]
        assert sets[0].mate_object_id == EXTRA_R2
        assert sets[1].mate_object_id is None

    def test_defaults_additional_read_sets_to_empty(self):
        request = AlignRequest.model_validate(
            {"object_id": PRIMARY, "reference_id": REFERENCE}
        )
        assert request.additional_read_sets == []

    def test_a_set_requires_an_object_id(self):
        with pytest.raises(ValidationError):
            AlignRequest.model_validate(
                {
                    "object_id": PRIMARY,
                    "reference_id": REFERENCE,
                    "additional_read_sets": [{"mate_object_id": EXTRA_R2}],
                }
            )

    def test_mate_object_id_is_optional(self):
        request = AlignRequest.model_validate(
            {
                "object_id": PRIMARY,
                "reference_id": REFERENCE,
                "additional_read_sets": [{"object_id": EXTRA_R1}],
            }
        )
        assert request.additional_read_sets[0].mate_object_id is None

    def test_mate_object_id_must_be_a_valid_object_id(self):
        with pytest.raises(ValidationError):
            AlignRequest.model_validate(
                {
                    "object_id": PRIMARY,
                    "reference_id": REFERENCE,
                    "additional_read_sets": [
                        {"object_id": EXTRA_R1, "mate_object_id": "not-an-id"}
                    ],
                }
            )
