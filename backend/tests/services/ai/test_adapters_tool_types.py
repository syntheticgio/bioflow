"""ToolCall/ToolSpec: the shared vocabulary both adapters target for
tool-calling. No wire logic here -- just the types."""

import pytest
from app.services.ai.adapters import ToolCall, ToolSpec


class TestToolCall:
    def test_carries_id_name_and_parsed_arguments(self):
        call = ToolCall(id="call_1", name="search_objects", arguments={"kinds": ["fastq"]})
        assert call.id == "call_1"
        assert call.name == "search_objects"
        assert call.arguments == {"kinds": ["fastq"]}

    def test_is_frozen(self):
        call = ToolCall(id="call_1", name="x", arguments={})
        with pytest.raises(AttributeError):
            call.name = "other"


class TestToolSpec:
    def test_carries_a_json_schema_dict(self):
        spec = ToolSpec(
            name="search_objects",
            description="Search files in this project.",
            parameters={"type": "object", "properties": {"kinds": {"type": "array"}}},
        )
        assert spec.name == "search_objects"
        assert spec.parameters["type"] == "object"

    def test_is_frozen(self):
        spec = ToolSpec(name="x", description="d", parameters={})
        with pytest.raises(AttributeError):
            spec.description = "other"
