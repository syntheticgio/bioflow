"""The registry is the contract between the backend and the dialog.

The tests that matter are the completeness ones: every aligner must have a
spec, and every spec's fields must match the parameter class it names. A
field the form renders but the params class rejects is a dialog the user
cannot submit, and it would not be caught by any per-tool test.
"""

import pytest

from app.pipelines import align_params, aligner_registry
from app.pipelines.aligners import Aligner


class TestCompleteness:
    def test_every_aligner_has_a_spec(self):
        for aligner in Aligner:
            assert aligner_registry.spec_for(aligner) is not None

    def test_every_spec_names_its_own_aligner(self):
        for aligner in Aligner:
            assert aligner_registry.spec_for(aligner).aligner is aligner

    def test_every_spec_has_a_memory_model(self):
        for aligner in Aligner:
            model = aligner_registry.spec_for(aligner).memory_model
            assert model.fixed_overhead_mb > 0
            assert model.index_bytes_per_ref_base > 0


class TestFieldMetadataMatchesParams:
    def test_every_field_key_is_accepted_by_the_params_class(self):
        """A field the form renders that the params class does not accept is
        a form the user cannot submit."""
        for aligner in Aligner:
            spec = aligner_registry.spec_for(aligner)
            payload = {"aligner": aligner.value}
            for f in spec.fields:
                payload[f.key] = f.default
            params = align_params.from_dict(payload)
            for f in spec.fields:
                assert hasattr(params, f.key), (
                    f"{aligner.value} field {f.key!r} has no params attribute"
                )

    def test_select_fields_declare_choices(self):
        for aligner in Aligner:
            for f in aligner_registry.spec_for(aligner).fields:
                if f.kind == "select":
                    assert f.choices, f"{f.key} is a select with no choices"

    def test_select_defaults_are_among_their_choices(self):
        for aligner in Aligner:
            for f in aligner_registry.spec_for(aligner).fields:
                if f.kind == "select":
                    values = [c.value for c in f.choices]
                    assert f.default in values

    def test_every_field_has_help_text(self):
        """The help line is the only explanation a generated form carries,
        so an empty one is a knob with no stated meaning."""
        for aligner in Aligner:
            for f in aligner_registry.spec_for(aligner).fields:
                assert f.help.strip(), f"{aligner.value}.{f.key} has no help"


class TestSerialization:
    def test_schema_is_json_serializable(self):
        """It is served straight to the dialog, so anything not JSON-native
        breaks the endpoint rather than the test that built it."""
        import json

        schema = aligner_registry.schema_for(Aligner.BOWTIE2)
        json.dumps(schema)

    def test_schema_carries_the_field_groups(self):
        schema = aligner_registry.schema_for(Aligner.BOWTIE2)
        groups = {f["group"] for f in schema["fields"]}
        assert "performance" in groups
        assert "biology" in groups
