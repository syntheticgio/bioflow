"""One spec per assembly-completeness tool: the same shape `assembler_registry`
uses, for the same reason. There is exactly one usable tool today, but the
seam is the point -- CRAQ, Merqury and a declared-but-unavailable BUSCO all
become specs here rather than edits spread across a runner, a handler and a
card, which is what happened before `assembler_registry` existed for
assemblers.

Unlike `assembler_registry`, dispatch is not keyed on chemistry: any
assembly-shaped FASTA is eligible, and the parameter that varies per run is
the lineage, chosen from organism metadata or by the user rather than fixed
per tool. So there is no `spec_for_chemistry` here -- `spec_for` is the whole
accessor, same as picking an assembler once chemistry has already chosen one.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from app.pipelines import tools


class CompletenessTool(StrEnum):
    COMPLEASM = "compleasm"
    # Declared, not installed. The name a reviewer asks for by default, and
    # having somewhere to point when that happens is worth the few lines --
    # see HIFIASM_SPEC in assembler_registry for the same reasoning. compleasm
    # is chosen over it as the default: ~10-20x faster, and it recovers some
    # BUSCOs that BUSCO's own metaeuk step misses (Huang & Li 2023).
    BUSCO = "busco"


@dataclass(frozen=True)
class CompletenessToolSpec:
    tool_enum: CompletenessTool
    # None for a declared-but-not-installed tool. `available()` is what
    # callers should ask; this being None is why it can answer False without
    # probing anything.
    probe: Callable[[], tools.Tool] | None
    # The OrthoDB version this tool's lineage names carry, so a stored fact
    # can be compared honestly against a different run: compleasm defaults to
    # odb12, and Debian's BUSCO is odb10. Two completeness percentages from
    # different OrthoDB versions are not the same measurement.
    odb: str
    # Why this tool is not usable, when it is not. Rendered by the card, so it
    # says what the user could do about it rather than naming a module.
    unavailable_reason: str = ""

    def available(self) -> bool:
        return self.probe is not None and self.probe().available


COMPLEASM_SPEC = CompletenessToolSpec(
    tool_enum=CompletenessTool.COMPLEASM,
    probe=tools.compleasm,
    odb="odb12",
)

# Not installed: not packaged for Debian trixie, and Debian's own busco
# package cannot score eukaryotic lineages as installed -- its dependencies
# bring prodigal, not metaeuk, which a green install hides until a eukaryotic
# run fails. Declared anyway so the API can say "not installed in this build"
# rather than "unknown tool", the same reasoning HIFIASM_SPEC follows.
BUSCO_SPEC = CompletenessToolSpec(
    tool_enum=CompletenessTool.BUSCO,
    probe=None,
    odb="odb10",
    unavailable_reason="BUSCO is not installed in this build. Use compleasm.",
)


SPECS: dict[CompletenessTool, CompletenessToolSpec] = {
    CompletenessTool.COMPLEASM: COMPLEASM_SPEC,
    CompletenessTool.BUSCO: BUSCO_SPEC,
}


def spec_for(tool_enum: CompletenessTool) -> CompletenessToolSpec:
    """The spec for a completeness tool.

    Patch *this* to simulate a tool being absent. Patching `tools.compleasm`
    does not work: `COMPLEASM_SPEC` is a frozen dataclass that captured the
    function object at import time, so the module attribute is no longer what
    `spec.probe` refers to -- the same seam `assembler_registry.spec_for`
    documents, recorded here before someone writes the test that silently
    reads the host machine instead of the patch.
    """
    return SPECS[tool_enum]


def default_tool() -> CompletenessToolSpec:
    """The tool a launch should use when the caller has no preference.

    compleasm for every request today. This function is the single place
    that changes if a faster or more accurate default tool arrives -- the
    same role `assembler_registry.spec_for_chemistry` plays for assemblers.
    """
    return COMPLEASM_SPEC
