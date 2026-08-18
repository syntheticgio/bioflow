"""The ParameterSet document (#414)."""

import pytest

from app.models.parameter_set import ParameterSet, ParamSpecFamily

pytestmark = pytest.mark.usefixtures("beanie_models")


class TestParameterSetModel:
    def test_defaults_to_revision_one(self):
        s = ParameterSet(
            name="Nanopore fast",
            tool="minimap2",
            family=ParamSpecFamily.ALIGNER,
            params={"threads": 8},
        )
        assert s.revision == 1

    def test_family_is_a_string_enum(self):
        assert ParamSpecFamily.ALIGNER == "aligner"
        assert ParamSpecFamily.ASSEMBLER == "assembler"

    def test_collection_name(self):
        assert ParameterSet.Settings.name == "parameter_sets"

    def test_declares_unique_name_per_owner_and_tool(self):
        names = {i.document["name"] for i in ParameterSet.Settings.indexes}
        assert "owner_tool_name_unique" in names

    def test_is_registered_for_beanie(self):
        from app.models import ALL_MODELS

        assert ParameterSet in ALL_MODELS
