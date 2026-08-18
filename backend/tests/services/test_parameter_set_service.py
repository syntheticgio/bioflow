"""Deriving parameter-set eligibility from tool specs (#414).

The point of every test here is that nothing is hand-listed: eligibility comes
from `spec.fields`, so a knob added to a registry becomes saveable with no
second edit.
"""

import math

import pytest

from app.models.parameter_set import ParamSpecFamily
from app.pipelines.aligner_registry import Choice, ParamField
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
        """`AssemblerSpec.fields` defaults to `()`. HIFIASM takes that
        default today, so it resolves without raising and yields no
        eligible keys -- which is what `has_parameter_sets` below exists to
        detect, so the UI never offers a picker that can only save nothing."""
        assert svc.spec_fields(ParamSpecFamily.ASSEMBLER, Assembler.HIFIASM.value) == ()

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

    def test_drops_only_unset_none_values_after_filtering_to_spec_keys(self):
        params = {
            "preset": "map-ont",
            "kmer_size": None,
            "window_size": 10,
            "emit_md": None,
            "reference_id": "68a1f00000000000000000aa",
        }
        got = svc.eligible_params(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value, params)
        assert got == {"preset": "map-ont", "window_size": 10}

    def test_empty_params_is_empty(self):
        assert svc.eligible_params(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value, {}) == {}


class TestHasParameterSets:
    def test_true_when_the_spec_declares_fields(self):
        assert svc.has_parameter_sets(ParamSpecFamily.ALIGNER, Aligner.MINIMAP2.value)

    def test_false_when_the_spec_declares_none(self):
        assert not svc.has_parameter_sets(ParamSpecFamily.ASSEMBLER, Assembler.HIFIASM.value)

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


def _int_field(**kw):
    base = dict(key="threads", label="Threads", kind="int", default=4, help="")
    return ParamField(**{**base, **kw})


class TestResolveRejectionReasons:
    """One test per reason in the spec's four-reason table. Each mutates a
    field the way a tool's spec would drift between save and apply."""

    def test_unknown_field(self, monkeypatch):
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (_int_field(),))
        got = svc.resolve_params(
            ParamSpecFamily.ALIGNER, "minimap2", {"threads": 8, "gone": 1}
        )
        assert got.applied == {"threads": 8}
        assert [r.reason for r in got.rejected] == [svc.RejectionReason.UNKNOWN_FIELD]
        assert got.rejected[0].key == "gone"

    def test_wrong_kind(self, monkeypatch):
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (_int_field(),))
        got = svc.resolve_params(ParamSpecFamily.ALIGNER, "minimap2", {"threads": "eight"})
        assert got.applied == {}
        assert got.rejected[0].reason is svc.RejectionReason.WRONG_KIND

    def test_out_of_range(self, monkeypatch):
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (_int_field(min=1, max=12),))
        got = svc.resolve_params(ParamSpecFamily.ALIGNER, "minimap2", {"threads": 16})
        assert got.applied == {}
        assert got.rejected[0].reason is svc.RejectionReason.OUT_OF_RANGE
        assert "12" in got.rejected[0].detail

    def test_invalid_choice(self, monkeypatch):
        field = ParamField(
            key="preset", label="Preset", kind="select", default="sr", help="",
            choices=(Choice(value="sr", label="Short read"),),
        )
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (field,))
        got = svc.resolve_params(ParamSpecFamily.ALIGNER, "minimap2", {"preset": "map-ont"})
        assert got.applied == {}
        assert got.rejected[0].reason is svc.RejectionReason.INVALID_CHOICE

    def test_bool_accepts_only_bool(self, monkeypatch):
        field = ParamField(key="paired", label="Paired", kind="bool", default=True, help="")
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (field,))
        resolved = svc.resolve_params(ParamSpecFamily.ALIGNER, "minimap2", {"paired": False})
        assert resolved.applied == {"paired": False}
        assert svc.resolve_params(ParamSpecFamily.ALIGNER, "minimap2", {"paired": 1}).rejected

    def test_keeps_explicit_false_zero_and_empty_string_values(self, monkeypatch):
        fields = (
            ParamField(key="paired", label="Paired", kind="bool", default=True, help=""),
            ParamField(key="batch_size", label="Batch size", kind="int", default=500, help=""),
            ParamField(key="extra_args", label="Extra args", kind="text", default="--eqx", help=""),
        )
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: fields)
        resolved = svc.resolve_params(
            ParamSpecFamily.ALIGNER,
            "minimap2",
            {"paired": False, "batch_size": 0, "extra_args": ""},
        )
        assert resolved.applied == {"paired": False, "batch_size": 0, "extra_args": ""}
        assert resolved.rejected == []

    def test_applies_float_values_that_match_the_current_spec(self, monkeypatch):
        field = ParamField(
            key="secondary_ratio",
            label="Secondary ratio",
            kind="float",
            default=0.8,
            help="",
            min=0.0,
            max=1.0,
        )
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (field,))
        resolved = svc.resolve_params(
            ParamSpecFamily.ALIGNER, "minimap2", {"secondary_ratio": 0.35}
        )
        assert resolved.applied == {"secondary_ratio": 0.35}
        assert resolved.rejected == []

    def test_rejects_invalid_saved_float_values(self, monkeypatch):
        field = ParamField(
            key="secondary_ratio",
            label="Secondary ratio",
            kind="float",
            default=0.8,
            help="",
            min=0.0,
            max=1.0,
        )
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (field,))
        wrong_kind = svc.resolve_params(
            ParamSpecFamily.ALIGNER, "minimap2", {"secondary_ratio": "0.35"}
        )
        out_of_range = svc.resolve_params(
            ParamSpecFamily.ALIGNER, "minimap2", {"secondary_ratio": 1.2}
        )
        assert wrong_kind.applied == {}
        assert wrong_kind.rejected[0].reason is svc.RejectionReason.WRONG_KIND
        assert out_of_range.applied == {}
        assert out_of_range.rejected[0].reason is svc.RejectionReason.OUT_OF_RANGE

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_rejects_non_finite_saved_float_values(self, monkeypatch, value):
        field = ParamField(
            key="secondary_ratio",
            label="Secondary ratio",
            kind="float",
            default=0.8,
            help="",
            min=0.0,
            max=1.0,
        )
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (field,))
        resolved = svc.resolve_params(
            ParamSpecFamily.ALIGNER, "minimap2", {"secondary_ratio": value}
        )
        assert resolved.applied == {}
        assert resolved.rejected[0].reason is svc.RejectionReason.OUT_OF_RANGE


class TestResolveDoesNotSanitize:
    """Regression guard for the spec's decision 6.

    The issue's question 5 asks for `params_sanitizer.sanitize()` on apply.
    That module is a disclosure boundary for uploaded records, not input
    validation; applying it here would strip every value containing a path
    separator and every key outside its fourteen-key allowlist. If someone
    "fixes" this back, this test fails."""

    def test_keeps_values_sanitize_would_strip(self, monkeypatch):
        field = ParamField(key="extra_args", label="Extra", kind="text", default="", help="")
        monkeypatch.setattr(svc, "spec_fields", lambda f, t: (field,))
        got = svc.resolve_params(
            ParamSpecFamily.ALIGNER, "minimap2", {"extra_args": "--junc-bed=/data/ref.bed"}
        )
        assert got.applied == {"extra_args": "--junc-bed=/data/ref.bed"}
        assert got.rejected == []

    def test_sanitize_is_not_imported_by_this_module(self):
        import inspect

        assert "params_sanitizer" not in inspect.getsource(svc)
