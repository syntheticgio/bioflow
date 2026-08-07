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
    STAR = "star"
    WINNOWMAP = "winnowmap"


# bwa-mem2's index is a five-file set, all named by appending to the reference
# filename. `.0123` is bwa-mem2's own (plain bwa has `.bwt`/`.sa` instead), so
# this list is specific to bwa-mem2 and not interchangeable with bwa's.
BWA_MEM2_SUFFIXES: tuple[str, ...] = (".0123", ".amb", ".ann", ".bwt.2bit.64", ".pac")

# minimap2's is a single file, and its name is ours to choose -- it is passed
# explicitly rather than discovered by suffix. Keeping the reference filename as
# the stem means the workdir listing reads the same for both aligners.
MINIMAP2_SUFFIX = ".mmi"

# winnowmap's "index" is the repetitive-k-mer list meryl produces (`meryl
# count` then `meryl print greater-than`), passed via `-W` rather than
# discovered by suffix -- same shape as minimap2's `.mmi`, a single file
# whose name is ours to choose. It is not a minimizer index the way the
# other four aligners' are; winnowmap re-derives its own index from the
# reference at alignment time and only consumes this file as an extra input.
WINNOWMAP_REPETITIVE_KMER_SUFFIX = ".repetitive_k15.txt"

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

# STAR is the directory shape: `--genomeDir` names a directory, and the files
# inside it carry fixed names of STAR's choosing that have nothing to do with
# the reference's. The directory itself is named after the reference, which is
# what keeps two genomes' indexes from colliding in one workdir.
STAR_DIR_SUFFIX = ".STARindex"

# A separate directory (and role) for an annotation-built index, rather than
# reusing STAR_DIR_SUFFIX with a different member count: the two carry
# different information and a project can want both, e.g. one alignment run
# before a GTF was uploaded and one after. Sharing the directory name would
# mean the second build overwrites sidecars the first is still using, or
# `missing_index_for` seeing a mismatched file count and reporting the index
# broken rather than absent.
STAR_ANNOTATED_DIR_SUFFIX = ".STARindex.annotated"

# What `STAR --runMode genomeGenerate` writes into that directory, verified by
# running STAR 2.7.11b rather than recalled: a run *without* an annotation
# produces exactly these eight files. Recollection said it also wrote
# exonInfo.tab, geneInfo.tab and transcriptInfo.tab -- it does not, and
# requiring them would have failed every index build. STAR writes those only
# when given --sjdbGTFfile.
#
# `Log.out` is also written into the directory and is deliberately absent from
# this list: it is a build transcript, not something STAR reads back at
# alignment time, so carrying it as a sidecar would store a log file forever
# under the guise of an index member.
STAR_MEMBERS: tuple[str, ...] = (
    "Genome",
    "SA",
    "SAindex",
    "chrLength.txt",
    "chrName.txt",
    "chrNameLength.txt",
    "chrStart.txt",
    "genomeParameters.txt",
)

# Extra files an annotation-built index writes, again measured rather than
# predicted: the TODO entry that asked for this guessed exonInfo.tab,
# geneInfo.tab, transcriptInfo.tab and sjdbList.out.tab from STAR's docs, and
# a real `--sjdbGTFfile` run against the yeast reference
# (GCF_000146045.2_R64_genomic.gtf) wrote three more the guess missed --
# exonGeTrInfo.tab, sjdbInfo.txt and sjdbList.fromGTF.out.tab -- seven in
# total, not four. Trusting the guess would have left `build_index` reporting
# success while silently dropping three sidecars, the exact failure mode
# `_SIDECAR_ROLES` produced for the base STAR index the first time.
STAR_ANNOTATED_MEMBERS: tuple[str, ...] = STAR_MEMBERS + (
    "exonGeTrInfo.tab",
    "exonInfo.tab",
    "geneInfo.tab",
    "sjdbInfo.txt",
    "sjdbList.fromGTF.out.tab",
    "sjdbList.out.tab",
    "transcriptInfo.tab",
)


INDEX_ROLE: dict[Aligner, SidecarRole] = {
    Aligner.BWA_MEM2: SidecarRole.BWA_MEM2_INDEX,
    Aligner.MINIMAP2: SidecarRole.MINIMAP2_INDEX,
    Aligner.BOWTIE2: SidecarRole.BOWTIE2_INDEX,
    Aligner.HISAT2: SidecarRole.HISAT2_INDEX,
    Aligner.STAR: SidecarRole.STAR_INDEX,
    Aligner.WINNOWMAP: SidecarRole.WINNOWMAP_INDEX,
}


def index_role(aligner: Aligner, *, annotated: bool = False) -> SidecarRole:
    """Which sidecar role an index build's files are stored under.

    A thin wrapper around `INDEX_ROLE` rather than a second dict, since
    `annotated` only ever changes the answer for STAR -- every other aligner
    has no annotation concept and the flag is meaningless for it.
    """
    if annotated:
        if aligner is not Aligner.STAR:
            raise ValueError(f"{aligner.value} has no annotated index")
        return SidecarRole.STAR_ANNOTATED_INDEX
    return INDEX_ROLE[aligner]


# STAR's members become suffixes too -- `.STARindex.SA`, `.STARindex.Genome`.
# Flattening the directory into the same suffix-per-file shape the other three
# aligners use is what lets the whole sidecar model (naming, `owns_sidecar`,
# the database records, `plan_links`' path sanitization) stay exactly as it
# was. Only `materialize` needs to know a directory is wanted, and only at the
# moment it hands the files to a tool.
STAR_SUFFIXES: tuple[str, ...] = tuple(
    f"{STAR_DIR_SUFFIX}.{member}" for member in STAR_MEMBERS
)

# The annotated index's fifteen files, flattened the same way, under the
# separate directory name so they cannot collide with an unannotated build's
# sidecars of the same aligner.
STAR_ANNOTATED_SUFFIXES: tuple[str, ...] = tuple(
    f"{STAR_ANNOTATED_DIR_SUFFIX}.{member}" for member in STAR_ANNOTATED_MEMBERS
)


def index_suffixes(aligner: Aligner, *, annotated: bool = False) -> tuple[str, ...]:
    """Every suffix an aligner's index is made of, relative to the reference."""
    if annotated:
        if aligner is not Aligner.STAR:
            raise ValueError(f"{aligner.value} has no annotated index")
        return STAR_ANNOTATED_SUFFIXES
    if aligner is Aligner.BWA_MEM2:
        return BWA_MEM2_SUFFIXES
    if aligner is Aligner.BOWTIE2:
        return BOWTIE2_SUFFIXES
    if aligner is Aligner.HISAT2:
        return HISAT2_SUFFIXES
    if aligner is Aligner.STAR:
        return STAR_SUFFIXES
    if aligner is Aligner.WINNOWMAP:
        return (WINNOWMAP_REPETITIVE_KMER_SUFFIX,)
    return (MINIMAP2_SUFFIX,)


def index_filenames(
    reference_name: str, aligner: Aligner, *, annotated: bool = False
) -> tuple[str, ...]:
    """The filenames an aligner's index occupies beside `reference_name`.

    The reference's *own* name is the base, not the blob digest: these names are
    what the tool will look for, and it derives them from the path it was given.
    """
    return tuple(
        f"{reference_name}{suffix}"
        for suffix in index_suffixes(aligner, annotated=annotated)
    )


@dataclass(frozen=True)
class IndexLayout:
    """How one aligner's index is shaped on disk, and how it is named.

    Three shapes exist in the wild and this application will eventually need
    all three:

    - suffix: files named by appending to the reference path, discovered by
      the tool (bwa-mem2, minimap2)
    - prefix: files named by appending to a basename that is passed to the
      tool explicitly via -x (bowtie2, HISAT2)
    - directory: a fixed set of names inside a directory passed via a flag
      (STAR's `--genomeDir`)

    The distinction that matters for correctness is `owns_sidecar`. Dropping a
    sidecar that does not belong to a reference is what stops an index built
    for one genome from being silently materialized beside another; the
    resulting run would produce a plausible-looking wrong answer rather than
    an error.

    The directory shape is stored flat and only becomes a directory here.
    STAR's members (`SA`, `Genome`, `chrName.txt`) share no prefix with the
    reference and would collide between two genomes in one workdir, so they
    are *recorded* as `genome.fna.STARindex.SA` -- which `owns_sidecar` and
    every database record handle like any other suffix -- and `workdir_path`
    translates each back to `genome.fna.STARindex/SA` at materialize time.
    Storing them under their bare names instead would have meant teaching the
    sidecar model about directories end to end for one aligner.
    """

    suffixes: tuple[str, ...]
    # The separate binary that builds this index, when there is one. bwa-mem2
    # uses `bwa-mem2 index` and minimap2 uses `minimap2 -d`, so both are None.
    builder: str | None = None
    # Set for the directory shape: the suffix naming the directory itself,
    # e.g. `.STARindex`. None for the suffix and prefix shapes.
    directory_suffix: str | None = None

    def filenames(self, reference_name: str) -> tuple[str, ...]:
        return tuple(f"{reference_name}{s}" for s in self.suffixes)

    def directory_name(self, reference_name: str) -> str | None:
        if self.directory_suffix is None:
            return None
        return f"{reference_name}{self.directory_suffix}"

    def workdir_path(self, reference_name: str, stored_name: str) -> str:
        """Where a stored sidecar belongs in the workdir, relative to it.

        Identity for the suffix and prefix shapes. For the directory shape,
        `genome.fna.STARindex.SA` becomes `genome.fna.STARindex/SA` -- the
        one place the flattening is undone.

        A name that does not carry this layout's directory prefix is returned
        unchanged rather than forced into the directory: `fai` and `bai`
        sidecars travel in the same dict as the index members and belong
        beside the reference, not inside its index directory.
        """
        directory = self.directory_name(reference_name)
        if directory is None:
            return stored_name
        prefix = f"{directory}."
        if not stored_name.startswith(prefix):
            return stored_name
        return f"{directory}/{stored_name[len(prefix):]}"

    def reference_argument(self, reference: Path) -> str:
        """What to hand the aligner to locate the index.

        The same string for the suffix and prefix shapes, and deliberately so:
        index files are named after the *full* reference filename
        (`genome.fna.1.bt2`), so bowtie2's basename is the full reference
        path. Stripping the extension to form a basename would make the tool
        look for `genome.1.bt2` and find nothing.

        The directory shape is where this stops being an identity function:
        STAR wants the directory, not the reference inside it, and handing it
        the reference path produces "genome directory does not exist".
        """
        directory = self.directory_name(reference.name)
        if directory is None:
            return str(reference)
        return str(reference.parent / directory)

    def owns_sidecar(self, reference_name: str, sidecar_name: str) -> bool:
        return Path(sidecar_name).name.startswith(reference_name)


_LAYOUTS: dict[Aligner, IndexLayout] = {
    Aligner.BWA_MEM2: IndexLayout(suffixes=BWA_MEM2_SUFFIXES),
    Aligner.MINIMAP2: IndexLayout(suffixes=(MINIMAP2_SUFFIX,)),
    Aligner.BOWTIE2: IndexLayout(suffixes=BOWTIE2_SUFFIXES, builder="bowtie2-build"),
    Aligner.HISAT2: IndexLayout(suffixes=HISAT2_SUFFIXES, builder="hisat2-build"),
    # STAR indexes through its own binary in a different --runMode, so there
    # is no separate builder the way bowtie2-build is one.
    Aligner.STAR: IndexLayout(
        suffixes=STAR_SUFFIXES, directory_suffix=STAR_DIR_SUFFIX
    ),
    # winnowmap's builder is meryl, not winnowmap itself -- the same
    # separate-binary shape as bowtie2-build/hisat2-build, except the
    # produced file is consumed via `-W` rather than discovered by suffix at
    # alignment time the way a minimizer index would be.
    Aligner.WINNOWMAP: IndexLayout(
        suffixes=(WINNOWMAP_REPETITIVE_KMER_SUFFIX,), builder="meryl"
    ),
}

# The annotated variant only exists for STAR, so it is a separate one-entry
# table rather than a second dimension on `_LAYOUTS` that every other aligner
# would carry a meaningless value for.
_ANNOTATED_LAYOUTS: dict[Aligner, IndexLayout] = {
    Aligner.STAR: IndexLayout(
        suffixes=STAR_ANNOTATED_SUFFIXES, directory_suffix=STAR_ANNOTATED_DIR_SUFFIX
    ),
}


def layout_for(aligner: Aligner, *, annotated: bool = False) -> IndexLayout:
    if annotated:
        if aligner not in _ANNOTATED_LAYOUTS:
            raise ValueError(f"{aligner.value} has no annotated index")
        return _ANNOTATED_LAYOUTS[aligner]
    return _LAYOUTS[aligner]


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
        """True when no sidecar at all was linked.

        Deliberately weaker than `missing_index_for`: a caller that does not
        name an aligner (variant calling, which needs only the `.fai`) still
        gets a useful answer.
        """
        return not self.linked

    def missing_index_for(self, layout: "IndexLayout", reference_name: str) -> bool:
        """True when this aligner's own index files are not all present.

        `missing_index` alone is not enough to gate an alignment, and the gap
        is not theoretical: a reference carrying only a `.fai` has *something*
        linked, so the weaker check passed and the run proceeded to fail
        inside the aligner -- STAR reporting that its genome directory did not
        exist, which reads as a broken index rather than an absent one.
        """
        expected = {
            layout.workdir_path(reference_name, name)
            for name in layout.filenames(reference_name)
        }
        return not expected.issubset(set(self.linked))


def plan_links(
    *,
    reference_name: str,
    sidecars: dict[str, str],
    layout: IndexLayout | None = None,
) -> dict[str, str]:
    """Map workdir path -> blob path, for a reference and its sidecars.

    Pure: the mapping is the decision worth testing, separately from whether
    the symlinks were created. `sidecars` is {stored sidecar name: blob path},
    and the keys of the result are paths *relative to the workdir* -- which is
    the same thing as a filename for every layout except the directory shape.

    A sidecar whose name does not belong to this reference is dropped rather
    than linked under a corrected name. Silently renaming it would hide a
    genuine bookkeeping error -- an index attached to the wrong reference -- in
    exactly the way that produces a plausible-looking wrong result.

    `layout` is optional because most callers only need the reference and its
    `.fai`: `variant_handlers` materializes a reference to call against and
    never touches an aligner index. Omitting it for a STAR index would link
    the members flat, which STAR reads as a missing genome directory -- an
    error, not a wrong answer.
    """
    links: dict[str, str] = {}
    for name, blob in sidecars.items():
        # `Path(name).name` is the sanitizer, and it runs *before* any layout
        # translation for a reason: a stored name is untrusted input, and
        # translating first would let `../../etc/x` become a path that escapes
        # the workdir. Stored names are flat by construction, so this only
        # ever discards a directory component that should not have been there.
        safe = Path(name).name
        if not safe.startswith(reference_name):
            log.warning(
                "sidecar_name_mismatch",
                reference=reference_name,
                sidecar=safe,
            )
            continue
        if layout is not None:
            links[layout.workdir_path(reference_name, safe)] = blob
        else:
            links[safe] = blob
    return links


def materialize(
    *,
    workdir: Path,
    reference_name: str,
    reference_blob: Path,
    sidecars: dict[str, str],
    layout: IndexLayout | None = None,
) -> MaterializedRef:
    """Build a directory where a reference and its sidecars appear as siblings.

    ```
    tmp/align/<job_id>/ref/genome.fna      -> the reference blob
                          genome.fna.bwt.2bit.64 -> a sidecar blob
                          genome.fna.fai   -> ...
    ```

    A directory-shaped index (STAR) gains one level, built from the same flat
    sidecar names:

    ```
    tmp/align/<job_id>/ref/genome.fna
                          genome.fna.fai
                          genome.fna.STARindex/SA
                          genome.fna.STARindex/Genome
    ```

    Symlinks rather than copies: an index for a mammalian genome is several
    gigabytes -- STAR's is ~30 GB -- and every tool here follows links happily.
    """
    workdir.mkdir(parents=True, exist_ok=True)

    safe_reference = Path(reference_name).name
    reference_link = workdir / safe_reference
    _link(reference_link, reference_blob)

    linked: list[str] = []
    planned = plan_links(
        reference_name=safe_reference, sidecars=sidecars, layout=layout
    )
    for name, blob in sorted(planned.items()):
        link = workdir / name
        # Only the directory shape ever has a parent to create, but doing it
        # unconditionally costs one stat and keeps the loop free of a branch
        # that would only be exercised by one aligner.
        link.parent.mkdir(parents=True, exist_ok=True)
        _link(link, Path(blob))
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
