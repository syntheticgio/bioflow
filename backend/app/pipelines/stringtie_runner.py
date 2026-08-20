"""Building and reading a StringTie run.

Kept separate from the job handler for the same reason `salmon_runner.py` is:
the parts worth testing -- command construction and output parsing -- are pure
functions over strings and paths, with no queue or filesystem involved.

The novel-transcript count is the part that earns the care. It is the only
number here that says what this tool did that no other tool in the app can do,
and it rests on a single fact about StringTie's output format: a transcript
the reference annotation already contained is emitted with a `reference_id`
attribute, and one StringTie proposed is not. Verified against real StringTie
2.2.1 output in both directions rather than recalled -- see the fixtures in
tests/pipelines/test_stringtie_runner.py.
"""

import re
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

# GTF is tab-separated with the feature type in column 3. Keying on the
# column rather than searching the line for "transcript" matters: the word
# also appears inside `transcript_id`, which every exon line carries, so a
# substring match would count each transcript once per exon.
_TRANSCRIPT_FEATURE = "transcript"

_GENE_ID_RE = re.compile(r'gene_id "([^"]+)"')


def assemble_command(
    *,
    bam: Path,
    annotation: Path,
    out_gtf: Path,
    stringtie_path: str,
    threads: int = 1,
) -> list[str]:
    """Argv for a reference-guided assembly of one alignment.

    `-G` is not optional here even though StringTie allows omitting it.
    Without a reference every assembled transcript gets a generated
    identifier (`STRG.1.1`) that nothing downstream can match to a gene, so
    the output would be uninterpretable rather than merely less informative.
    """
    return [
        stringtie_path,
        str(bam),
        "-G",
        str(annotation),
        "-o",
        str(out_gtf),
        "-p",
        str(threads),
    ]


def merge_command(
    *,
    stringtie_path: str,
    gtfs: list[Path],
    out_gtf: Path,
    reference_gtf: Path | None = None,
    min_len: int | None = None,
    min_cov: int | None = None,
) -> list[str]:
    """Argv for a non-redundant merge of N assembled-transcript GTFs.

    `--merge` takes the per-sample GTFs positionally (verified against
    StringTie 2.2.1: `stringtie --merge [Options] { gtf_list | strg1.gtf ... }`).
    The options -- `-o`, `-G`, `-m`, `-c` -- come before the positional list.
    `-G` is the reference annotation to include in the merge, and accepts both
    GTF and GFF3; it is present only when a reference is actually passed. A
    merge with no inputs is a caller bug, not a tool invocation.
    """
    if not gtfs:
        raise ValueError("merge_command needs at least one input GTF")
    argv = [stringtie_path, "--merge"]
    if reference_gtf is not None:
        argv += ["-G", str(reference_gtf)]
    argv += ["-o", str(out_gtf)]
    if min_len is not None:
        argv += ["-m", str(min_len)]
    if min_cov is not None:
        argv += ["-c", str(min_cov)]
    argv += [str(g) for g in gtfs]
    return argv


def parse_gtf(text: str) -> dict:
    """Assembled-transcript counts from a StringTie GTF.

    `novel_transcript_count` is transcripts carrying no `reference_id` --
    the models this run proposed rather than measured. It is reported
    alongside the total rather than instead of it for the same reason
    `salmon_runner.parse_quant` reports `transcripts_detected` next to the
    total: the total alone cannot separate "this sample assembled well" from
    "this annotation already described everything here", and the two numbers
    move differently in each case.

    An empty GTF is an empty result, not an error. StringTie exits zero on an
    alignment with too little coverage to assemble anything, writing only its
    header comments, and a caller that treated that as a failure would report
    a crash where the honest answer is "nothing assembled".
    """
    transcript_count = 0
    novel_transcript_count = 0
    genes: set[str] = set()

    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 9 or parts[2] != _TRANSCRIPT_FEATURE:
            continue

        attributes = parts[8]
        transcript_count += 1
        if "reference_id " not in attributes:
            novel_transcript_count += 1

        match = _GENE_ID_RE.search(attributes)
        if match:
            genes.add(match.group(1))

    return {
        "transcript_count": transcript_count,
        "novel_transcript_count": novel_transcript_count,
        "gene_count": len(genes),
    }
