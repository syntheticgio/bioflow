"""Assembly command construction, log parsing, and output harvesting.

The same split `align_runner` uses: pure functions over strings and paths, so
every one of them is testable without a container, a queue, or a binary.

The one thing worth knowing before reading: **genome size is not passed to the
assembler.** Flye stopped requiring `--genome-size` at 2.8, and passing it
changes behaviour only in combination with `--asm-coverage`, which BioFlow does
not offer. It is collected because BioFlow's own memory estimate needs it, and
sending it to the tool anyway would be a parameter recorded in a run's
provenance that the tool did not act on -- the same lie `align_params`'
docstring refuses for inapplicable aligner fields.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger
from app.pipelines.assemblers import Assembler, OutputKind
from app.pipelines.assembly_params import BaseAssemblyParams, FlyeParams

log = get_logger(__name__)


def build_assembly_command(
    *,
    assembler: Assembler,
    tool_path: str,
    reads: Path,
    out_dir: Path,
    params: BaseAssemblyParams,
) -> list[str]:
    """The argv for one assembly run."""
    if assembler is not Assembler.FLYE:
        # Not a fallback: an assembler with no builder here would otherwise
        # produce a Flye command line for a different binary.
        raise ValueError(f"No command builder for {assembler.value}")

    assert isinstance(params, FlyeParams)
    return [
        tool_path,
        f"--{params.mode}",
        str(reads),
        "--out-dir",
        str(out_dir),
        "--threads",
        str(params.threads),
        "--iterations",
        str(params.iterations),
    ]


# Flye announces each stage with a `>>>STAGE: <name>` line, and those names are
# the same vocabulary `--resume-from` accepts. Read off a real 2.9.5 log rather
# than guessed: an earlier version of this table matched on prose banners
# ("Assembly draft", "Building repeat graph") and only one of five patterns
# would ever have fired, which is a progress display that silently stops
# updating rather than one that fails.
_STAGE_RE = re.compile(r">>>STAGE:\s*(\w+)")

_STAGE_LABELS: dict[str, str] = {
    "configure": "configuring",
    "assembly": "assembling draft",
    "consensus": "building consensus",
    "repeat": "resolving repeats",
    "contigger": "generating contigs",
    "trestle": "resolving repeats",
    "polishing": "polishing",
    "finalize": "finishing",
}


@dataclass
class AssemblyProgress:
    """Turns Flye's own log into a phase name.

    Deliberately no percentage. Flye's five stages differ in duration by more
    than an order of magnitude -- polishing alone can outlast everything before
    it -- so a bar derived from "stage 3 of 5" would sit at 60% for most of a
    run and then jump. `align_runner`'s progress at least counts reads against
    an estimated total; there is no equivalent countable unit here, and a
    fabricated fraction is worse than an honest phase name.
    """

    phase: str = "starting"

    def feed(self, line: str) -> bool:
        """Consume a log line. True if the phase changed."""
        match = _STAGE_RE.search(line)
        if not match:
            return False
        stage = match.group(1).lower()
        # An unmapped stage still counts: a future Flye adding one should
        # display its raw name rather than leave the phase stuck on the
        # previous stage, which would read as a hang.
        phase = _STAGE_LABELS.get(stage, stage)
        if phase == self.phase:
            return False
        self.phase = phase
        return True

    def message(self) -> str:
        return self.phase


# assembly_info.txt is tab-separated with a `#seq_name` header:
#   #seq_name  length  cov.  circ.  repeat  mult.  alt_group  graph_path
_INFO_COLUMNS = ("length", "cov.", "circ.", "repeat", "mult.")


def parse_assembly_info(text: str) -> dict:
    """Per-contig facts from Flye's own table.

    This is the column that no generic FASTA parse can produce: `_parse_fasta`
    already reports contig count, longest and shortest, so what is actually
    new here is **coverage and circularity**. A circular contig is how a
    finished bacterial chromosome announces itself, and it is the first thing
    someone looks for.

    Returns {} for anything malformed rather than raising. A table that failed
    to parse must not fail an assembly that took six hours and produced a
    perfectly good FASTA.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {}

    header = lines[0].lstrip("#").split("\t")
    index = {name: i for i, name in enumerate(h.strip() for h in header)}
    if not all(col in index for col in _INFO_COLUMNS):
        log.warning("assembly_info_unexpected_header", header=header[:8])
        return {}

    contigs: list[dict] = []
    for line in lines[1:]:
        if line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < len(header):
            continue
        try:
            contigs.append(
                {
                    "name": cols[0].strip(),
                    "length": int(cols[index["length"]]),
                    "coverage": float(cols[index["cov."]]),
                    "circular": cols[index["circ."]].strip().upper() in ("Y", "+"),
                    "repeat": cols[index["repeat"]].strip().upper() in ("Y", "+"),
                    "multiplicity": int(cols[index["mult."]]),
                }
            )
        except (ValueError, IndexError):
            # One unparseable row should not discard the rest of the table.
            continue

    if not contigs:
        return {}

    circular = [c for c in contigs if c["circular"]]
    total = sum(c["length"] for c in contigs)
    return {
        "assembly_contig_count": len(contigs),
        "assembly_total_length": total,
        "assembly_circular_count": len(circular),
        # The headline number for a bacterial assembly: one circular contig of
        # roughly the expected size is a finished chromosome.
        "assembly_longest_circular": max((c["length"] for c in circular), default=0),
        "assembly_mean_coverage": (
            round(sum(c["coverage"] * c["length"] for c in contigs) / total, 2)
            if total
            else 0.0
        ),
        "assembly_n50": _n50([c["length"] for c in contigs]),
        # Capped for the same reason `parsers.MAX_STORED_CONTIGS` is: a
        # fragmented draft has tens of thousands of rows and the facts
        # document is not where they belong.
        "assembly_contigs": sorted(contigs, key=lambda c: -c["length"])[:50],
        "assembly_contigs_truncated": len(contigs) > 50,
    }


def _n50(lengths: list[int]) -> int:
    """The length at which half the assembly sits in contigs at least that long.

    Genuinely absent from the generic FASTA parse -- `sequence_stats` computes
    composition only -- so this is the one QUAST-shaped number that arrives
    without QUAST.
    """
    if not lengths:
        return 0
    ordered = sorted(lengths, reverse=True)
    half = sum(ordered) / 2
    running = 0
    for length in ordered:
        running += length
        if running >= half:
            return length
    return ordered[-1]


def harvest(out_dir: Path, outputs) -> dict[OutputKind, Path]:
    """Which declared outputs the assembler actually produced.

    Missing optional outputs are simply absent from the result; a missing
    required one raises, because a run without contigs did not assemble
    anything however cleanly it exited.
    """
    found: dict[OutputKind, Path] = {}
    for output in outputs:
        path = out_dir / output.filename
        if path.exists() and path.stat().st_size > 0:
            found[output.kind] = path
        elif output.required:
            raise FileNotFoundError(
                f"{output.filename} is missing from the assembly output. The "
                f"assembler exited successfully but produced no contigs."
            )
    return found
