"""Polypolish command construction and output parsing.

Same split `ivar_runner` and `completeness_runner` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.

Three shapes here are load-bearing and easy to "fix" into something wrong.
All three come from Polypolish's own documentation, and the design
(`docs/superpowers/specs/2026-08-05-polypolish-design.md`) explains why each
one matters:

- **`-a` on every alignment.** Polypolish's entire advantage over Pilon is
  that it sees every location a read maps to and declines to change a
  position where those locations disagree. Without `-a` the aligner reports
  best alignments only and Polypolish silently degrades into the tool it
  replaced -- no error, a plausible output, a worse polish in exactly the
  repetitive regions a long-read assembly was chosen for.
- **R1 and R2 are aligned separately.** Two independent invocations against
  one file each, not one paired invocation. This looks like a bug to anyone
  used to paired alignment and is not one: `polypolish filter` is what
  reunites the pairs and applies insert-size logic, and aligning them
  together is what defeats `-a`.
- **The SAMs stay raw.** Name-ordered SAM is what Polypolish reads. The
  reflex `ivar_runner` needed -- pipe it through `samtools sort` -- breaks
  this tool instead of feeding it.
"""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

# Polypolish's own guidance: below this depth it recommends `--careful`,
# which discards multi-mapping reads. That trades away repeat correction --
# the thing Polypolish is uniquely good at -- to avoid introducing errors
# where there is too little evidence to be sure. Both directions are
# defensible; the threshold is upstream's, not ours.
CAREFUL_DEPTH_THRESHOLD = 25.0


@dataclass(frozen=True)
class PolishParams:
    # Estimated mean depth of the short reads over the draft. None when it
    # could not be computed -- see `estimate_depth`.
    depth: float | None = None
    careful: bool = False


def estimate_depth(*, read_bases: int | None, assembly_length: int | None) -> float | None:
    """Mean short-read depth over the draft, or None when unknowable.

    Deliberately returns None rather than a guess when either input is
    missing. The depth decides whether `--careful` engages, which changes
    what the tool does to the user's assembly; defaulting a missing input to
    a number would make that choice on evidence nobody has.
    """
    if not read_bases or not assembly_length:
        return None
    return read_bases / assembly_length


def params_for_depth(depth: float | None) -> PolishParams:
    """`--careful` below the threshold, and *not* when depth is unknown.

    Unknown depth takes the non-careful path on purpose. `--careful` is the
    conservative choice about *edits*, but it is not the conservative choice
    about *behaviour*: it silently stops correcting repeats, which is the
    capability the tool was chosen for. An unmeasurable depth should leave
    the tool doing its normal job, with the run recording that depth was
    unknown, rather than quietly switching modes on no evidence.
    """
    if depth is None:
        return PolishParams(depth=None, careful=False)
    return PolishParams(depth=depth, careful=depth <= CAREFUL_DEPTH_THRESHOLD)


def build_index_command(*, aligner_path: str, draft: Path) -> list[str]:
    """The argv for indexing the draft before aligning against it."""
    return [aligner_path, "index", str(draft)]


def build_align_command(
    *, aligner_path: str, draft: Path, reads: Path, threads: int = 1
) -> list[str]:
    """The argv for aligning ONE read file against the draft.

    `-a` is not optional and not configurable -- see the module docstring.
    Callers align each read file with its own invocation; there is no
    paired form of this function by design.

    Output goes to stdout; the handler redirects it, rather than this
    function embedding a shell redirect, so the argv stays directly
    executable and directly assertable in a test.
    """
    return [
        aligner_path,
        "mem",
        "-t",
        str(threads),
        "-a",
        str(draft),
        str(reads),
    ]


def build_filter_command(
    *,
    polypolish_path: str,
    sam_in: list[Path],
    sam_out: list[Path],
) -> list[str]:
    """The argv for `polypolish filter`.

    Upstream calls this step optional. It is not skipped here: it removes
    alignments inconsistent with the observed insert-size distribution, and
    omitting it is a quality regression with nothing gained. It only applies
    to paired reads -- with a single read file there are no inserts to
    reason about, which is why the handler skips it for single-end input
    rather than this builder growing a one-file mode.
    """
    if len(sam_in) != 2 or len(sam_out) != 2:
        raise ValueError("polypolish filter takes exactly two SAM files in and out")
    return [
        polypolish_path,
        "filter",
        "--in1",
        str(sam_in[0]),
        "--in2",
        str(sam_in[1]),
        "--out1",
        str(sam_out[0]),
        "--out2",
        str(sam_out[1]),
    ]


def build_polish_command(
    *,
    polypolish_path: str,
    draft: Path,
    sams: list[Path],
    params: PolishParams,
) -> list[str]:
    """The argv for `polypolish polish`.

    The polished FASTA goes to stdout, which the handler redirects. Same
    reasoning as `build_align_command`.
    """
    argv = [polypolish_path, "polish"]
    if params.careful:
        argv.append("--careful")
    argv.append(str(draft))
    argv.extend(str(s) for s in sams)
    return argv


def redirect_stdout(argv: list[str], out: Path) -> list[str]:
    """Wrap an argv so its stdout lands in `out`.

    Both the aligner and `polypolish polish` write their real output to
    stdout, and `run_subprocess` has no redirect parameter. Kept as a
    separate wrapper rather than folded into the builders so the builders
    stay directly executable and their argv stays directly assertable --
    the `-a` test in particular must inspect a real argv, not a shell
    string it would have to re-parse.

    Every element is shell-quoted because file names derive from
    user-supplied object names, the same reason `ivar_runner._quote` exists.
    """
    quoted = " ".join(shlex.quote(a) for a in argv)
    return ["/bin/sh", "-c", f"{quoted} > {shlex.quote(str(out))}"]


# Polypolish reports its tally to stderr, and it reports it **once per
# contig**, not once per run:
#
#     Polishing ctgA (10,000 bp):
#       mean read depth: 57.5x
#       11 positions changed (0.1100% of total positions)
#       estimated pre-polishing sequence accuracy: 99.8900% (Q29.59)
#     Polishing ctgB (10,000 bp):
#       ...
#       9 positions changed (0.0900% of total positions)
#
# Verified against a real 0.7.1 run on 2026-08-05 (synthetic 20kb draft, 20
# planted single-base errors, 60x synthetic Illumina pairs: all 20 corrected,
# zero remaining mismatches against the truth). The two-contig split of that
# same draft reported 11 and 9 -- so a parser taking the first match would
# have reported 11 changes for a run that made 20, and nothing would have
# said so. These are summed across blocks for that reason.
#
# Numbers carry thousands separators (`4,000`), and the surrounding headings
# carry ANSI colour codes, so both are handled rather than assumed away.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CONTIG_RE = re.compile(r"^Polishing\s+(\S+)\s+\(([\d,]+)\s*bp\):", re.MULTILINE)
_CHANGED_RE = re.compile(r"^\s*([\d,]+)\s+positions? changed", re.MULTILINE)
_DEPTH_RE = re.compile(r"^\s*mean read depth:\s*([\d.]+)x", re.MULTILINE)
_ZERO_DEPTH_RE = re.compile(r"^\s*([\d,]+)\s*bp have a depth of zero", re.MULTILINE)


def _int(raw: str) -> int:
    return int(raw.replace(",", ""))


def parse_polish_stderr(text: str) -> dict:
    """`polish_*` facts from Polypolish's own per-contig stderr summary.

    Returns {} for anything unparseable rather than raising, the same posture
    `ivar_runner.parse_consensus_stderr` and `completeness_runner.parse_summary`
    take. The cost of a missed fact is a blank field; the cost of raising is
    discarding a polished assembly that already exists on disk.

    The mean depth reported here is Polypolish's own *measurement* over the
    alignments, length-weighted across contigs. It is not the same number as
    the pre-run `estimate_depth`, which is a division over object facts made
    before any alignment exists -- that estimate decides `--careful`, this
    measurement is what actually happened. Both are recorded, because a run
    where they disagree sharply is a run whose `--careful` decision was made
    on a bad estimate, and nothing else would show that.
    """
    text = _ANSI_RE.sub("", text)

    contigs = _CONTIG_RE.findall(text)
    changed = _CHANGED_RE.findall(text)
    depths = _DEPTH_RE.findall(text)
    zero_depth = _ZERO_DEPTH_RE.findall(text)

    if not contigs and not changed:
        return {}

    facts: dict = {}
    if changed:
        facts["polish_changed_positions"] = sum(_int(c) for c in changed)
    if contigs:
        facts["polish_contigs"] = len(contigs)
        lengths = [_int(length) for _, length in contigs]
        facts["polish_assembly_length"] = sum(lengths)
        # Length-weighted, not a plain mean: a 5Mb chromosome and a 3kb
        # plasmid are not two equal votes on "the depth of this assembly".
        # Falls back to a plain mean if the block counts ever disagree,
        # rather than zipping two lists of different lengths into silence.
        if depths and len(depths) == len(lengths) and sum(lengths):
            total = sum(float(d) * n for d, n in zip(depths, lengths, strict=False))
            facts["polish_measured_depth"] = round(total / sum(lengths), 1)
        elif depths:
            facts["polish_measured_depth"] = round(
                sum(float(d) for d in depths) / len(depths), 1
            )
    if zero_depth:
        facts["polish_zero_depth_bp"] = sum(_int(z) for z in zero_depth)
    return facts
