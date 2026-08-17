"""What an assembler is, as a name and an output shape.

Separate from `assembler_registry` for the reason `aligners` is separate from
`aligner_registry`: the enum and the output layout are imported by modules
that must not pull in the registry's dependency on `tools` and
`assembly_params`. `assembly_params` imports this; the registry imports both.

Every member is declared, including the two that are not installed. A name the
API can reject with "not installed in this build" is more useful than one it
rejects as unknown, and the registry needs somewhere to hang the reason a
chemistry has no assembler yet.
"""

from dataclasses import dataclass
from enum import StrEnum


class Assembler(StrEnum):
    FLYE = "flye"
    # Declared, not installed. Not packaged for Debian; needs a source build
    # with the arm64 SIMD problem bwa-mem2 already has a script for.
    HIFIASM = "hifiasm"
    # Declared, not installed. NOT packaged for trixie (the 2026-08-01 spec's
    # claim that it is was true for bookworm) -- needs a vendored upstream
    # tarball. See #519.
    SPADES = "spades"
    ABYSS = "abyss"


class OutputKind(StrEnum):
    """What an assembler's output file *is*, independent of its filename.

    Filenames differ per tool (`assembly.fasta` vs `contigs.fasta`), and the
    applier needs to know which one becomes a reference without knowing which
    tool produced it.
    """

    # The contigs. Becomes a DataObject roled REFERENCE.
    CONTIGS = "contigs"
    # The assembly graph. Becomes a DataObject in its own right -- a result
    # someone opens in Bandage, not scaffolding for another tool, which is why
    # it is not a SidecarRole.
    GRAPH = "graph"
    # A table parsed into facts on the contigs object rather than stored as a
    # file of its own. Nobody opens `assembly_info.txt`; they want its columns
    # rendered.
    INFO_TABLE = "info_table"


@dataclass(frozen=True)
class Output:
    """One file an assembler leaves in its output directory."""

    kind: OutputKind
    filename: str
    # Whether the run failed if this is missing. The contigs are the run; the
    # graph and the table are a bonus that a partial success can lack without
    # the assembly being worthless.
    required: bool = False
