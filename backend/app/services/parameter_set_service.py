"""What a parameter set may hold, and whether a saved value still applies.

Everything here derives from the tool's own `ParamField` metadata. Nothing is
hand-listed, which is the whole point: a hand-maintained per-tool key dict is
the silent-skip registry shape CLAUDE.md's "Hand-maintained registries keyed by
an enum" section warns about -- a missing entry would mean "no keys eligible",
producing a set that saves and applies nothing with no error anywhere.

`params_sanitizer.sanitize()` is deliberately NOT called from this module. It
is a disclosure boundary deciding what is safe to upload in computation
records, not input validation -- freshly typed parameters never pass through
it either. Its fourteen-key allowlist rejects any value containing a path
separator, so applying it here would strip most real parameters and produce
exactly the silent under-application the drift notice exists to prevent. See
the spec's decision 6.
"""

from typing import Any

from app.models.parameter_set import ParamSpecFamily
from app.pipelines import aligner_registry, assembler_registry
from app.pipelines.aligner_registry import ParamField
from app.pipelines.aligners import Aligner
from app.pipelines.assemblers import Assembler


class UnknownToolError(ValueError):
    """A set names a tool its family's registry does not know."""


def spec_fields(family: ParamSpecFamily, tool: str) -> tuple[ParamField, ...]:
    """The tool's declared parameter fields, whichever registry owns it."""
    try:
        if family is ParamSpecFamily.ALIGNER:
            return tuple(aligner_registry.spec_for(Aligner(tool)).fields)
        return tuple(assembler_registry.spec_for(Assembler(tool)).fields)
    except (ValueError, KeyError) as exc:
        raise UnknownToolError(f"{family.value} {tool!r} is not registered") from exc


def preset_eligible_keys(family: ParamSpecFamily, tool: str) -> frozenset[str]:
    """Which param keys a set for this tool may hold.

    Derived, so a field added to a spec becomes saveable without a second
    edit here -- and `reference_id`, input object ids, `target_node`,
    `project_id`, `label`, and `chunked` are excluded structurally, because
    they were never `ParamField`s.
    """
    return frozenset(f.key for f in spec_fields(family, tool))


def eligible_params(
    family: ParamSpecFamily, tool: str, params: dict[str, Any]
) -> dict[str, Any]:
    """`params` narrowed to what a set for this tool may store."""
    keys = preset_eligible_keys(family, tool)
    return {k: v for k, v in params.items() if k in keys}


def has_parameter_sets(family: ParamSpecFamily, tool: str) -> bool:
    """Whether this tool has enough declared parameters to be worth saving.

    `AssemblerSpec.fields` defaults to `()`, and HIFIASM and SPADES take that
    default today. Such a tool resolves without raising but has no eligible
    keys, so a set saved against it would store nothing and apply nothing --
    silently. The UI asks this before rendering a picker, so a tool that can
    only save an empty set never offers to.
    """
    try:
        return bool(spec_fields(family, tool))
    except UnknownToolError:
        return False
