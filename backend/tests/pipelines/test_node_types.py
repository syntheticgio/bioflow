"""The canvas node registry.

This file's most important test is the exhaustiveness one. A launch_* function
absent from both NODE_TYPES and EXCLUDED_LAUNCHES is a tool that installs
cleanly, passes every other test, and simply never appears on the canvas --
the STAR/_SIDECAR_ROLES failure in a new place.
"""

import inspect

from app.models import FormatKind, ObjectRole
from app.pipelines.node_types import (
    EXCLUDED_LAUNCHES,
    NODE_TYPES,
    launch_function_names,
)


class TestExhaustiveness:
    def test_every_launch_function_is_classified(self):
        """Every launch_* either has a node type or is explicitly excluded.

        If this fails after you added a launcher: add a NODE_TYPES entry, or
        add it to EXCLUDED_LAUNCHES *with a comment saying why*. Do not delete
        the assertion.
        """
        classified = {spec.launch_name for spec in NODE_TYPES.values()} | EXCLUDED_LAUNCHES
        assert launch_function_names() == classified

    def test_exclusions_are_real_functions(self):
        """A typo'd exclusion silently stops guarding anything."""
        assert EXCLUDED_LAUNCHES <= launch_function_names()

    def test_no_launcher_is_both_used_and_excluded(self):
        used = {spec.launch_name for spec in NODE_TYPES.values()}
        assert not (used & EXCLUDED_LAUNCHES)


class TestSpecs:
    def test_every_spec_declares_a_callable_launch(self):
        for key, spec in NODE_TYPES.items():
            assert callable(spec.launch), f"{key} has no callable launch adapter"

    def test_every_spec_has_a_label(self):
        """The palette renders these; a blank one is an unusable node."""
        for key, spec in NODE_TYPES.items():
            assert spec.label.strip(), f"{key} has no label"

    def test_port_names_are_unique_within_a_spec(self):
        """Output->port resolution is by declared name, so duplicates make it
        ambiguous."""
        for key, spec in NODE_TYPES.items():
            names = [p.name for p in spec.inputs]
            assert len(names) == len(set(names)), f"{key} has duplicate input ports"
            out_names = [p.name for p in spec.outputs]
            assert len(out_names) == len(set(out_names)), f"{key} has duplicate outputs"

    def test_align_declares_a_reference_port_that_rejects_protein(self):
        """The concrete rule the typing exists for."""
        spec = NODE_TYPES["align"]
        reference = next(p for p in spec.inputs if p.name == "reference")
        assert reference.type.accepts(FormatKind.FASTA, ObjectRole.REFERENCE)
        assert not reference.type.accepts(FormatKind.FASTA, ObjectRole.PROTEIN)

    def test_trim_consumes_fastq_and_produces_trimmed_reads(self):
        spec = NODE_TYPES["trim"]
        reads = next(p for p in spec.inputs if p.name == "reads")
        assert reads.type.accepts(FormatKind.FASTQ, None)
        out = spec.outputs[0]
        assert out.type.role is ObjectRole.TRIMMED_READS


class TestAdapterSignatures:
    def test_every_adapter_takes_inputs_and_params(self):
        """The registry's whole purpose is presenting 24 differently-shaped
        launchers behind one call shape."""
        for key, spec in NODE_TYPES.items():
            sig = inspect.signature(spec.launch)
            assert {"inputs", "params", "owner"} <= set(sig.parameters), (
                f"{key}'s adapter does not take (inputs, params, owner)"
            )
