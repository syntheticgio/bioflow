"""Aligner index layouts, and the workdir that satisfies them.

Alignment tools assume a filesystem this application deliberately does not
provide. BWA locates its index by appending suffixes to the reference path
(`genome.fna` -> `genome.fna.bwt`, `.amb`, `.ann`, `.pac`, `.0123`); samtools
wants `genome.fna.fai` beside it. Content-addressed storage keeps every blob
alone under its hash, with no extension and no siblings.

`parsers._has_index` has named this since Phase 3: indexes for managed content
are generated and tracked as their own objects, not discovered on disk. This is
the module that puts them back into the shape the tools expect, at the moment a
tool needs them.

The naming is a first-class concern with its own tests rather than a
per-handler afterthought, because of what Phase 6a taught: fastp read a
compressed blob as plain text because it infers gzip from the *filename*, and
the command that caused it was perfectly well-formed. No unit test over command
construction could have caught it. The same class of error here -- a `.bwt`
symlinked under a name bwa-mem2 does not look for -- fails identically: a
correct command, a wrong filename, and an error thousands of reads later.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.logging import get_logger
from app.models import SidecarRole

log = get_logger(__name__)


class Aligner(StrEnum):
    BWA_MEM2 = "bwa-mem2"
    MINIMAP2 = "minimap2"
    BOWTIE2 = "bowtie2"
    HISAT2 = "hisat2"


# bwa-mem2's index is a five-file set, all named by appending to the reference
# filename. `.0123` is bwa-mem2's own (plain bwa has `.bwt`/`.sa` instead), so
# this list is specific to bwa-mem2 and not interchangeable with bwa's.
BWA_MEM2_SUFFIXES: tuple[str, ...] = (".0123", ".amb", ".ann", ".bwt.2bit.64", ".pac")

# minimap2's is a single file, and its name is ours to choose -- it is passed
# explicitly rather than discovered by suffix. Keeping the reference filename as
# the stem means the workdir listing reads the same for both aligners.
MINIMAP2_SUFFIX = ".mmi"

# samtools indexes a FASTA to `<name>.fai` and a coordinate-sorted BAM to
# `<name>.bam.bai`, both by appending, both discovered rather than passed.
FAI_SUFFIX = ".fai"
BAI_SUFFIX = ".bai"

# bowtie2 and HISAT2 both name their index files by appending a numbered
# suffix to a basename, and both are handed that basename via -x rather than
# a path to the reference. The counts differ: bowtie2 writes six files,
# HISAT2 eight.
#
# These lists are the tool's contract, and `build_index` fails loudly when a
# builder exits 0 without producing one of them -- see the verification step
# in Task 5, which builds a real index and compares.
BOWTIE2_SUFFIXES: tuple[str, ...] = (
    ".1.bt2", ".2.bt2", ".3.bt2", ".4.bt2", ".rev.1.bt2", ".rev.2.bt2",
)
HISAT2_SUFFIXES: tuple[str, ...] = (
    ".1.ht2", ".2.ht2", ".3.ht2", ".4.ht2",
    ".5.ht2", ".6.ht2", ".7.ht2", ".8.ht2",
)


INDEX_ROLE: dict[Aligner, SidecarRole] = {
    Aligner.BWA_MEM2: SidecarRole.BWA_MEM2_INDEX,
    Aligner.MINIMAP2: SidecarRole.MINIMAP2_INDEX,
    Aligner.BOWTIE2: SidecarRole.BOWTIE2_INDEX,
    Aligner.HISAT2: SidecarRole.HISAT2_INDEX,
}


def index_suffixes(aligner: Aligner) -> tuple[str, ...]:
    """Every suffix an aligner's index is made of, relative to the reference."""
    if aligner is Aligner.BWA_MEM2:
        return BWA_MEM2_SUFFIXES
    if aligner is Aligner.BOWTIE2:
        return BOWTIE2_SUFFIXES
    if aligner is Aligner.HISAT2:
        return HISAT2_SUFFIXES
    return (MINIMAP2_SUFFIX,)


def index_filenames(reference_name: str, aligner: Aligner) -> tuple[str, ...]:
    """The filenames an aligner's index occupies beside `reference_name`.

    The reference's *own* name is the base, not the blob digest: these names are
    what the tool will look for, and it derives them from the path it was given.
    """
    return tuple(f"{reference_name}{suffix}" for suffix in index_suffixes(aligner))


def sidecar_name_for(reference_name: str, produced: str) -> str:
    """The stored name for a produced index file.

    Indexes are built in a scratch directory against a symlink named for the
    reference, so a produced file already carries the right name. Recording it
    verbatim is what lets `materialize` put it back under the same name later,
    and keeps the pairing legible in the database.
    """
    return Path(produced).name


@dataclass(frozen=True)
class MaterializedRef:
    """A reference laid out on disk the way a tool expects to find it."""

    directory: Path
    reference: Path  # the path to hand the tool
    linked: tuple[str, ...]  # filenames created, for logging and tests

    @property
    def missing_index(self) -> bool:
        return not self.linked


def plan_links(
    *,
    reference_name: str,
    sidecars: dict[str, str],
) -> dict[str, str]:
    """Map workdir filename -> blob path, for a reference and its sidecars.

    Pure: the mapping is the decision worth testing, separately from whether
    the symlinks were created. `sidecars` is {stored sidecar name: blob path}.

    A sidecar whose name does not belong to this reference is dropped rather
    than linked under a corrected name. Silently renaming it would hide a
    genuine bookkeeping error -- an index attached to the wrong reference -- in
    exactly the way that produces a plausible-looking wrong result.
    """
    links: dict[str, str] = {}
    for name, blob in sidecars.items():
        safe = Path(name).name
        if not safe.startswith(reference_name):
            log.warning(
                "sidecar_name_mismatch",
                reference=reference_name,
                sidecar=safe,
            )
            continue
        links[safe] = blob
    return links


def materialize(
    *,
    workdir: Path,
    reference_name: str,
    reference_blob: Path,
    sidecars: dict[str, str],
) -> MaterializedRef:
    """Build a directory where a reference and its sidecars appear as siblings.

    ```
    tmp/align/<job_id>/ref/genome.fna      -> the reference blob
                          genome.fna.bwt.2bit.64 -> a sidecar blob
                          genome.fna.fai   -> ...
    ```

    Symlinks rather than copies: an index for a mammalian genome is several
    gigabytes, and every tool here follows links happily.
    """
    workdir.mkdir(parents=True, exist_ok=True)

    safe_reference = Path(reference_name).name
    reference_link = workdir / safe_reference
    _link(reference_link, reference_blob)

    linked: list[str] = []
    for name, blob in sorted(plan_links(reference_name=safe_reference, sidecars=sidecars).items()):
        _link(workdir / name, Path(blob))
        linked.append(name)

    log.info(
        "reference_materialized",
        directory=str(workdir),
        reference=safe_reference,
        sidecars=len(linked),
    )
    return MaterializedRef(
        directory=workdir, reference=reference_link, linked=tuple(linked)
    )


def _link(link: Path, target: Path) -> None:
    """Point `link` at `target`, replacing whatever was there.

    Replaced rather than skipped-if-present: a retry reuses the job's scratch
    directory, and a stale link from a previous attempt pointing at a
    half-written blob is the kind of input that produces a wrong answer instead
    of an error.
    """
    link.unlink(missing_ok=True)
    link.symlink_to(target)
