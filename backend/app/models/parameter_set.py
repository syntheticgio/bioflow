"""Saved pipeline parameter sets: named tuning knobs a user can re-apply.

Named `ParameterSet` rather than `Preset` because "preset" already means two
other things in this subsystem: `align_runner.Preset` (minimap2's `-x` values)
and `AlignerSpec.presets` (built-in read-only tuning profiles). Both are
tool-authored and immutable; this one is user-authored and per-profile. The UI
still says "preset" -- users never see the type name.

See docs/superpowers/specs/2026-08-18-parameter-sets-design.md.
"""

from enum import StrEnum

from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument


class ParamSpecFamily(StrEnum):
    """Which registry resolves a set's `tool` to its `ParamField` list.

    Stored rather than inferred: `aligner_registry.spec_for` and
    `assembler_registry.spec_for` are separate lookups whose schema envelopes
    differ, and guessing which one owns a tool string at apply time is a
    lookup that fails silently when a name is ambiguous.

    Grows by one member per tool family that gains a `ParamSpec`.
    """

    ALIGNER = "aligner"
    ASSEMBLER = "assembler"


class ParameterSet(TimestampedDocument):
    """Tuning knobs a user saved under a name, scoped to one specific tool.

    Bound to the tool rather than the run kind: minimap2 and STAR parameters
    are not interchangeable, so a kind-scoped set would be almost entirely
    drift by construction.

    `params` holds tuning knobs only -- never input bindings. That exclusion is
    structural rather than a denylist: `parameter_set_service` derives the
    eligible keys from the tool's `spec.fields`, and `reference_id`, input
    object ids, `target_node`, and `project_id` are excluded because they were
    never `ParamField`s.
    """

    name: str
    tool: str
    family: ParamSpecFamily
    params: dict = {}
    # Bumped when `params` changes, never on rename. Provenance asks "were
    # these runs configured the same?" -- a rename does not change that
    # answer, an edit does.
    revision: int = 1

    class Settings:
        name = "parameter_sets"
        indexes = [
            IndexModel(
                [("owner", ASCENDING), ("tool", ASCENDING)],
                name="owner_tool",
            ),
            # Enforced at the database rather than in a service check that
            # races two concurrent saves of the same name.
            IndexModel(
                [("owner", ASCENDING), ("tool", ASCENDING), ("name", ASCENDING)],
                name="owner_tool_name_unique",
                unique=True,
            ),
        ]
