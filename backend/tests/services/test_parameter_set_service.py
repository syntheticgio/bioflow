"""Deriving parameter-set eligibility from tool specs (#414).

The point of every test here is that nothing is hand-listed: eligibility comes
from `spec.fields`, so a knob added to a registry becomes saveable with no
second edit.
"""

import pytest

from app.models.parameter_set import ParamSpecFamily
from app.pipelines.aligners import Aligner
from app.pipelines.assemblers import Assembler
from app.services import parameter_set_service as svc


class TestSpecFields:
    def test_resolves_an_aligner(self):
        fields = svc.spec_fields(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value)
        assert fields
        assert all(hasattr(f, "key") for f in fields)

    def test_resolves_an_assembler(self):
        # FLYE and ABYSS declare fields; HIFIASM and SPADES do not (yet).
        fields = svc.spec_fields(ParamSpecFamily.ASSEMBLER, Assembler.FLYE.value)
        assert fields
        assert all(hasattr(f, "key") for f in fields)

    def test_a_spec_with_no_fields_resolves_to_empty(self):
        """`AssemblerSpec.fields` defaults to `()`. HIFIASM and SPADES take
        that default today, so they resolve without raising and yield no
        eligible keys -- which is what `has_parameter_sets` below exists to
        detect, so the UI never offers a picker that can only save nothing."""
        assert svc.spec_fields(ParamSpecFamily.ASSEMBLER, Assembler.SPADES.value) == ()

    def test_unknown_tool_raises(self):
        with pytest.raises(svc.UnknownToolError):
            svc.spec_fields(ParamSpecFamily.ALIGNER, "not-a-real-aligner")


class TestEligibleKeys:
    def test_keys_come_from_the_spec(self):
        expected = {
            f.key for f in svc.spec_fields(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value)
        }
        assert svc.preset_eligible_keys(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value) == expected


class TestEligibleParams:
    def test_drops_input_bindings(self):
        keys = svc.preset_eligible_keys(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value)
        knob = next(iter(keys))
        params = {
            knob: 8,
            "reference_id": "68a1f00000000000000000aa",
            "target_node": "worker-2",
            "project_id": "68a1f00000000000000000bb",
            "label": "sample -> ref",
            "chunked": True,
        }
        got = svc.eligible_params(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value, params)
        assert got == {knob: 8}

    def test_empty_params_is_empty(self):
        assert svc.eligible_params(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value, {}) == {}


class TestHasParameterSets:
    def test_true_when_the_spec_declares_fields(self):
        assert svc.has_parameter_sets(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value)

    def test_false_when_the_spec_declares_none(self):
        assert not svc.has_parameter_sets(ParamSpecFamily.ASSEMBLER, Assembler.SPADES.value)

    def test_false_for_an_unknown_tool(self):
        assert not svc.has_parameter_sets(ParamSpecFamily.ALIGNER, "not-a-real-aligner")


class TestEveryToolIsResolvable:
    """Exhaustiveness, in the shape CLAUDE.md's derivable-registry section
    prescribes: every member of both enums resolves, so a tool added to a
    registry cannot silently have no eligible keys."""

    @pytest.mark.parametrize("aligner", list(Aligner))
    def test_every_aligner_resolves(self, aligner):
        svc.spec_fields(ParamSpecFamily.ALIGNER, aligner.value)

    @pytest.mark.parametrize("assembler", list(Assembler))
    def test_every_assembler_resolves(self, assembler):
        svc.spec_fields(ParamSpecFamily.ASSEMBLER, assembler.value)
