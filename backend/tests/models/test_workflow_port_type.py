"""PortType's format matching, including the multi-format case.

The single-format path is the one ~30 existing PortSpec declarations use, so
it is tested here explicitly rather than assumed: a change to `formats` that
broke `format` would break every port on the canvas.
"""

from app.models.object import FormatKind, ObjectRole
from app.models.workflow import PortType


class TestSingleFormat:
    def test_accepts_its_own_format(self):
        port = PortType(format=FormatKind.BAM)
        assert port.accepts(FormatKind.BAM, None)

    def test_rejects_another_format(self):
        port = PortType(format=FormatKind.BAM)
        assert not port.accepts(FormatKind.VCF, None)

    def test_required_role_is_not_satisfied_by_an_absent_one(self):
        """The rule that stops a protein FASTA reaching a reference port."""
        port = PortType(format=FormatKind.FASTA, role=ObjectRole.REFERENCE)
        assert not port.accepts(FormatKind.FASTA, None)
        assert not port.accepts(FormatKind.FASTA, ObjectRole.PROTEIN)
        assert port.accepts(FormatKind.FASTA, ObjectRole.REFERENCE)


class TestMultiFormat:
    def test_accepts_every_declared_format(self):
        port = PortType(formats=(FormatKind.GFF, FormatKind.GTF, FormatKind.BED))
        assert port.accepts(FormatKind.GFF, None)
        assert port.accepts(FormatKind.GTF, None)
        assert port.accepts(FormatKind.BED, None)

    def test_rejects_a_format_not_declared(self):
        port = PortType(formats=(FormatKind.GFF, FormatKind.GTF, FormatKind.BED))
        assert not port.accepts(FormatKind.GENBANK, None)

    def test_role_rule_still_applies_across_formats(self):
        port = PortType(
            formats=(FormatKind.GFF, FormatKind.GTF),
            role=ObjectRole.ANNOTATION,
        )
        assert port.accepts(FormatKind.GFF, ObjectRole.ANNOTATION)
        assert not port.accepts(FormatKind.GFF, None)

    def test_single_format_is_readable_as_a_set(self):
        """`accepted_formats` is what serialization and the frontend read, so
        it must be populated for single-format ports too."""
        port = PortType(format=FormatKind.BAM)
        assert port.accepted_formats == (FormatKind.BAM,)
