"""One spec per completeness tool.

Every availability case here patches `spec_for` rather than `tools.compleasm`,
mirroring `assembler_registry`'s own tests and for the same reason:
`CompletenessToolSpec` is a frozen dataclass that captured the probe as a
function object at import time, so patching `tools.compleasm` on the module
never reaches `spec.probe`. A test that patches the wrong seam would silently
read the host machine's real compleasm install instead of the fixture.
"""

import dataclasses
from unittest.mock import patch

from app.pipelines import assembly_qc_registry
from app.pipelines.assembly_qc_registry import (
    COMPLEASM_SPEC,
    CompletenessTool,
    default_tool,
    spec_for,
)


class _Available:
    available = True


class _Absent:
    available = False


class TestSpecFor:
    def test_compleasm_spec_has_the_expected_odb(self):
        assert spec_for(CompletenessTool.COMPLEASM).odb == "odb12"

    def test_busco_spec_is_declared_unavailable_by_construction(self):
        """BUSCO is not built by the Dockerfile at all -- probe=None means
        available() can answer False without probing anything, the same
        reasoning HIFIASM_SPEC follows in assembler_registry."""
        spec = spec_for(CompletenessTool.BUSCO)
        assert spec.probe is None
        assert spec.available() is False
        assert "compleasm" in spec.unavailable_reason.lower()


class TestAvailability:
    """Exercises spec.available() directly on a constructed spec rather than
    going through a patched `spec_for` -- patching `assembly_qc_registry.
    spec_for` does not reach a `spec_for` name already imported into this
    test module, which is the same "patch the wrong reference" trap the
    class docstring warns about, one level removed. Callers elsewhere in the
    app (suggestion_service, pipeline_service) call `assembly_qc_registry.
    spec_for(...)` through the module, so that seam works for them; a test
    module that imported the bare name needs a different one.
    """

    def test_available_flips_true_when_the_probe_reports_installed(self):
        installed = dataclasses.replace(COMPLEASM_SPEC, probe=lambda: _Available())
        assert installed.available() is True

    def test_it_flips_to_unavailable_when_the_probe_reports_absent(self):
        """The direction that fails when the patch seam breaks. Asserting
        *availability* would pass whether or not the patch worked, since the
        image ships compleasm installed once the Dockerfile change lands."""
        absent = dataclasses.replace(COMPLEASM_SPEC, probe=lambda: _Absent())
        assert absent.available() is False

    def test_patching_tools_compleasm_directly_does_not_reach_the_spec(self):
        """Documents the trap itself: COMPLEASM_SPEC.probe is the
        tools.compleasm function object, bound once when the module loaded.
        Replacing the *module attribute* `app.pipelines.tools.compleasm`
        afterwards does not change what `COMPLEASM_SPEC.probe` points at --
        so `spec.probe()` still calls the original, real probe rather than
        this patch. A test asserting availability here would read whatever
        the host machine's real compleasm reports, not the fixture below.
        """
        with patch("app.pipelines.tools.compleasm", return_value=_Absent()):
            # The module-level spec is untouched by the patch above: this
            # calls the real probe, which is why every other test in this
            # class goes through `spec_for` / `dataclasses.replace` instead.
            assert COMPLEASM_SPEC.probe is not assembly_qc_registry.tools.compleasm


class TestDefaultTool:
    def test_default_tool_is_compleasm(self):
        assert default_tool() is COMPLEASM_SPEC
        assert default_tool().tool_enum is CompletenessTool.COMPLEASM
