"""compleasm command construction and summary.txt parsing.

Same split `align_runner` and `assembly_runner` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.

The output shape was verified against a real 0.2.9 run on a small bacterial
FASTA rather than assumed from the source: `compleasm run` writes
`<outdir>/summary.txt` with a fixed six-line body --

    ## lineage: bacteria_odb12
    S:12.93%, 15
    D:0.00%, 0
    F:0.00%, 0
    I:0.00%, 0
    M:87.07%, 101
    N:116

-- and the per-lineage detail (full_table.tsv, hmmer_output/, etc.) under
`<outdir>/<lineage>_<odb>/`. There is no JSON output.

The `I:` (interspaced) line is always present -- reading the source in
isolation suggested it was dead code, since the `analyze` subcommand's own
copy of this block has it commented out, but the `run` subcommand actually
used here writes it unconditionally. Caught only by running the real,
installed 0.2.9 package rather than trusting the grep.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

log = None  # set lazily below to avoid an import cycle at module load time


def _log():
    global log
    if log is None:
        from app.logging import get_logger

        log = get_logger(__name__)
    return log


@dataclass(frozen=True)
class CompletenessParams:
    threads: int = 8
    # Bare organism-group name, e.g. "bacteria" or "eukaryota" -- never
    # suffixed with an OrthoDB version. compleasm's own download_lineage
    # rewrites whatever suffix is present to match its --odb argument
    # (`"{}_{}".format(lineage.split("_")[0], odb)`), so a caller-supplied
    # "_odb10" is silently discarded and replaced. Verified against a real
    # run on 2026-08-02: requesting "bacteria_odb10" with the default --odb
    # actually downloaded and scored bacteria_odb12. Keeping this bare avoids
    # writing a lineage name that looks meaningful and is not honoured.
    lineage: str = "bacteria"
    odb: str = "odb12"


def build_completeness_command(
    *,
    compleasm_path: str,
    assembly: Path,
    out_dir: Path,
    library_path: Path,
    params: CompletenessParams,
) -> list[str]:
    """The argv for one compleasm run.

    `run`, not the split `miniprot` + `analyze` subcommands: this application
    has no use for the intermediate alignment on its own, and one subcommand
    is one failure point to report instead of two.
    """
    return [
        compleasm_path,
        "run",
        "-a",
        str(assembly),
        "-o",
        str(out_dir),
        "-l",
        params.lineage,
        "--odb",
        params.odb,
        "--library_path",
        str(library_path),
        "-t",
        str(params.threads),
    ]


def build_download_command(
    *, compleasm_path: str, lineage: str, odb: str, library_path: Path
) -> list[str]:
    """The argv for fetching one lineage dataset.

    Also downloads compleasm's placement-file set on first use regardless of
    which lineage is requested -- verified on 2026-08-02, this is
    `Downloader.__init__`'s own behaviour (`download_placement=True` by
    default) and not something a flag here can skip. Roughly 100MB, paid
    once per `library_path`, not per lineage.
    """
    return [
        compleasm_path,
        "download",
        lineage,
        "--odb",
        odb,
        "--library_path",
        str(library_path),
    ]


# The four categories BioFlow stores. Order matches summary.txt.
#
# Verified against the real, installed 0.2.9 `run` subcommand on 2026-08-02,
# not the source read alone: it always writes an `I:` (interspaced) line
# unconditionally, and an `R:` (retrocopy) line when `--retrocopy` is passed.
# BioFlow never passes --retrocopy, so R never appears in practice, but the
# character class tolerates both -- a category this parser does not store is
# a smaller problem than a summary it refuses to parse because of one line it
# did not expect.
_SUMMARY_LINE_RE = re.compile(r"^([SDFIMR]):\s*([\d.]+)%,\s*(\d+)\s*$")
_LINEAGE_RE = re.compile(r"^##\s*lineage:\s*(\S+)\s*$")
_TOTAL_RE = re.compile(r"^N:\s*(\d+)\s*$")

_CATEGORY_NAMES = {
    "S": "single",
    "D": "duplicated",
    "F": "fragmented",
    "M": "missing",
}

# compleasm's own stderr is mostly tool-discovery chatter; the batched work is
# entirely miniprot's, which shares minimap2's logging library and its
# per-batch line:
#   [M::worker_pipeline::50.792*7.96] mapped 2257 sequences
# Same regex, same per-batch-not-cumulative semantics as align_runner's
# _MAPPED_RE (#57) -- both come from mm_analysis.c's worker_pipeline printf.
# Verified against a real captured run of this exact pipeline
# (`assess_completeness`) rather than assumed: compleasm never prints a
# closed phase list the way fastp or bwa-mem2's aligning/sorting split does,
# so there is nothing to name "step N of M" -- this is one phase
# ("mapping") with a running count, then a silent hmmsearch stage with no
# stderr of its own to parse. hmmsearch is only ever a phase transition, not
# a countable one, for the same reason variant calling's full-alignment
# phase is: it exists to be named, not measured.
_MAPPED_RE = re.compile(r"worker_pipeline.*mapped\s+(\d+)\s+sequences", re.IGNORECASE)
_HMMSEARCH_RE = re.compile(r"hmmsearch execute command", re.IGNORECASE)


@dataclass
class CompletenessProgress:
    """Turns compleasm's own output into a phase and a running mapped-
    sequence count.

    No expected_reads, unlike TrimProgress/AlignProgress -- compleasm's
    ortholog set size is known ahead of time (the lineage's marker count) but
    is not the same unit as the sequences miniprot maps, so there is no
    honest total to divide by. The count is shown as a running number, not a
    fraction, for the same reason assembly_runner's Flye phases show no pct:
    a number that cannot be verified against a total must not claim one.
    """

    name: str = "compleasm"
    phase: str = "mapping"
    mapped: int = 0
    _seen_hmmsearch: bool = field(default=False, repr=False)

    def feed(self, line: str) -> bool:
        """Consume a line. True if the caller should publish an update."""
        if _HMMSEARCH_RE.search(line):
            if self._seen_hmmsearch:
                return False
            self._seen_hmmsearch = True
            self.phase = "scoring"
            return True

        match = _MAPPED_RE.search(line)
        if not match:
            return False

        # Cumulative, not a running total: miniprot reports per batch, same
        # as bwa-mem2 and minimap2 (align_runner.AlignProgress).
        self.mapped += int(match.group(1))
        return True

    @property
    def pct(self) -> float | None:
        """Always None -- see the class docstring for why there is no honest
        total to divide the mapped count by."""
        return None

    def message(self) -> str:
        if self.phase == "scoring":
            return "scoring completeness"
        if self.mapped:
            return f"mapping proteins: {self.mapped:,} sequences"
        return "mapping proteins"

    def snapshot(self) -> dict:
        return {"pct": self.pct, "phase": self.phase, "message": self.message()}


def parse_summary(text: str) -> dict:
    """`assembly_completeness_*` facts from compleasm's `summary.txt`.

    Returns {} for anything unparseable rather than raising -- the same
    posture `assembly_runner.parse_assembly_info` takes, for the same
    reason: a summary that failed to parse must not fail a job that spent
    possibly hours running miniprot and hmmsearch over the whole assembly.

    Namespace is `assembly_completeness_*`, not `busco_*`: `busco_score`
    already means something else on this application's objects -- the
    completeness of a *proteome* UniProt computed, not of an assembly BioFlow
    measured (see app/metadata/uniprot.py). An object can carry both.
    """
    lineage: str | None = None
    counts: dict[str, tuple[float, int]] = {}
    total: int | None = None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LINEAGE_RE.match(line)
        if m:
            lineage = m.group(1)
            continue
        m = _TOTAL_RE.match(line)
        if m:
            total = int(m.group(1))
            continue
        m = _SUMMARY_LINE_RE.match(line)
        if m:
            code, pct, count = m.group(1), float(m.group(2)), int(m.group(3))
            counts[code] = (pct, count)

    # Missing entirely, not just incomplete: a summary with no S/D/F/M lines
    # at all is not compleasm's format, whatever produced this text.
    if not counts or total is None:
        _log().warning("completeness_summary_unparseable", head=text[:200])
        return {}

    facts: dict = {
        "assembly_completeness_tool": "compleasm",
        "assembly_completeness_total": total,
    }
    if lineage is not None:
        facts["assembly_completeness_lineage"] = lineage
    for code, name in _CATEGORY_NAMES.items():
        if code in counts:
            facts[f"assembly_completeness_{name}_pct"] = counts[code][0]
    single_pct = counts.get("S", (0.0, 0))[0]
    duplicated_pct = counts.get("D", (0.0, 0))[0]
    if "S" in counts or "D" in counts:
        facts["assembly_completeness_complete_pct"] = round(
            single_pct + duplicated_pct, 2
        )
    return facts
