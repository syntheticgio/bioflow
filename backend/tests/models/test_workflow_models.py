"""The persisted workflow graph.

Port types reuse FormatKind/ObjectRole rather than a parallel vocabulary,
because the rule they enforce already exists: ObjectRole.PROTEIN is commented
in models/object.py as the thing that keeps a protein FASTA out of the
aligner's reference picker. A canvas refusing that wire is that same rule.
"""

from app.models.workflow import PortType, WorkflowNodeKind
from app.models import FormatKind, ObjectRole


class TestPortType:
    def test_same_format_and_role_accepts(self):
        port = PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE)
        assert port.accepts(FormatKind.FASTA, ObjectRole.REFERENCE)

    def test_a_protein_fasta_is_not_a_reference(self):
        """The failure this typing exists to prevent."""
        port = PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE)
        assert not port.accepts(FormatKind.FASTA, ObjectRole.PROTEIN)

    def test_wrong_format_never_accepts(self):
        port = PortType(format=FormatKind.BAM, role=None)
        assert not port.accepts(FormatKind.FASTQ, None)

    def test_a_null_role_accepts_any_role(self):
        """A port that cares only about format -- QC reads any FASTQ,
        trimmed or raw."""
        port = PortType(format=FormatKind.FASTQ, role=None)
        assert port.accepts(FormatKind.FASTQ, ObjectRole.TRIMMED_READS)
        assert port.accepts(FormatKind.FASTQ, None)

    def test_a_typed_port_rejects_an_untyped_object(self):
        """An object with no role cannot satisfy a port that requires one:
        the role is what carries the intent, and guessing is what
        ObjectRole exists to avoid."""
        port = PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE)
        assert not port.accepts(FormatKind.FASTA, None)


class TestWorkflowNodeKind:
    def test_both_kinds_exist(self):
        assert {k.value for k in WorkflowNodeKind} == {"input", "action"}
