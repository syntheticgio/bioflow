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
from app.pipelines.assembly_params import (
    AbyssParams,
    BaseAssemblyParams,
    FlyeParams,
    MegahitParams,
    SpadesParams,
)

log = get_logger(__name__)

# ABySS refuses to start without a Bloom filter budget, so a run with no memory
# estimate still needs a number. 200M is small enough to be safe anywhere and
# large enough to assemble a bacterial genome.
MIN_BLOOM_MB = 200

# SPAdes' `-m` is a hard ceiling in gigabytes: it terminates on reaching it,
# and its own default is 250GB. A run with no estimate must still get a real
# number, or it inherits that default and dies deep into a run on a
# workstation rather than never starting.
MIN_SPADES_MEMORY_GB = 4

# MEGAHIT's `-m` is NOT SPAdes' `-m`, in either units or meaning, and both
# differences are silent when got wrong:
#
#   * Units. `-m/--memory <float>` is "max memory in byte", and a value
#     between 0 and 1 is read as a *fraction of the machine's total memory*
#     instead. So `-m 4` -- the shape MIN_SPADES_MEMORY_GB produces -- is a
#     legal argument meaning four bytes, not four gigabytes.
#   * Meaning. It is a budget MEGAHIT plans its graph construction to fit,
#     not a ceiling it terminates on reaching. That difference is why #731
#     measured MEGAHIT completing under a cap that killed metaSPAdes, which
#     is the entire reason this assembler is here.
#
# Floored at 2 GiB so a run with no estimate still gets a real budget, and
# expressed in bytes so the number the guard used and the number the tool
# gets are the same number -- the argument `_abyss_command`'s Bloom budget
# makes. The 0-1 fraction form is deliberately never emitted: it would make a
# run's memory depend on the host rather than on the estimate that admitted
# it, so two runs with identical recorded parameters would behave differently
# on different machines.
MIN_MEGAHIT_MEMORY_BYTES = 2 * 1024**3


def build_assembly_command(
    *,
    assembler: Assembler,
    tool_path: str,
    reads: Path,
    out_dir: Path,
    params: BaseAssemblyParams,
    mate: Path | None = None,
    memory_bytes: int | None = None,
) -> list[str]:
    """The argv for one assembly run.

    `mate` and `memory_bytes` are read by ABySS (as a Bloom filter budget) and
    SPAdes (as a memory ceiling) and ignored by the Flye builder -- a paired
    long-read assembly is not a thing, and Flye needs no memory ceiling to start.
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
            memory_bytes=memory_bytes,
        )
    if assembler is Assembler.SPADES:
        assert isinstance(params, SpadesParams)
        return _spades_command(
            tool_path=tool_path,
            reads=reads,
            out_dir=out_dir,
            params=params,
            mate=mate,
            memory_bytes=memory_bytes,
        )
    if assembler is Assembler.MEGAHIT:
        assert isinstance(params, MegahitParams)
        return _megahit_command(
            tool_path=tool_path,
            reads=reads,
            out_dir=out_dir,
            params=params,
            mate=mate,
            memory_bytes=memory_bytes,
        )
    # Not a fallback: an assembler with no builder here would otherwise
    # produce another tool's command line for this binary.
    raise ValueError(f"No command builder for {assembler.value}")


def _flye_command(
    *, tool_path: str, reads: Path, out_dir: Path, params: FlyeParams
) -> list[str]:
    cmd = [
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
    if params.meta:
        cmd.append("--meta")
    return cmd


def _abyss_command(
    *,
    tool_path: str,
    reads: Path,
    out_dir: Path,
    params: AbyssParams,
    mate: Path | None,
    memory_bytes: int | None,
) -> list[str]:
    """`abyss-pe` takes Make variable assignments, not flags.

    `-C <dir>` is make's own change-directory option and is how the outputs
    land in `out_dir` -- ABySS has no `--out-dir` equivalent and would
    otherwise write into the process's cwd.
    """
    bloom_mb = MIN_BLOOM_MB
    if memory_bytes:
        bloom_mb = max(MIN_BLOOM_MB, int(memory_bytes / (1024 * 1024)))

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


def _spades_command(
    *,
    tool_path: str,
    reads: Path,
    out_dir: Path,
    params: SpadesParams,
    mate: Path | None,
    memory_bytes: int | None,
) -> list[str]:
    """`spades.py` takes conventional flags, unlike abyss-pe's Make variables.

    `-m` is the one that matters: it is a ceiling SPAdes enforces by
    terminating, not a hint, so it is always passed and always floored.
    """
    memory_gb = MIN_SPADES_MEMORY_GB
    if memory_bytes:
        memory_gb = max(MIN_SPADES_MEMORY_GB, int(memory_bytes / (1024**3)))

    cmd = [tool_path, "-o", str(out_dir), "-t", str(params.threads), "-m", str(memory_gb)]

    # `standard` is BioFlow's name for neither flag; SPAdes has no such option.
    # These stay mutually exclusive because SPAdes rejects the combinations:
    # `--meta` with either `--isolate` or `--careful` is an error, and emitting
    # both would fail minutes in, after read error correction.
    if params.mode == "isolate":
        cmd.append("--isolate")
    elif params.mode == "careful":
        cmd.append("--careful")
    elif params.mode == "meta":
        cmd.append("--meta")

    if mate is not None:
        cmd += ["-1", str(reads), "-2", str(mate)]
    else:
        cmd += ["-s", str(reads)]
    return cmd


def _megahit_command(
    *,
    tool_path: str,
    reads: Path,
    out_dir: Path,
    params: MegahitParams,
    mate: Path | None,
    memory_bytes: int | None,
) -> list[str]:
    """`megahit` takes conventional flags, like spades.py -- but `-o` and `-m`
    both behave differently enough to be worth stating.

    **`--force` is load-bearing, not a convenience.** MEGAHIT refuses to start
    when its output directory already exists:

        if not opt.force_overwrite and not opt.test_mode
                and os.path.exists(opt.out_dir):
            raise Usage('Output directory ... already exists, ...')

    (v1.2.9's own wrapper). `assembly_handlers` creates `out_dir` before
    building any command -- which Flye, ABySS and SPAdes all accept -- so
    without `--force` every MEGAHIT run here fails instantly, before
    assembling anything. Overwriting is safe because that directory is a
    per-job scratch dir the handler just made empty; there is nothing of
    anyone's in it. The alternative, making the handler skip its mkdir for
    this one assembler, puts a tool-specific conditional in a path every
    assembler shares -- which is how the *next* assembler breaks silently.

    `-m` is in **bytes**, not gigabytes, and is a budget rather than a
    ceiling. See MIN_MEGAHIT_MEMORY_BYTES.
    """
    memory = max(MIN_MEGAHIT_MEMORY_BYTES, int(memory_bytes or 0))

    cmd = [
        tool_path,
        "-o",
        str(out_dir),
        "--force",
        "-t",
        str(params.threads),
        # Formatted as an integer, never in scientific notation: `1e+10` is
        # not a float MEGAHIT's own `float()` parse would reject, but a value
        # that rounded into [0, 1] would silently become a *fraction of host
        # memory*. The floor above is what actually prevents that; this keeps
        # the recorded command readable as the byte count it is.
        "-m",
        str(memory),
        "--min-contig-len",
        str(params.min_contig_len),
    ]

    if mate is not None:
        cmd += ["-1", str(reads), "-2", str(mate)]
    else:
        # MEGAHIT assembles single-end input fine, unlike metaSPAdes -- so
        # there is no launch-time refusal for unpaired reads here.
        cmd += ["-r", str(reads)]
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


# ABySS writes its own stats table, which Flye does not:
#   n  n:500  L50  min  N75  N50  N25  E-size  max  sum  name
# One row per output stage; the `-scaffolds.fa` row is the finished assembly.
_ABYSS_STATS_ROW = f"{assembler_registry.ASSEMBLY_NAME_PREFIX}-scaffolds.fa"


def parse_abyss_stats(text: str) -> dict:
    """Contiguity facts from ABySS's own stats table.

    Unlike Flye's table, this one already contains N50 -- so unlike
    `parse_assembly_info`, this parser does report it. Note the asymmetry is
    deliberate: `parsers._contiguity_stats` computes `sequence_n50` from the
    FASTA bytes independently, so these two numbers are computed from the same
    sequences by different code and must agree. If they ever disagree, the
    FASTA-derived one is authoritative.

    Returns {} for anything malformed rather than raising, for the same reason
    `parse_assembly_info` does: a table that could not be read must not fail an
    assembly that produced a perfectly good FASTA.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return {}

    header = [h.strip() for h in lines[0].split("\t")]
    index = {name: i for i, name in enumerate(header)}
    required = ("n", "N50", "max", "sum", "name")
    if not all(col in index for col in required):
        log.warning("abyss_stats_unexpected_header", header=header[:11])
        return {}

    for line in lines[1:]:
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) < len(header):
            continue
        if cols[index["name"]] != _ABYSS_STATS_ROW:
            continue
        try:
            return {
                "assembly_contig_count": int(cols[index["n"]]),
                "assembly_total_length": int(cols[index["sum"]]),
                "assembly_n50": int(cols[index["N50"]]),
                "assembly_longest": int(cols[index["max"]]),
            }
        except (ValueError, IndexError):
            return {}
    return {}


def gfa_to_fasta(text: str) -> str:
    """hifiasm's primary contigs, as FASTA.

    hifiasm writes no FASTA at all -- its contigs are GFA `S` lines
    (`S <name> <seq> [tags...]`, layout confirmed against a real 0.25.0
    run). Everything downstream of assembly consumes FASTA, so this is
    the bridge, called by HIFIASM_SPEC's postprocess hook before
    `harvest()` looks for assembly.fasta.

    Raises rather than returning an empty string when there are no `S`
    records: an exit-0 run that assembled nothing must surface as the
    missing-contigs failure `harvest()` knows how to report, not as a
    valid, empty REFERENCE object that every later align silently
    accepts.
    """
    records: list[str] = []
    for line in text.splitlines():
        if not line.startswith("S\t"):
            continue
        fields = line.split("\t")
        if len(fields) < 3 or not fields[2]:
            continue
        records.append(f">{fields[1]}\n{fields[2]}\n")
    if not records:
        raise ValueError(
            "The assembly graph contains no sequences. The assembler "
            "exited successfully but produced no contigs."
        )
    return "".join(records)


# ABySS runs as a Make pipeline whose recipes echo the binary they invoke.
# There is no `>>>STAGE:` equivalent and no count knowable in advance, so this
# reports a phase name and no step counter -- which the snapshot contract
# already handles by omitting phase_index/phase_total.
_ABYSS_PHASES: tuple[tuple[str, str], ...] = (
    ("ABYSS-P", "assembling contigs"),
    ("ABySS-P", "assembling contigs"),
    ("abyss-map", "mapping reads"),
    ("abyss-fixmate", "pairing alignments"),
    ("DistanceEst", "estimating distances"),
    ("abyss-scaffold", "scaffolding"),
    ("abyss-fac", "computing statistics"),
)


@dataclass
class AbyssProgress:
    """Phase names from ABySS's Make output.

    No percentage and no step counter, for the reason `AssemblyProgress`'s
    docstring gives and one more: ABySS's stage list is not knowable before the
    run, so a denominator would be invented.
    """

    name: str = "abyss"
    phase: str = "starting"

    def feed(self, line: str) -> bool:
        """Consume a log line. True if the phase changed."""
        for token, label in _ABYSS_PHASES:
            if token in line:
                if label == self.phase:
                    return False
                self.phase = label
                return True
        return False

    def message(self) -> str:
        return self.phase

    def snapshot(self) -> dict:
        return {"pct": None, "phase": self.phase, "message": self.message()}


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
            # `.resolve()` because ABySS's outputs are symlinks over numbered
            # stage files (`asm-scaffolds.fa` -> `asm-8.fa`). Storing the link
            # would dangle as soon as the workdir is reaped. Harmless for Flye,
            # whose outputs are already real files.
            found[output.kind] = path.resolve()
        elif output.required:
            raise FileNotFoundError(
                f"{output.filename} is missing from the assembly output. The "
                f"assembler exited successfully but produced no contigs."
            )
    return found
