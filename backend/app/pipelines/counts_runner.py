"""Building and observing a featureCounts run.

Kept separate from the job handler for the same reason `variant_runner.py` is:
the parts worth testing -- command construction, the strandedness mapping, the
annotation attribute choice, summary parsing -- are pure functions over strings
and paths, with no queue or filesystem involved.

Two of those functions exist because of failures that do not look like
failures. `strandedness_for_align_params` translates the aligner's library
orientation into featureCounts' `-s`, and a wrong value there returns counts
near zero rather than an error. `attributes_for_format` picks the feature type
and grouping attribute, and the defaults everyone quotes (`-t exon -g gene_id`)
are GTF-shaped: NCBI's GFF3 exon lines carry no `gene_id` at all. Both were
checked against the annotations this application actually downloads -- see the
comments on each.
"""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from app.errors import ValidationError
from app.logging import get_logger
from app.models import FormatKind

log = get_logger(__name__)


# featureCounts' -s. Not an enum: these are the literal values the flag takes,
# they are ordered, and every place they are used wants the integer.
UNSTRANDED = 0
FORWARD = 1
REVERSE = 2

STRANDEDNESS_LABELS: dict[int, str] = {
    UNSTRANDED: "unstranded",
    FORWARD: "forward",
    REVERSE: "reverse",
}

# HISAT2's --rna-strandness to featureCounts' -s.
#
# F/FR mean the read (or the first mate) is on the transcript's strand;
# R/RF mean it is on the opposite strand, which is what every dUTP protocol
# produces and therefore the common stranded case. The single- and paired-end
# spellings map to the same -s value because -s describes the orientation, and
# `-p` is what tells featureCounts there are two mates to consider.
_HISAT2_STRANDNESS: dict[str, int] = {
    "F": FORWARD,
    "FR": FORWARD,
    "R": REVERSE,
    "RF": REVERSE,
}


def strandedness_for_align_params(align_params: dict | None) -> int | None:
    """featureCounts' -s implied by the alignment's library orientation.

    None when the alignment did not record one -- either it was not HISAT2, or
    it was run unstranded, or the BAM predates the field. None means "nothing
    to infer", *not* unstranded: the caller decides what an unknown becomes,
    and conflating the two here would silently turn a stranded library into an
    unstranded count.

    This mapping exists because a mismatch between the library prep and `-s`
    does not fail. featureCounts happily attributes reads to the wrong strand
    and returns a counts file that is structurally perfect and near-zero
    throughout, which reads as "the experiment did not work" rather than as a
    parameter error. Reading the value back off the alignment is the only way
    the user does not have to answer the same question twice and get it right
    both times.
    """
    if not align_params:
        return None
    raw = align_params.get("rna_strandness")
    if not raw:
        # HISAT2 writes "" for unstranded, which is a real answer rather than a
        # missing one -- but it is indistinguishable here from a non-HISAT2
        # alignment that never had the field. Both return None and let the
        # caller default, which costs an unstranded library nothing: its
        # default is unstranded anyway.
        return None
    return _HISAT2_STRANDNESS.get(str(raw).upper())


def paired_from_facts(facts: dict | None) -> bool | None:
    """Whether a BAM holds paired reads, from the facts `index_bam` recorded.

    `properly_paired_reads` comes from `samtools flagstat`, which prints the
    line for every BAM -- and prints 0 for a single-end one, since a read with
    no mate can never be properly paired. So a positive value is a definite
    yes and the key's absence is a definite "nobody has looked", but a zero is
    genuinely ambiguous: it is what a single-end BAM gives *and* what a paired
    alignment gives when every pair failed to align concordantly.

    Returning None for that ambiguous case rather than guessing. The caller
    falls back to the alignment run's own inputs, which knew whether there was
    a mate file before anything was aligned.
    """
    if not facts:
        return None
    value = facts.get("properly_paired_reads")
    if value is None:
        return None
    if value > 0:
        return True
    return None


def attributes_for_format(kind: FormatKind | str | None) -> tuple[str, str]:
    """The `-t` feature type and `-g` grouping attribute for an annotation.

    Returns GTF's conventional pair for GTF, and something different for GFF3
    -- which is the point of the function.

    Checked against the files this application downloads rather than recalled.
    NCBI Datasets ships both formats for an assembly, and on
    GCF_000146045.2 (S. cerevisiae R64) they differ in exactly the way that
    matters:

        GTF  exon ... gene_id "YAL068C"; transcript_id "NM_001180043.1"; ...
        GFF3 exon ... ID=exon-NM_001180043.1-1;Parent=rna-NM_001180043.1;
                      gene=PAU8;locus_tag=YAL068C;...

    There is no `gene_id` anywhere on the GFF3 exon line. The quoted default
    `-t exon -g gene_id` therefore fails outright on the GFF3 while working on
    the GTF, which is why the launch path prefers the GTF when a project holds
    both (see `pipeline_service.resolve_annotation`).

    `locus_tag` rather than `gene` for GFF3, measured on that same file across
    its 6852 exon lines:

        locus_tag   6852  (100%)
        gene        5790  (84.5%)
        gene_id        0

    `gene` is a symbol and is simply absent on the ~15% of features that have
    never been named, so grouping on it drops them without a word. `locus_tag`
    is present on all of them and is stable across annotation releases.
    """
    kind_str = str(getattr(kind, "value", kind) or "").lower()
    if kind_str == "gff":
        return ("exon", "locus_tag")
    # GTF and anything unrecognized. GTF's pair is both the conventional
    # default and the one featureCounts assumes, so an annotation we cannot
    # classify is best served by the behaviour a user would expect from
    # reading any featureCounts documentation.
    return ("exon", "gene_id")


@dataclass
class CountsParams:
    """User-facing knobs for a quantification run."""

    threads: int = 4
    # featureCounts' -s. See strandedness_for_align_params: the launch path
    # fills this in from the alignment where it can, and the dialog shows what
    # it chose rather than asking blind.
    strandedness: int = UNSTRANDED
    # -p --countReadPairs. Counts fragments rather than reads, so a properly
    # paired mate pair contributes 1 and not 2. Wrong-way-round on a
    # single-end BAM featureCounts merely warns about, but the count doubles
    # on paired data if it is left off, which is the direction that quietly
    # corrupts a comparison between differently-prepped samples.
    paired: bool = False
    # -t / -g. Defaults are GTF's; attributes_for_format overrides for GFF3.
    feature_type: str = "exon"
    attribute: str = "gene_id"
    # -M. Off by default, matching featureCounts' own default and DESeq2's
    # assumption that a count is a count of fragments. Turning it on inflates
    # counts for gene families and multi-copy loci.
    count_multi_mapping: bool = False

    def as_dict(self) -> dict:
        return {
            "threads": self.threads,
            "strandedness": self.strandedness,
            "strandedness_label": STRANDEDNESS_LABELS.get(
                self.strandedness, "unknown"
            ),
            "paired": self.paired,
            "feature_type": self.feature_type,
            "attribute": self.attribute,
            "count_multi_mapping": self.count_multi_mapping,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "CountsParams":
        raw = dict(raw or {})

        threads = int(raw.get("threads", 4))
        if threads < 1:
            raise ValidationError("threads must be at least 1")

        strandedness = int(raw.get("strandedness", UNSTRANDED))
        if strandedness not in STRANDEDNESS_LABELS:
            raise ValidationError(
                f"strandedness must be one of {sorted(STRANDEDNESS_LABELS)} "
                f"(unstranded, forward, reverse), not {strandedness!r}",
                details={"valid": sorted(STRANDEDNESS_LABELS)},
            )

        feature_type = str(raw.get("feature_type") or "exon")
        attribute = str(raw.get("attribute") or "gene_id")

        return cls(
            threads=threads,
            strandedness=strandedness,
            paired=bool(raw.get("paired", False)),
            feature_type=feature_type,
            attribute=attribute,
            count_multi_mapping=bool(raw.get("count_multi_mapping", False)),
        )


def output_name(bam_name: str) -> str:
    """The counts file name for an alignment.

    Named after the BAM rather than the reads, matching `variant_runner`: two
    alignments of the same reads against different references produce
    different counts, and naming both after the reads would collide.
    """
    stem = Path(bam_name).stem
    return f"{stem}.counts.tsv"


def build_command(
    *,
    bam: str | Path,
    annotation: str | Path,
    output: str | Path,
    params: CountsParams,
    featurecounts_path: str = "featureCounts",
) -> list[str]:
    """The featureCounts invocation for one sample."""
    cmd = [
        featurecounts_path,
        "-T", str(params.threads),
        "-a", str(annotation),
        "-o", str(output),
        "-t", params.feature_type,
        "-g", params.attribute,
        "-s", str(params.strandedness),
    ]

    if params.paired:
        # Both flags, not just -p. In featureCounts 2.x, -p alone says "this
        # input is paired-end" and still counts *reads*; --countReadPairs is
        # what switches the unit to fragments. Passing only -p was the 1.x
        # behaviour and silently doubles every count here.
        cmd += ["-p", "--countReadPairs"]

    if params.count_multi_mapping:
        cmd.append("-M")

    cmd.append(str(bam))
    return cmd


def summary_path(output: str | Path) -> Path:
    """Where featureCounts writes its assignment summary.

    Not configurable: it appends `.summary` to whatever `-o` was given.
    """
    return Path(str(output) + ".summary")


def parse_summary(text: str) -> dict:
    """featureCounts' `.summary` file as facts.

    Two columns, a header line and then one `Status<TAB>N` row per assignment
    outcome. Only the totals are kept plus the assignment rate, which is the
    number worth looking at: a rate near zero on a file that otherwise ran
    cleanly is the signature of wrong strandedness or a mismatched annotation,
    and it is the only place either of those two silent failures becomes
    visible.
    """
    counts: dict[str, int] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("Status"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            counts[parts[0].strip()] = int(parts[1].strip())
        except ValueError:
            continue

    if not counts:
        return {}

    assigned = counts.get("Assigned", 0)
    total = sum(counts.values())

    facts: dict = {
        "assigned_fragments": assigned,
        "total_fragments": total,
        "unassigned_no_features": counts.get("Unassigned_NoFeatures", 0),
        "unassigned_ambiguity": counts.get("Unassigned_Ambiguity", 0),
        "unassigned_multimapping": counts.get("Unassigned_MultiMapping", 0),
    }
    if total:
        facts["assigned_pct"] = round(100 * assigned / total, 2)
    return facts


# featureCounts writes a two-line preamble before the table: a `#` comment
# holding the command line, then the column header starting with "Geneid".
_HEADER_RE = re.compile(r"^Geneid\t")


def parse_counts(text: str) -> tuple[dict[str, int], dict]:
    """A featureCounts table as {gene_id: count} plus summary facts.

    The table's last column holds the counts; the six before it are Geneid,
    Chr, Start, End, Strand, Length. Indexed from the end rather than by
    position so a future flag that adds a column does not silently shift which
    one is read as the count.

    `genes_detected` -- genes with at least one fragment -- is returned
    alongside the total because it is the second signal that separates "this
    sample is bad" from "this parameter is wrong": a strandedness error
    flattens it near zero while leaving the gene count untouched.
    """
    counts: dict[str, int] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#") or _HEADER_RE.match(line):
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 7:
            continue
        try:
            counts[parts[0]] = int(parts[-1])
        except ValueError:
            continue

    total = sum(counts.values())
    facts = {
        "genes_in_annotation": len(counts),
        "genes_detected": sum(1 for v in counts.values() if v > 0),
        "counted_fragments": total,
    }
    return counts, facts


def describe(params: CountsParams) -> str:
    """A one-line description of what a run counted, for a log or a label."""
    unit = "fragments" if params.paired else "reads"
    strand = STRANDEDNESS_LABELS.get(params.strandedness, "unknown")
    return f"{unit}, {strand}, grouped by {params.attribute}"


def command_line(cmd: list[str]) -> str:
    """The command as a copy-pasteable string, for provenance."""
    return shlex.join(cmd)
