"""MetaBAT2 command-building, depth parsing, and bin enumeration.

A pure module, on the mosdepth_runner.py model: no subprocess calls live here,
so every function is unit-testable without the binary installed. The handler
in app/queue/binning_handlers.py supplies the process.

Binning is the one stage of this app's pipeline that turns **one artifact into
N**, where N is data-dependent -- so most of what this module exists to do is
describe that N precisely enough for the applier to ingest it safely.

Output shapes are pinned to a real MetaBAT2 2.18 run captured on 2026-08-20
against a synthetic two-organism community (AT-rich/high-depth vs
GC-rich/low-depth, the two signals MetaBAT2 separates on); see
tests/pipelines/test_metabat_runner.py for the fixtures. Three things that run
revealed, none of them guessable from the docs:

  * bins are `<prefix>.<N>.fa`, 1-indexed, and the SAME directory also gets
    `<prefix>.BinInfo.txt`, `<prefix>.BinMembers.txt`, `<prefix>.unbinned.fa`,
    `<prefix>.tooShort.fa` and `<prefix>.lowDepth.fa`. Globbing `<prefix>.*.fa`
    therefore sweeps up three non-bins; `_BIN_FILE` matches the integer only.
  * `--unbinned` is NOT the default. Without it MetaBAT2 silently drops the
    contigs it could not place, which is exactly what #728 refuses to do.
  * `BinInfo.txt` already carries each bin's contig count, total length and
    length-weighted average coverage -- so the per-bin facts come from one
    small TSV rather than from re-reading every bin FASTA.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# `<prefix>.<N>.fa` with N an integer, and nothing else. Anchored on both ends
# so `bin.unbinned.fa`, `bin.tooShort.fa`, `bin.lowDepth.fa` and
# `bin.BinInfo.txt` -- all real files MetaBAT2 writes into the same directory
# -- cannot be mistaken for bins. A `.*.fa` glob would have ingested three
# non-genomes as MAGs.
_BIN_FILE = re.compile(r"^(?P<prefix>.+)\.(?P<index>\d+)\.fa$")


@dataclass(frozen=True)
class ContigDepth:
    """One row of `jgi_summarize_bam_contig_depths --outputDepth`."""

    contig: str
    length: int
    mean_depth: float


@dataclass(frozen=True)
class Bin:
    """One MAG MetaBAT2 emitted, as the applier needs to see it."""

    index: int
    path: Path
    contig_count: int
    total_bases: int
    mean_depth: float | None


def build_depths_command(
    *,
    bam: Path,
    output: Path,
    jgi_depths: str = "jgi_summarize_bam_contig_depths",
) -> list[str]:
    """Build `jgi_summarize_bam_contig_depths --outputDepth <out> <bam>`.

    This is MetaBAT2's own depth summarizer, and using it is a decision rather
    than a convenience (design doc B1). It reports per-contig depth **variance**
    alongside the mean, and MetaBAT2 bins on coverage co-variance as well as
    tetranucleotide composition.

    The tempting shortcut is mosdepth, which this app already runs (#626) and
    which already produces per-contig mean depth. Feeding MetaBAT2 a file built
    from those means yields a file it **accepts and bins from** -- with worse
    bins, no error, and nothing anywhere to say the result is degraded. That is
    the silent-degradation shape this repo keeps naming, so the depth step runs
    here even though a depth number already exists elsewhere.
    """
    return [
        jgi_depths,
        "--outputDepth",
        str(output),
        str(bam),
    ]


def build_binning_command(
    *,
    contigs: Path,
    depths: Path,
    out_prefix: Path,
    min_contig: int = 2500,
    threads: int = 1,
    seed: int = 1,
    metabat2: str = "metabat2",
) -> list[str]:
    """Build the `metabat2` command.

    `out_prefix` is a *base name*, not a directory: MetaBAT2 writes
    `<out_prefix>.1.fa`, `<out_prefix>.2.fa`, and so on beside it.

    `--unbinned` is passed always, never conditionally. Without it the contigs
    MetaBAT2 could not place are simply not written anywhere, and the fraction
    of a community that failed to resolve is frequently the most interesting
    thing about it (design doc B3).

    `--seed` defaults to 1 rather than MetaBAT2's own 0. Zero means "use a
    random seed", which makes a re-run of the same job produce different MAGs
    from the same inputs -- and this app's job dedup, provenance and
    re-run-to-compare workflows all assume a job is a function of its inputs.
    """
    if min_contig < 1500:
        # MetaBAT2's own floor. Below it the tetranucleotide signal is too
        # noisy to bin on, and MetaBAT2 exits with a usage error -- caught here
        # so a bad parameter fails at launch rather than after the depth step.
        raise ValueError(
            f"min_contig must be >= 1500 (MetaBAT2's floor); got {min_contig}"
        )
    return [
        metabat2,
        "-i",
        str(contigs),
        "-a",
        str(depths),
        "-o",
        str(out_prefix),
        "-m",
        str(min_contig),
        "-t",
        str(threads),
        "--seed",
        str(seed),
        # See the docstring: the unbinned fraction is a result, not a discard.
        "--unbinned",
    ]


def parse_depths(path: Path) -> list[ContigDepth]:
    """Parse `jgi_summarize_bam_contig_depths` output.

    The format, from a real 2.18 run:

        contigName  contigLen  totalAvgDepth  aln.bam  aln.bam-var
        orgA_ctg0   200000     30.013         30.013   30.5048

    Columns 4+ are per-BAM, two per BAM (depth and variance), so their count
    and their names both depend on the input -- only the first three columns
    are positionally stable, and those are the three read here. The variance
    matters enormously to MetaBAT2 and not at all to this parser: it is
    consumed by the binner from the file itself, never through this function.
    """
    rows: list[ContigDepth] = []
    text = path.read_text()
    for lineno, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        fields = line.split("\t")
        if lineno == 0 and fields[0] == "contigName":
            continue
        if len(fields) < 3:
            log.warning("depth_row_short", line=lineno, fields=len(fields))
            continue
        try:
            rows.append(
                ContigDepth(
                    contig=fields[0],
                    length=int(fields[1]),
                    mean_depth=float(fields[2]),
                )
            )
        except ValueError:
            # One malformed row is not a reason to lose the file: the depth
            # table can be tens of thousands of contigs long, and MetaBAT2 has
            # already binned from the file itself by the time this runs.
            log.warning("depth_row_unparseable", line=lineno)
    return rows


def parse_bin_info(path: Path) -> dict[int, tuple[int, int, float]]:
    """Parse `<prefix>.BinInfo.txt` into `{bin_index: (contigs, bases, depth)}`.

    Format, from a real 2.18 run:

        BinNum  NumContigs  TotalLength  LengthWeightedAvgCoveage  FileName
        1       6           1200000      8.00292                   bins/bin.1.fa

    (`Coveage` is MetaBAT2's own spelling, not a typo here.)

    Read by column position rather than by header name for exactly that
    reason -- a header this file spells wrong is a header a later release may
    spell right, and a name lookup would start returning nothing the day it is
    corrected.

    Returns an empty mapping when the file is absent. That is not an error:
    the facts it supplies are enrichment, and `enumerate_bins` counts contigs
    and bases from the FASTAs themselves when this file cannot supply them.
    """
    if not path.exists():
        return {}
    info: dict[int, tuple[int, int, float]] = {}
    for lineno, line in enumerate(path.read_text().splitlines()):
        if not line.strip():
            continue
        fields = line.split("\t")
        if lineno == 0 and fields[0].startswith("BinNum"):
            continue
        if len(fields) < 4:
            continue
        try:
            info[int(fields[0])] = (int(fields[1]), int(fields[2]), float(fields[3]))
        except ValueError:
            log.warning("bin_info_row_unparseable", line=lineno)
    return info


def measure_fasta(path: Path) -> tuple[int, int]:
    """`(contig_count, total_bases)` for a FASTA, counting sequence only.

    Streamed line by line rather than read whole: a bin is a draft genome and
    can be hundreds of megabytes, and the applier measures every bin.

    Whitespace is stripped per line before counting, so wrapped FASTA (which
    MetaBAT2 emits) does not inflate the base count by one per line.
    """
    contigs = 0
    bases = 0
    with path.open() as fh:
        for line in fh:
            if line.startswith(">"):
                contigs += 1
            else:
                bases += len(line.strip())
    return contigs, bases


def enumerate_bins(out_prefix: Path) -> list[Bin]:
    """Every `<out_prefix>.<N>.fa` MetaBAT2 wrote, ordered by N.

    Ordered numerically, not lexically: `sorted()` over the filenames puts
    bin.10 before bin.2, which would make `bin_index` disagree with the order
    the user sees and make a truncation (were one ever added) drop an
    arbitrary set.

    Empty and unreadable bins are skipped with a warning rather than raising.
    MetaBAT2 does not normally emit an empty bin, so one here means something
    is wrong with this bin specifically -- and losing the other thirty-nine to
    it is the failure mode #728's R3 exists to prevent.
    """
    directory = out_prefix.parent
    base = out_prefix.name
    if not directory.is_dir():
        return []

    info = parse_bin_info(directory / f"{base}.BinInfo.txt")

    bins: list[Bin] = []
    for candidate in directory.iterdir():
        match = _BIN_FILE.match(candidate.name)
        if not match or match.group("prefix") != base:
            continue
        index = int(match.group("index"))
        try:
            if candidate.stat().st_size == 0:
                log.warning("bin_empty", bin_index=index, path=str(candidate))
                continue
            recorded = info.get(index)
            if recorded is not None:
                contig_count, total_bases, mean_depth = recorded
            else:
                contig_count, total_bases = measure_fasta(candidate)
                mean_depth = None
            if contig_count == 0 or total_bases == 0:
                log.warning("bin_no_sequence", bin_index=index, path=str(candidate))
                continue
        except OSError as e:
            log.warning("bin_unreadable", bin_index=index, error=str(e))
            continue
        bins.append(
            Bin(
                index=index,
                path=candidate,
                contig_count=contig_count,
                total_bases=total_bases,
                mean_depth=mean_depth,
            )
        )
    bins.sort(key=lambda b: b.index)
    return bins


def unbinned_path(out_prefix: Path) -> Path | None:
    """The `<prefix>.unbinned.fa` MetaBAT2 wrote, if it holds anything.

    `None` covers two different-looking cases that mean the same thing to a
    caller: the file is absent, or it is present and empty. MetaBAT2 writes a
    zero-byte `unbinned.fa` when every contig was placed, and ingesting an
    empty FASTA as an object would put an unopenable, meaningless "unbinned
    contigs" object in the user's project.
    """
    candidate = out_prefix.parent / f"{out_prefix.name}.unbinned.fa"
    try:
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    except OSError:
        return None
    return None


def excluded_paths(out_prefix: Path) -> dict[str, Path]:
    """The `tooShort` / `lowDepth` FASTAs, when non-empty.

    MetaBAT2 always writes both, usually empty. They are **not** the same thing
    as unbinned contigs and are deliberately not merged with them: a contig in
    `tooShort` was never eligible for binning (below `--minContig`), and one in
    `lowDepth` had too little coverage to place. Reporting either as "MetaBAT2
    could not resolve this" would overstate how diverse the community is, when
    the honest answer is that the assembly or the sequencing depth was the
    limit.

    Counted into facts, not ingested as objects: unlike the unbinned fraction
    there is nothing useful to do downstream with contigs excluded for being
    short or shallow.
    """
    found: dict[str, Path] = {}
    for label in ("tooShort", "lowDepth"):
        candidate = out_prefix.parent / f"{out_prefix.name}.{label}.fa"
        try:
            if candidate.exists() and candidate.stat().st_size > 0:
                found[label] = candidate
        except OSError:
            continue
    return found


def check_bin_cap(bin_count: int, cap: int) -> None:
    """Raise when a run produced more bins than one job may ingest.

    Refuses; never truncates (design doc B4). Ingesting the first `cap` bins
    would discard MAGs ordered by MetaBAT2's numbering rather than by quality,
    so the dropped set is arbitrary AND invisible -- the user would have no way
    to learn which genomes went missing, or that any did.

    A refusal naming both numbers is actionable: raise `--minContig` to bin
    fewer, larger contigs, or raise `metagenome_bin_cap`.

    Raises `ValueError` rather than `PermanentError` to keep this module free
    of queue imports; the handler translates it.
    """
    if bin_count > cap:
        raise ValueError(
            f"MetaBAT2 produced {bin_count} bins, more than the {cap} this "
            f"instance will ingest from one run. Nothing was ingested. Raise "
            f"the minimum contig length to bin fewer, larger contigs, or raise "
            f"metagenome_bin_cap."
        )


def bin_facts(
    *,
    bin_: Bin,
    source_assembly_id: str,
    total_bins: int,
) -> dict:
    """What one MAG records about where it came from.

    A bin has no container object -- it is a `DataObject` beside its siblings
    (design doc B2/M3) -- so these facts are the only thing tying it back to
    the community it was separated out of. `bin_source_assembly` in particular
    is what makes "which assembly is this a MAG of" answerable at all.
    """
    facts = {
        "bin_index": bin_.index,
        "bin_source_assembly": source_assembly_id,
        "bin_contig_count": bin_.contig_count,
        "bin_total_bases": bin_.total_bases,
        # How many siblings it has. On the bin rather than only on the
        # assembly, so a MAG opened on its own still says how finely the
        # community it came from was split.
        "bin_total_bins": total_bins,
    }
    if bin_.mean_depth is not None:
        facts["bin_mean_depth"] = round(bin_.mean_depth, 4)
    return facts


def binning_facts(
    *,
    bins: list[Bin],
    unbinned_bases: int,
    excluded: dict[str, int],
    tool_version: str | None,
) -> dict:
    """What the *source assembly* records about having been binned.

    The split is the point. A user looking at a community assembly needs to
    know what fraction of it resolved into genomes without opening N objects
    and adding up their sizes -- and the unbinned fraction is what says whether
    the answer is "this community is well resolved" or "most of this sample is
    organisms nothing here can separate".
    """
    binned_bases = sum(b.total_bases for b in bins)
    excluded_bases = sum(excluded.values())
    considered = binned_bases + unbinned_bases
    facts: dict = {
        "binning_bin_count": len(bins),
        "binning_binned_bases": binned_bases,
        "binning_unbinned_bases": unbinned_bases,
    }
    if considered:
        # Of what was eligible for binning -- excluded contigs are left out of
        # the denominator deliberately, since a contig below --minContig was
        # never a candidate and counting it as "not recovered" would report a
        # worse community than the data shows.
        facts["binning_binned_pct"] = round(100.0 * binned_bases / considered, 2)
    if excluded_bases:
        facts["binning_excluded_bases"] = excluded_bases
        for label, bases in excluded.items():
            facts[f"binning_{label.lower()}_bases"] = bases
    if tool_version:
        facts["binned_by"] = "metabat2"
        facts["binner_version"] = tool_version
    return facts
