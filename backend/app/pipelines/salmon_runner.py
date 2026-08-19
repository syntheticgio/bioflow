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
