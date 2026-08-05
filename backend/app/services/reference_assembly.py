"""Shared validation for reference-based assembly workflows.

These helpers are foundation code for Polypolish, RagTag and iVar. They
validate object shape and provenance before a tool-specific launch queues any
long-running work.

(Written for Pilon rather than Polypolish; #23 swapped the tool in 2026-08-05
because Pilon's best-alignment input mis-corrects repeats. The foundation
needed no change for the swap, but its own comments named the wrong tool.)
"""

from app.errors import NotFoundError, ValidationError
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

    parents = await _alignment_parent_lookup(
        bam, owner=owner, get_object=object_service.get_object
    )
    return alignment_target_for_bam(bam, object_lookup=parents.get)


async def _alignment_parent_lookup(
    bam: DataObject, *, owner, get_object
) -> dict:
    parents = {}
    for parent_id in bam.derived_from or []:
        try:
            parents[parent_id] = await get_object(parent_id, owner=owner)
        except NotFoundError:
            parents[parent_id] = None
    return parents


def check_primer_bed(obj: DataObject, reference: DataObject) -> DataObject:
    """Validate a primer scheme BED against the reference it will trim.

    Two checks, not one. Column shape catches files that are not BED at all
    -- a samtools .fai index sniffs as BED (name plus two integers) and its
    first column *is* a reference's contig names verbatim, so contig overlap
    alone would pass it (see #48). Real BED carries at least 3 columns
    (chrom, chromStart, chromEnd); iVar's own primer scheme format wants 6.
    Requiring at least 3 here still rejects a bare 2-column .fai without
    assuming callers always supply a full 6-column iVar-format BED.

    Contig overlap catches the case shape alone cannot: a well-formed BED for
    the wrong organism. iVar's own behaviour when primer contigs don't match
    the reference is to trim nothing and exit 0, producing an untrimmed
    consensus that looks like a successful trimmed one -- so this is rejected
    outright rather than warned about.
    """
    if obj.status is not ObjectStatus.READY:
        raise ValidationError(
            f"{obj.name!r} is not ready (status={obj.status.value})",
            details={"object_id": str(obj.id), "status": obj.status.value},
        )
    if obj.format.kind is not FormatKind.BED:
        raise ValidationError(
            f"{obj.name!r} is {obj.format.kind.value}, not a BED primer scheme",
            details={"object_id": str(obj.id), "kind": obj.format.kind.value},
        )

    column_counts = (obj.facts or {}).get("column_counts") or []
    if column_counts and max(column_counts) < 3:
        raise ValidationError(
            f"{obj.name!r} is not a valid BED primer scheme "
            f"({max(column_counts)} columns; BED needs at least 3)",
            details={"object_id": str(obj.id), "column_counts": column_counts},
        )

    bed_contigs = set((obj.facts or {}).get("reference_names") or [])
    ref_contigs = set((reference.facts or {}).get("reference_names") or [])
    if bed_contigs and ref_contigs and bed_contigs.isdisjoint(ref_contigs):
        raise ValidationError(
            f"{obj.name!r} has no contigs in common with {reference.name!r}",
            details={
                "object_id": str(obj.id),
                "reference_id": str(reference.id),
                "bed_contigs": sorted(bed_contigs)[:10],
                "reference_contigs": sorted(ref_contigs)[:10],
            },
        )
    return obj


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


# --- Short reads for polishing ---------------------------------------------
#
# Polishing is the first workflow here whose second input is *reads* rather
# than another assembly or a BAM, and the reads must be short reads: running
# a short-read polisher over ONT or PacBio data is not merely unusual, it is
# meaningless. So unlike the assembly validators above, which check shape,
# these have to reason about chemistry.

LONG_READ_PLATFORMS = frozenset({"OXFORD_NANOPORE", "PACBIO_SMRT"})
SHORT_READ_PLATFORMS = frozenset(
    {"ILLUMINA", "BGISEQ", "DNBSEQ", "ELEMENT", "ULTIMA", "SINGULAR", "ION_TORRENT"}
)


def is_short_read(obj: DataObject) -> bool:
    """Whether a FASTQ is short-read data.

    **Platform first, chemistry only as a tie-break** -- and that order is
    the whole point of this function, not an implementation detail.

    The obvious rule is `qc_read_chemistry == "short"`, since QC already
    infers chemistry and the enum has a SHORT member. Checked against the
    real database on 2026-08-05, that rule is wrong: `ERR16145610.fastq` is
    a MinION run whose `qc_platform` is OXFORD_NANOPORE and whose
    `qc_read_chemistry` is `short`. The chemistry inference reads read
    *lengths*, so a nanopore run that happens to carry short reads infers
    short -- true about the reads, false about the data. Trusting it would
    let a polish job run ONT reads through a short-read polisher, which does
    not error and quietly degrades the assembly it was meant to improve.
    No fixture would have caught this; the file did.

    So a known long-read platform is disqualifying regardless of what
    chemistry says, and chemistry only gets a vote when the platform is
    unknown.

    A file with *no* platform recorded counts as short, because
    `_qc_platform` defaults to ILLUMINA -- this module does not second-guess
    that default. It is the same call `sam_platform` documents ("the
    overwhelmingly common case here"), and reversing it locally would mean
    an uploaded Illumina FASTQ, which typically carries no metadata at all,
    never gets a polish card. The residual risk is an uploaded long-read
    file with no metadata; the launch path validates the same way, so the
    user who names it explicitly gets the same answer, and this is a wrong
    *offer* rather than a wrong run.
    """
    if obj.format.kind is not FormatKind.FASTQ:
        return False

    # Lazily imported: pipeline_service imports this module, so a top-level
    # import here would be circular. `_qc_platform` is the one place that
    # knows how to turn "PromethION" or "Illumina NovaSeq X Plus" into a
    # platform name, and reimplementing that table here is how the two
    # copies drift.
    from app.services.pipeline_service import _qc_platform

    platform = _qc_platform(obj)
    if platform in LONG_READ_PLATFORMS:
        return False
    if platform in SHORT_READ_PLATFORMS:
        return True

    return (obj.facts or {}).get("qc_read_chemistry") == "short"


def group_read_sets(reads: list[DataObject]) -> list[list[DataObject]]:
    """Group FASTQ objects into read sets: mate-linked pairs, or singletons.

    Returns each set with R1 first, so a caller can pass them to an aligner
    positionally without re-deriving which is which. Ordering within a pair
    comes from `read_number` when the mate detection (#17) recorded one, and
    falls back to object id for a stable -- if arbitrary -- order when it did
    not, since an unordered pair would make the run non-reproducible for no
    reason.
    """
    by_id = {obj.id: obj for obj in reads}
    seen: set = set()
    sets: list[list[DataObject]] = []

    for obj in reads:
        if obj.id in seen:
            continue
        mate_id = getattr(obj, "mate_object_id", None)
        mate = by_id.get(mate_id) if mate_id else None
        if mate is None or mate.id in seen:
            seen.add(obj.id)
            sets.append([obj])
            continue
        seen.add(obj.id)
        seen.add(mate.id)
        pair = sorted(
            [obj, mate],
            key=lambda o: (
                (o.facts or {}).get("read_number") or 0,
                str(o.id),
            ),
        )
        sets.append(pair)
    return sets


def short_read_sets(objects: list[DataObject]) -> list[list[DataObject]]:
    """The ready short-read sets among a project's objects.

    The unit a polish run consumes is a *set* -- one FASTQ, or a mate-linked
    pair -- not an individual file, which is why this returns groups rather
    than a flat list. A project with one paired Illumina sample has one set
    here, not two candidates, and that distinction is what lets the polish
    card offer an unambiguous launch instead of guessing.
    """
    ready = [
        obj
        for obj in objects
        if obj.status is ObjectStatus.READY and is_short_read(obj)
    ]
    return group_read_sets(ready)
