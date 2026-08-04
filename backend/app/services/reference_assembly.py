"""Shared validation for reference-based assembly workflows.

These helpers are foundation code for Pilon, RagTag and iVar. They validate
object shape and provenance before a future tool-specific launch queues any
long-running work.
"""

from app.errors import ValidationError
from app.models import DataObject, FormatKind, ObjectRole, ObjectStatus

ASSEMBLY_EXCLUDED_ROLES = {ObjectRole.PROTEIN, ObjectRole.TRANSCRIPT}
ALIGNMENT_KINDS = {FormatKind.BAM, FormatKind.SAM, FormatKind.CRAM}


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


def _is_assembly_like(obj: DataObject) -> bool:
    return (
        obj.format.kind is FormatKind.FASTA
        and obj.role not in ASSEMBLY_EXCLUDED_ROLES
    )


def _is_unassigned_assembly_like(obj: DataObject) -> bool:
    return _is_assembly_like(obj) and obj.role is None


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


def alignment_target_for_bam(
    bam: DataObject, *, object_lookup
) -> DataObject:
    """Return the single assembly/reference this alignment was made against.

    `object_lookup` is injected so tests can use an in-memory mapping and
    future service callers can pass an owner-scoped lookup. The function never
    guesses from filenames.
    """
    if bam.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{bam.name!r} is not ready (status={bam.status.value})",
            details={"object_id": str(bam.id), "status": bam.status.value},
        )
    if bam.format.kind not in ALIGNMENT_KINDS:
        raise ValidationError(
            f"{bam.name!r} is {bam.format.kind.value}, not an alignment",
            details={"object_id": str(bam.id), "kind": bam.format.kind.value},
        )

    explicit_targets = []
    fallback_targets = []
    for parent_id in bam.derived_from or []:
        parent = object_lookup(parent_id)
        if parent is None:
            continue
        if parent.format.kind is not FormatKind.FASTA:
            continue
        if parent.role is ObjectRole.REFERENCE:
            explicit_targets.append(parent)
        elif _is_unassigned_assembly_like(parent):
            fallback_targets.append(parent)

    targets = explicit_targets or fallback_targets

    if not targets:
        raise ValidationError(
            f"{bam.name!r} has no recorded alignment target",
            details={"object_id": str(bam.id)},
        )
    if len(targets) > 1:
        raise ValidationError(
            f"{bam.name!r} has an ambiguous alignment target",
            details={
                "object_id": str(bam.id),
                "targets": [str(target.id) for target in targets],
            },
        )
    return targets[0]


async def resolve_alignment_target_for_bam(
    bam: DataObject, *, owner
) -> DataObject:
    """Resolve an alignment target using the owner-scoped object service."""
    from app.services import object_service

    parents = {}
    for parent_id in bam.derived_from or []:
        parents[parent_id] = await object_service.get_object(parent_id, owner=owner)
    return alignment_target_for_bam(bam, object_lookup=parents.get)


def check_bam_aligned_to(
    bam: DataObject, target: DataObject, *, object_lookup
) -> DataObject:
    """Validate that a BAM/CRAM was aligned to the selected assembly target."""
    resolved = alignment_target_for_bam(bam, object_lookup=object_lookup)
    if resolved.id != target.id:
        raise ValidationError(
            f"{bam.name!r} is aligned to {resolved.name!r}, not {target.name!r}",
            details={
                "bam_id": str(bam.id),
                "target_id": str(target.id),
                "resolved_target_id": str(resolved.id),
            },
        )
    return bam


async def validate_bam_aligned_to(
    bam: DataObject, target: DataObject, *, owner
) -> DataObject:
    """Validate a BAM/CRAM target using the owner-scoped object service."""
    resolved = await resolve_alignment_target_for_bam(bam, owner=owner)
    if resolved.id != target.id:
        raise ValidationError(
            f"{bam.name!r} is aligned to {resolved.name!r}, not {target.name!r}",
            details={
                "bam_id": str(bam.id),
                "target_id": str(target.id),
                "resolved_target_id": str(resolved.id),
            },
        )
    return bam
