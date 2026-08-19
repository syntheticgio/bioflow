"""Building and reading a Salmon run.

Kept separate from the job handler for the same reason `counts_runner.py` is:
the parts worth testing -- command construction, output parsing, and the
transcript-to-gene summarization -- are pure functions over strings, paths and
dicts, with no queue or filesystem involved.

The summarization is the part that earns the care. Salmon reports fractional
reads per *transcript*; pyDESeq2 here consumes integer counts per *gene*. The
bridge between them has to agree with what featureCounts calls a gene, or two
count files that look interchangeable describe different gene universes.
"""

import re
import shlex
from pathlib import Path

from app.errors import ValidationError
from app.logging import get_logger

log = get_logger(__name__)

# quant.sf's header. Salmon writes exactly these five columns; NumReads is
# last and is a float, not an integer.
_QUANT_HEADER_RE = re.compile(r"^Name\tLength\tEffectiveLength\tTPM\tNumReads")


def parse_quant(text: str) -> tuple[dict[str, float], dict]:
    """A `quant.sf` table as {transcript_id: num_reads} plus summary facts.

    `NumReads` is an *estimate*, and fractional: Salmon distributes a
    multi-mapping read across the transcripts it is compatible with rather
    than discarding it or assigning it arbitrarily. Keeping the float here and
    rounding only after transcripts are summed to genes matters -- rounding
    per transcript first would discard a fraction of a read thousands of times
    over and drag every gene's count down.

    `transcripts_detected` is returned alongside the total for the same reason
    `counts_runner.parse_counts` returns `genes_detected`: the total alone
    cannot separate "this sample is bad" from "this is the wrong
    transcriptome", and the detected count moves differently in each case.
    """
    per_transcript: dict[str, float] = {}
    for line in text.splitlines():
        if not line.strip() or _QUANT_HEADER_RE.match(line):
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 5:
            continue
        try:
            per_transcript[parts[0]] = float(parts[-1])
        except ValueError:
            continue

    facts = {
        "transcripts_in_index": len(per_transcript),
        "transcripts_detected": sum(1 for v in per_transcript.values() if v > 0),
        "estimated_reads": sum(per_transcript.values()),
    }
    return per_transcript, facts


# NCBI CDS deflines carry bracketed attributes after the sequence ID:
#   >lcl|NC_001133.9_cds_NP_009332.1_1 [gene=PAU8] [locus_tag=YAL068C] ...
# Verified against GCF_000146045.2 (S. cerevisiae R64) rather than recalled --
# see the plan's defline verification task.
_ATTR_RE = re.compile(r"\[(\w+)=([^\]]*)\]")

# locus_tag first, deliberately. `counts_runner.attributes_for_format` groups
# NCBI GFF3 by locus_tag, so preferring `gene` here would produce a gene
# universe that does not match featureCounts output for the same organism --
# two counts files that look interchangeable and are not.
_GENE_ATTRIBUTES = ("locus_tag", "gene")


def parse_tx2gene(headers: list[str]) -> dict[str, str]:
    """Transcript ID to gene ID, from a transcriptome FASTA's deflines.

    Raises rather than guessing. The tempting fallback -- when a defline
    carries no gene attribute, use the transcript ID as its own gene -- is
    what makes this function dangerous: it produces a counts file with one
    "gene" per transcript that merges cleanly, passes every downstream sanity
    check, and quietly tests a gene universe the user never chose. Nothing
    downstream can detect it. So an unmappable header is an error naming the
    header, which a user can act on.
    """
    mapping: dict[str, str] = {}
    for header in headers:
        line = header[1:] if header.startswith(">") else header
        line = line.strip()
        if not line:
            continue

        transcript_id = line.split(None, 1)[0]
        attrs = dict(_ATTR_RE.findall(line))

        gene_id = None
        for key in _GENE_ATTRIBUTES:
            value = (attrs.get(key) or "").strip()
            if value:
                gene_id = value
                break

        if gene_id is None:
            raise ValidationError(
                "This transcriptome's sequence names do not say which gene "
                "each transcript belongs to, so transcript estimates cannot "
                f"be summed into genes. First one: {transcript_id!r}. "
                "A CDS or RNA FASTA downloaded from NCBI carries "
                "[locus_tag=...] or [gene=...] on every sequence.",
                details={"header": line[:200], "transcript_id": transcript_id},
            )

        mapping[transcript_id] = gene_id

    return mapping


def summarize_to_gene(
    per_transcript: dict[str, float], tx2gene: dict[str, str]
) -> tuple[dict[str, int], dict]:
    """Transcript-level estimates summed into integer gene-level counts.

    The tximport equivalent, and the reason Salmon output can feed the same
    differential expression test featureCounts output does.

    Two details that would each be a silent error if done the other way.
    Rounding happens once, after summing: a gene with three transcripts at 0.4
    estimated reads each has a read's worth of evidence, and rounding per
    transcript first would throw all of it away, thousands of times over.
    And every gene in the map is present in the output even at zero, because
    the gene universe belongs to the reference rather than to the sample --
    `de_runner.merge_counts` refuses samples whose gene sets differ at all, so
    dropping a gene that happened to get no reads in one sample would break
    the merge for the whole experiment.
    """
    unknown = set(per_transcript) - set(tx2gene)
    if unknown:
        raise ValidationError(
            f"{len(unknown)} transcripts in the quantification are not in the "
            "transcript-to-gene map, so their reads would be silently "
            f"dropped. First: {sorted(unknown)[0]!r}. This usually means the "
            "index was built from a different file than the one being "
            "summarized.",
            details={"unknown": sorted(unknown)[:5], "count": len(unknown)},
        )

    totals: dict[str, float] = {gene: 0.0 for gene in tx2gene.values()}
    for transcript, reads in per_transcript.items():
        totals[tx2gene[transcript]] += reads

    counts = {gene: round(value) for gene, value in totals.items()}
    facts = {
        "genes_in_reference": len(counts),
        "genes_detected": sum(1 for v in counts.values() if v > 0),
        "counted_fragments": sum(counts.values()),
    }
    return counts, facts


def index_command(
    *,
    transcriptome: Path,
    index_dir: Path,
    salmon_path: str,
    threads: int = 4,
) -> list[str]:
    """`salmon index` for one transcriptome.

    Built once per transcriptome and reused by every sample, which is why this
    is a separate command rather than folded into the quantification: on a
    twelve-sample experiment the index is built once instead of twelve times.
    """
    return [
        salmon_path,
        "index",
        "-t",
        str(transcriptome),
        "-i",
        str(index_dir),
        "-p",
        str(threads),
    ]


def quant_command(
    *,
    index_dir: Path,
    reads: list[Path],
    out_dir: Path,
    salmon_path: str,
    threads: int = 4,
) -> list[str]:
    """`salmon quant` for one sample.

    `-l A` always. Salmon infers the library's strandedness from the data and
    reports what it inferred, so unlike the featureCounts path there is no
    orientation for a user to supply and therefore none to get wrong -- the
    failure `counts_runner.strandedness_for_align_params` exists to prevent
    cannot happen here.
    """
    if not reads:
        raise ValidationError("Salmon needs at least one reads file.")
    if len(reads) > 2:
        raise ValidationError(
            "Salmon quantifies one sample at a time: either one file of "
            f"single-end reads or two of paired-end, not {len(reads)}.",
            details={"files": [str(r) for r in reads]},
        )

    cmd = [salmon_path, "quant", "-i", str(index_dir), "-l", "A"]
    if len(reads) == 2:
        cmd += ["-1", str(reads[0]), "-2", str(reads[1])]
    else:
        cmd += ["-r", str(reads[0])]
    cmd += ["-o", str(out_dir), "-p", str(threads)]
    return cmd


def quant_file(out_dir: Path) -> Path:
    """Where `salmon quant` writes its abundance table."""
    return out_dir / "quant.sf"


def output_name(sample_name: str) -> str:
    """The counts file name for a sample, matching the counts_runner shape."""
    stem = Path(sample_name).name
    for suffix in (".gz", ".fastq", ".fq"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    stem = stem.removesuffix("_1").removesuffix("_R1")
    return f"{stem}.counts.tsv"


def command_line(cmd: list[str]) -> str:
    """The command as a copy-pasteable string, for provenance."""
    return shlex.join(cmd)
