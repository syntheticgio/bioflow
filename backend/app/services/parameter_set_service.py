"""What a parameter set may hold, and whether a saved value still applies.

Everything here derives from the tool's own `ParamField` metadata. Nothing is
hand-listed, which is the whole point: a hand-maintained per-tool key dict is
the silent-skip registry shape CLAUDE.md's "Hand-maintained registries keyed by
an enum" section warns about -- a missing entry would mean "no keys eligible",
producing a set that saves and applies nothing with no error anywhere.

The upload sanitizer used for computation records is deliberately NOT called
from this module. That sanitizer is a disclosure boundary deciding what is
safe to upload, not input validation -- freshly typed parameters never pass
through it either. Its narrow allowlist rejects any value containing a path
separator, so applying it here would strip most real parameters and produce
exactly the silent under-application the drift notice exists to prevent. See
the spec's decision 6.
"""

from enum import StrEnum
import math
from typing import Any

from pydantic import BaseModel

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
    return {k: v for k, v in params.items() if k in keys and v is not None}


def has_parameter_sets(family: ParamSpecFamily, tool: str) -> bool:
    """Whether this tool has enough declared parameters to be worth saving.

    `AssemblerSpec.fields` defaults to `()`, and HIFIASM takes that default
    today. Such a tool resolves without raising but has no eligible keys, so
    a set saved against it would store nothing and apply nothing --
    silently. The UI asks this before rendering a picker, so a tool that can
    only save an empty set never offers to.
    """
    try:
        return bool(spec_fields(family, tool))
    except UnknownToolError:
        return False


class RejectionReason(StrEnum):
    """Why a saved value did not reach the form.

    Each member is derived from a `ParamField` attribute rather than a
    hand-written rule, which is what makes the drift check and the validation
    check the same comparison instead of two rules to keep in sync.
    """

    UNKNOWN_FIELD = "unknown_field"    # key not in spec.fields
    WRONG_KIND = "wrong_kind"          # field.kind
    OUT_OF_RANGE = "out_of_range"      # field.min / field.max
    INVALID_CHOICE = "invalid_choice"  # field.choices


class Rejected(BaseModel):
    key: str
    reason: RejectionReason
    detail: str
    value: Any = None


class Resolution(BaseModel):
    applied: dict[str, Any] = {}
    rejected: list[Rejected] = []


def _check(field: ParamField, value: Any) -> Rejected | None:
    """The one comparison. `None` means the value still applies."""
    if field.kind == "bool":
        if not isinstance(value, bool):
            return Rejected(
                key=field.key, reason=RejectionReason.WRONG_KIND, value=value,
                detail=f"{field.label} expects true or false",
            )
        return None

    if field.kind == "int":
        # bool is an int subclass in Python; a checkbox value is not a number.
        if isinstance(value, bool) or not isinstance(value, int):
            return Rejected(
                key=field.key, reason=RejectionReason.WRONG_KIND, value=value,
                detail=f"{field.label} expects a whole number",
            )
        if field.min is not None and value < field.min:
            return Rejected(
                key=field.key, reason=RejectionReason.OUT_OF_RANGE, value=value,
                detail=f"{value} is below the current minimum of {field.min}",
            )
        if field.max is not None and value > field.max:
            return Rejected(
                key=field.key, reason=RejectionReason.OUT_OF_RANGE, value=value,
                detail=f"{value} exceeds the current maximum of {field.max}",
            )
        return None

    if field.kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return Rejected(
                key=field.key, reason=RejectionReason.WRONG_KIND, value=value,
                detail=f"{field.label} expects a number",
            )
        if not math.isfinite(value):
            return Rejected(
                key=field.key, reason=RejectionReason.OUT_OF_RANGE, value=value,
                detail=f"{field.label} must be a finite number",
            )
        if field.min is not None and value < field.min:
            return Rejected(
                key=field.key, reason=RejectionReason.OUT_OF_RANGE, value=value,
                detail=f"{value} is below the current minimum of {field.min}",
            )
        if field.max is not None and value > field.max:
            return Rejected(
                key=field.key, reason=RejectionReason.OUT_OF_RANGE, value=value,
                detail=f"{value} exceeds the current maximum of {field.max}",
            )
        return None

    if not isinstance(value, str):
        return Rejected(
            key=field.key, reason=RejectionReason.WRONG_KIND, value=value,
            detail=f"{field.label} expects text",
        )
    if field.kind == "select":
        allowed = {c.value for c in field.choices}
        if value not in allowed:
            return Rejected(
                key=field.key, reason=RejectionReason.INVALID_CHOICE, value=value,
                detail=f"{value!r} is no longer an option for {field.label}",
            )
    return None


def resolve_params(
    family: ParamSpecFamily, tool: str, params: dict[str, Any]
) -> Resolution:
    """Split a set's saved params into what still applies and what does not.

    Applies what matches and reports the rest rather than refusing outright:
    one removed parameter should not make an otherwise-good set useless. See
    the spec's decision 1.
    """
    by_key = {f.key: f for f in spec_fields(family, tool)}
    applied: dict[str, Any] = {}
    rejected: list[Rejected] = []

    for key, value in params.items():
        field = by_key.get(key)
        if field is None:
            rejected.append(
                Rejected(
                    key=key, reason=RejectionReason.UNKNOWN_FIELD, value=value,
                    detail=f"no longer a parameter of {tool}",
                )
            )
            continue
        problem = _check(field, value)
        if problem is None:
            applied[key] = value
        else:
            rejected.append(problem)

    return Resolution(applied=applied, rejected=rejected)
