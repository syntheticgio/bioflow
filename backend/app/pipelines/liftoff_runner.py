"""Liftoff annotation-transfer command construction.

Liftoff transfers an existing gene annotation (GFF3 or GTF) from a reference
genome onto a target assembly by aligning the annotated features with
minimap2 and projecting them. This module builds the command line only --
execution is the queue handler's job, mirroring ``bakta_runner``.

Liftoff usage:

    liftoff <target> <reference> -g <reference_annotation> -o <output> \
        [-u <unmapped>] [-t <threads>] [-copies]

The target and reference are positional; ``-g`` names the annotation to
transfer. ``-copies`` allows transferring a feature more than once when the
target has duplicated the reference region, and ``-u`` writes the features
Liftoff could not place.
"""

from pathlib import Path


def build_liftoff_command(
    *,
    liftoff_path: str,
    target: Path,
    reference: Path,
    reference_annotation: Path,
    out_gff: Path,
    threads: int = 4,
    copies: bool = True,
    unmapped_gff: Path | None = None,
) -> list[str]:
    """Assemble the Liftoff invocation for one transfer run.

    Args:
        liftoff_path: path to the liftoff binary.
        target: the assembly to transfer the annotation onto.
        reference: the genome the annotation was built against.
        reference_annotation: GFF3/GTF to transfer (``-g``).
        out_gff: where Liftoff writes the lifted annotation (``-o``).
        threads: parallel threads (``-t``).
        copies: allow a reference feature to map to multiple target loci.
        unmapped_gff: where Liftoff writes unplaced features (``-u``).
    """
    cmd = [
        str(liftoff_path),
        str(target),
        str(reference),
        "-g",
        str(reference_annotation),
        "-o",
        str(out_gff),
        "-t",
        str(threads),
    ]
    if copies:
        cmd.append("-copies")
    if unmapped_gff is not None:
        cmd += ["-u", str(unmapped_gff)]
    return cmd
