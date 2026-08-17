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
from dataclasses import dataclass, field
from pathlib import Path

from app.logging import get_logger
from app.pipelines import assembler_registry
from app.pipelines.assemblers import Assembler, OutputKind
from app.pipelines.assembly_params import AbyssParams, BaseAssemblyParams, FlyeParams

log = get_logger(__name__)

# ABySS refuses to start without a Bloom filter budget, so a run with no memory
# estimate still needs a number. 200M is small enough to be safe anywhere and
# large enough to assemble a bacterial genome.
MIN_BLOOM_MB = 200


def build_assembly_command(
    *,
    assembler: Assembler,
    tool_path: str,
    reads: Path,
    out_dir: Path,
    params: BaseAssemblyParams,
    mate: Path | None = None,
    bloom_bytes: int | None = None,
) -> list[str]:
    """The argv for one assembly run.

    `mate` and `bloom_bytes` are ABySS-only and ignored by the Flye builder --
    a paired long-read assembly is not a thing, and Flye needs no memory
    ceiling to start.
    """
    if assembler is Assembler.FLYE:
        assert isinstance(params, FlyeParams)
        return _flye_command(
            tool_path=tool_path, reads=reads, out_dir=out_dir, params=params
        )
    if assembler is Assembler.ABYSS:
        assert isinstance(params, AbyssParams)
        return _abyss_command(
            tool_path=tool_path,
            reads=reads,
            out_dir=out_dir,
            params=params,
            mate=mate,
            bloom_bytes=bloom_bytes,
        )
    # Not a fallback: an assembler with no builder here would otherwise
    # produce another tool's command line for this binary.
    raise ValueError(f"No command builder for {assembler.value}")


def _flye_command(
    *, tool_path: str, reads: Path, out_dir: Path, params: FlyeParams
) -> list[str]:
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


def _abyss_command(
    *,
    tool_path: str,
    reads: Path,
    out_dir: Path,
    params: AbyssParams,
    mate: Path | None,
    bloom_bytes: int | None,
) -> list[str]:
    """`abyss-pe` takes Make variable assignments, not flags.

    `-C <dir>` is make's own change-directory option and is how the outputs
    land in `out_dir` -- ABySS has no `--out-dir` equivalent and would
    otherwise write into the process's cwd.
    """
    bloom_mb = MIN_BLOOM_MB
    if bloom_bytes:
        bloom_mb = max(MIN_BLOOM_MB, int(bloom_bytes / (1024 * 1024)))

    cmd = [
        tool_path,
        "-C",
        str(out_dir),
        f"name={assembler_registry.ASSEMBLY_NAME_PREFIX}",
        f"k={params.k}",
        f"j={params.threads}",
        f"B={bloom_mb}M",
    ]
    if mate is not None:
        # Both mates in one space-joined value: ABySS's `in` variable is a
        # single Make variable holding a read pair, not a repeatable flag.
        cmd.append(f"in={reads} {mate}")
    else:
        cmd.append(f"se={reads}")
    return cmd


# Flye announces each stage with a `>>>STAGE: <name>` line, and those names are
# the same vocabulary `--resume-from` accepts. Read off a real 2.9.5 log rather
# than guessed: an earlier version of this table matched on prose banners
# ("Assembly draft", "Building repeat graph") and only one of five patterns
# would ever have fired, which is a progress display that silently stops
# updating rather than one that fails.
_STAGE_RE = re.compile(r">>>STAGE:\s*(\w+)")

# The stages Flye runs, in order. Not a guess and not open-ended:
# `flye/main.py:_create_job_list` appends these seven jobs at launch, before
# any work starts, so the sequence is knowable before the process runs.
_FLYE_STAGES: tuple[str, ...] = (
    "configure",
    "assembly",
    "consensus",
    "repeat",
    "contigger",
    "polishing",
    "finalize",
)

_STAGE_LABELS: dict[str, str] = {
    "configure": "configuring",
    "assembly": "assembling draft",
    "consensus": "building consensus",
    "repeat": "resolving repeats",
    "contigger": "generating contigs",
    "polishing": "polishing",
    "finalize": "finishing",
}


def flye_stage_order(params: FlyeParams) -> tuple[str, ...]:
    """The stages this particular run will execute, in order.

    Mirrors the two conditionals in `_create_job_list`. Only one of them can
    fire here: `consensus` is skipped for `read_type == "subasm"`, which is
    not among the modes `assembler_registry` offers, while `--iterations 0`
    genuinely does drop polishing and is selectable in the dialog
    (`MIN_ITERATIONS = 0`).
    """
    if params.iterations > 0:
        return _FLYE_STAGES
    return tuple(stage for stage in _FLYE_STAGES if stage != "polishing")


@dataclass
class AssemblyProgress:
    """Turns Flye's own log into a phase name and a "step N of M".

    Deliberately no percentage. Flye's stages differ in duration by more than
    an order of magnitude -- polishing alone can outlast everything before it
    -- so a bar derived from stage position would sit at one value for most
    of a run and then jump. A step counter makes no duration claim, which is
    why it is safe here where a bar is not.

    `stage_order` comes from `flye_stage_order(params)` and defaults to empty,
    in which case no phase structure is reported at all and the display falls
    back to the phase name alone.
    """

    name: str = "flye"
    phase: str = "starting"
    stage_order: tuple[str, ...] = ()
    # The raw Flye stage name behind `phase`. Kept separately because
    # `_STAGE_LABELS` is not injective by construction -- two stages sharing a
    # display label would otherwise both resolve to the first one's index.
    _stage: str | None = field(default=None, init=False, repr=False)

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
        self._stage = stage
        self.phase = phase
        return True

    @property
    def phase_index(self) -> int | None:
        """Position in `stage_order`, 1-based for "step N of M" display.

        None for a stage this run did not declare -- a future Flye stage
        borrowing the previous stage's number would be worse than no number.
        """
        if self._stage is None or self._stage not in self.stage_order:
            return None
        return self.stage_order.index(self._stage) + 1

    @property
    def phase_total(self) -> int | None:
        return len(self.stage_order) or None

    def message(self) -> str:
        return self.phase

    def snapshot(self) -> dict:
        # No pct: see the class docstring. phase_index/phase_total appear only
        # when a stage order was declared -- a parser omits keys it does not
        # know rather than passing None over a value ctx.progress() would
        # otherwise leave alone.
        snap = {"pct": None, "phase": self.phase, "message": self.message()}
        if self.stage_order:
            snap["phase_index"] = self.phase_index
            snap["phase_total"] = self.phase_total
        return snap


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
        # No N50 here: `parsers._contiguity_stats` computes `sequence_n50` from
        # the FASTA bytes on the same object, independent of which assembler
        # (or none) produced the file. Two N50s on one object that are
        # supposed to agree is a bug with a delay fuse, so this table keeps
        # only what it uniquely knows -- coverage and circularity, which no
        # FASTA parse can produce.
        #
        # Capped for the same reason `parsers.MAX_STORED_CONTIGS` is: a
        # fragmented draft has tens of thousands of rows and the facts
        # document is not where they belong.
        "assembly_contigs": sorted(contigs, key=lambda c: -c["length"])[:50],
        "assembly_contigs_truncated": len(contigs) > 50,
    }


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
