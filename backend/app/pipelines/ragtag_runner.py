"""RagTag command construction and output parsing.

Same split `polypolish_runner` and `ivar_runner` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.

Verified against a real `ragtag 2.1.0` install on 2026-08-05 (a synthetic
2-chromosome reference and a 7-contig draft cut from it, shuffled and partly
reverse-complemented). Two findings from that run shape this module and are
recorded in detail on the design doc
(`docs/superpowers/specs/2026-08-05-ragtag-scaffolding-design.md`):

- **RagTag exits 0 when it fails.** Given an unrelated reference it raises
  `RuntimeError: There are no useful alignments`, writes no
  `ragtag.scaffold.fasta`, and returns status 0. The handler must not trust
  the return code -- see `reference_assembly_handlers.scaffold_assembly`.
- **Reference argument order is `<reference> <query>`**, the opposite of how
  the two objects read in this app's UI (draft first, reference second). An
  easy transposition to get backwards and a silent one: RagTag would happily
  scaffold the reference against the draft.
"""

import re
from pathlib import Path


# `ragtag.py scaffold`'s own default (`-x asm5`), which assumes roughly 5%
# divergence -- appropriate for the same or a very closely related species.
# The wrong preset does not error; minimap2 simply finds fewer alignments and
# RagTag places fewer contigs, which reads as a poor assembly rather than a
# wrong setting. See `mm2_params_for_divergence`.
class Divergence:
    SAME_SPECIES = "same_species"
    SAME_GENUS = "same_genus"
    DISTANT = "distant"


_MM2_PRESETS = {
    Divergence.SAME_SPECIES: "-x asm5",
    Divergence.SAME_GENUS: "-x asm10",
    Divergence.DISTANT: "-x asm20",
}


def mm2_params_for_divergence(divergence: str) -> str:
    """The `--mm2-params` value for a chosen divergence level.

    Defaults to RagTag's own default (asm5) for an unrecognised value rather
    than raising -- the same posture `align_runner.preset_for_chemistry`
    takes for UNKNOWN, so a value that fails to round-trip through the API
    degrades to the tool's own safe default instead of failing a launch.
    """
    return _MM2_PRESETS.get(divergence, _MM2_PRESETS[Divergence.SAME_SPECIES])


def build_scaffold_command(
    *,
    ragtag_path: str,
    reference: Path,
    draft: Path,
    out_dir: Path,
    threads: int,
    divergence: str = Divergence.SAME_SPECIES,
) -> list[str]:
    """The argv for `ragtag.py scaffold`.

    `<reference> <query>` positional order -- reference first. This is the
    opposite of how the two inputs are named in this app (draft, then
    reference), and swapping them is not a syntax error to the tool: it
    would scaffold the reference against the draft and produce a plausible,
    wrong result. Assert this on the argv, not just in review.

    `-u` is not optional in practice, despite being one upstream: without it
    RagTag's own log warns that "some component/object AGP pairs might share
    the same ID," which produces an AGP some downstream tools reject.
    """
    return [
        ragtag_path,
        "scaffold",
        str(reference),
        str(draft),
        "-o",
        str(out_dir),
        "-t",
        str(threads),
        "-u",
        "--mm2-params",
        mm2_params_for_divergence(divergence),
    ]


def parse_stats(text: str) -> dict:
    """`scaffold_*` facts from `ragtag.scaffold.stats`, a clean two-line TSV:

        placed_sequences  placed_bp  unplaced_sequences  unplaced_bp  gap_bp  gap_sequences
        7                 100000     0                    0           500     5

    Returns {} for anything that fails to parse rather than raising, the
    same posture `polypolish_runner.parse_polish_stderr` and
    `ivar_runner.parse_consensus_stderr` take: a summary that cannot be read
    must not fail a job that already produced a scaffolded FASTA.
    """
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        return {}

    header = lines[0].split("\t")
    values = lines[1].split("\t")
    if len(header) != len(values):
        return {}

    row = dict(zip(header, values, strict=False))
    facts: dict = {}
    for key in (
        "placed_sequences",
        "placed_bp",
        "unplaced_sequences",
        "unplaced_bp",
        "gap_bp",
        "gap_sequences",
    ):
        if key in row:
            try:
                facts[f"scaffold_{key}"] = int(row[key])
            except ValueError:
                continue
    return facts


def parse_confidence(text: str) -> dict:
    """`scaffold_min_grouping_confidence` from `ragtag.scaffold.confidence.txt`:

        query           grouping_confidence  location_confidence  orientation_confidence
        ctg5_c1_0       1.0                   1.0                  1.0
        ctg1_c1_1       1.0                   1.0                  1.0

    The **minimum** across contigs, not the mean. A mean of 0.98 hides the
    one contig placed at 0.3, and that contig is the one worth a user's
    attention -- the design doc is explicit that this must not be averaged
    away.
    """
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        return {}

    header = lines[0].split("\t")
    try:
        col = header.index("grouping_confidence")
    except ValueError:
        return {}

    scores = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) <= col:
            continue
        try:
            scores.append(float(parts[col]))
        except ValueError:
            continue

    if not scores:
        return {}
    return {"scaffold_min_grouping_confidence": min(scores)}


_SCAFFOLD_HEADER_RE = re.compile(r"^>(\S+)", re.MULTILINE)


def count_scaffolds(fasta_text: str) -> int:
    """How many sequences the output FASTA contains -- the deliverable's own
    headers, not derived from `.stats`, since `.stats` counts input contigs
    placed, not output scaffolds (a placed-plus-unplaced count differs from
    the scaffold count whenever more than one contig joins one scaffold)."""
    return len(_SCAFFOLD_HEADER_RE.findall(fasta_text))
