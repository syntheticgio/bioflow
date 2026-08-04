"""Shared validation for reference-based assembly workflows.

These helpers are foundation code for Pilon, RagTag and iVar. They validate
object shape and provenance before a future tool-specific launch queues any
long-running work.
"""

from app.errors import ValidationError
from app.models import DataObject, FormatKind, ObjectRole, ObjectStatus

ASSEMBLY_EXCLUDED_ROLES = {ObjectRole.PROTEIN, ObjectRole.TRANSCRIPT}


def _role_name(obj: DataObject) -> str:
    return obj.role.value if obj.role else "unassigned"


def _check_ready_fasta(obj: DataObject, *, purpose: str) -> None:
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready for {purpose} (status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind is not FormatKind.FASTA:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not a FASTA assembly",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )
    if obj.role in ASSEMBLY_EXCLUDED_ROLES:
        raise ValidationError(
            f"{obj.name!r} is a {_role_name(obj)} FASTA, not a genome assembly",
            details={"object_id": str(obj.id), "role": obj.role.value},
        )


def check_draft_assembly(obj: DataObject) -> DataObject:
    """Validate an assembly that a tool will polish or scaffold.

    Uploaded assemblies may have no role, so this checks shape rather than
    provenance. Protein and transcript FASTA are explicitly excluded because
    their bytes are FASTA while their biological meaning is not an assembly.
    """
    _check_ready_fasta(obj, purpose="reference-based assembly")
    return obj


def check_reference_assembly(obj: DataObject) -> DataObject:
    """Validate a trusted reference assembly input.

    Unlike draft assemblies, references must carry ObjectRole.REFERENCE so a
    generic uploaded FASTA is not silently treated as the authoritative target
    for scaffolding or consensus.
    """
    _check_ready_fasta(obj, purpose="reference-based assembly")
    if obj.role is not ObjectRole.REFERENCE:
        raise ValidationError(
            f"{obj.name!r} is not marked as a reference assembly",
            details={"object_id": str(obj.id), "role": _role_name(obj)},
        )
    return obj
