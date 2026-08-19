"""Medaka command construction and output parsing.

Same split `polypolish_runner` and `ivar_runner` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.

Three shapes here are load-bearing and easy to "fix" into something wrong,
all three from Medaka's own `medaka_consensus` wrapper script. See
`docs/superpowers/specs/2026-08-18-medaka-long-read-polishing-design.md`:

- **Medaka writes a directory, not stdout.** The wrapper runs minimap2,
  `medaka inference` and `medaka sequence`, then leaves
  `<outdir>/consensus.fasta`. There is no stdout to capture, which is why
  nothing here mirrors `polypolish_runner.redirect_stdout` -- wrapping this
  argv in a shell redirect would write an empty file beside a correct
  consensus the handler then ignores.
- **The alignment parameters belong to the model, not to us.**
  `medaka_consensus` calls `medaka tools get_alignment_params --model
  $MODEL` and hands the result to minimap2, because the right preset
  depends on which network will consume the alignment. Polypolish's `-a` is
  mandatory and hardcoded; this is the opposite case, where building the
  aligner call ourselves would override a model-dependent choice with a
  fixed guess.
- **`-f` is not optional.** Without it the wrapper prints "WARNING: Output
  ... already exists, may use old results" and returns whatever consensus
  is already there, exiting zero. The handler prepares a fresh workdir per
  job, so this should never trigger -- but the failure it prevents is a job
  returning a *previous* run's assembly and reporting success.
"""

import re
from pathlib import Path

# What `medaka_consensus` names its output inside the directory it is given.
# The `-p/--prefix` option could change this; BioFlow does not pass it, so
# the default is the contract between the runner and the handler.
CONSENSUS_FILENAME = "consensus.fasta"


def build_consensus_command(
    *,
    medaka_path: str,
    draft: Path,
    reads: Path,
    outdir: Path,
    threads: int = 1,
    bacteria: bool = False,
) -> list[str]:
    """The argv for `medaka_consensus`.

    `-f` is unconditional -- see the module docstring. `--bacteria` is an
    opt-in the launch dialog surfaces, never a default: ONT ships that model
    as a research release, and applying it to a eukaryotic draft because it
    happened to be the default would be a silent quality choice made on the
    user's behalf.

    No model is passed. Medaka inspects the basecaller metadata in the reads
    and resolves one itself; the handler records which one it chose, since
    the fallback to a legacy default is invisible in the output.
    """
    argv = [
        medaka_path,
        "-i",
        str(reads),
        "-d",
        str(draft),
        "-o",
        str(outdir),
        "-t",
        str(threads),
        "-f",
    ]
    if bacteria:
        argv.append("--bacteria")
    return argv


# Medaka names its model on stderr before inference starts. Two shapes
# matter and they mean different things:
#
#   Model r1041_e82_400bps_sup_v5.0.0 resolved from input file.
#   Using default consensus model r1041_e82_400bps_sup_v4.2.0.
#
# The first means basecaller metadata was present and read. The second
# means it was not, and Medaka fell back to a legacy default -- succeeding,
# with worse output, and no error anywhere. Nothing in the resulting
# consensus reveals which happened, which is the whole reason these facts
# are recorded.
_RESOLVED_RE = re.compile(r"Model\s+(\S+)\s+resolved from", re.IGNORECASE)
_DEFAULT_RE = re.compile(r"[Uu]sing default\s+\S*\s*model\s+(\S+)")


def parse_model_line(text: str) -> dict:
    """`polish_model` facts from Medaka's own stderr.

    Returns {} for anything unparseable rather than raising, the same
    posture `polypolish_runner.parse_polish_stderr` takes. The cost of a
    missed fact is a blank field; the cost of raising is discarding a
    consensus that already exists on disk.

    The fallback branch is checked first. A run that falls back can also
    mention resolution in a nearby line, and reporting such a run as
    auto-resolved would hide exactly the case these facts exist to expose.
    """
    fallback = _DEFAULT_RE.search(text)
    if fallback:
        return {
            "polish_model": fallback.group(1).rstrip("."),
            "polish_model_auto_resolved": False,
        }

    resolved = _RESOLVED_RE.search(text)
    if resolved:
        return {
            "polish_model": resolved.group(1).rstrip("."),
            "polish_model_auto_resolved": True,
        }

    return {}


def _read_fasta(path: Path) -> dict[str, str]:
    """Contig name -> sequence, uppercased.

    Keyed on the first whitespace-delimited token of the header, not the
    whole line: Medaka appends its own description to headers, and matching
    on the full line would find zero shared contigs and report a polish
    that changed nothing.

    Same streaming shape `gc_tracks.py` uses. Uppercased because soft-
    masking is a claim about repeats, not about bases, and a draft that
    disagrees with the consensus only in case has not been polished.
    """
    contigs: dict[str, str] = {}
    name: str | None = None
    buf: list[str] = []

    with open(path, errors="replace") as fh:
        for line in fh:
            stripped = line.rstrip("\n\r")
            if stripped.startswith(">"):
                if name is not None:
                    contigs[name] = "".join(buf)
                parts = stripped[1:].split()
                name = parts[0] if parts else ""
                buf = []
            elif name is not None:
                buf.append(stripped.strip().upper())

    if name is not None:
        contigs[name] = "".join(buf)
    return contigs


def count_changed_positions(draft: Path, consensus: Path) -> dict:
    """How much the consensus differs from the draft it was built from.

    Medaka, unlike Polypolish, prints no per-contig tally -- it writes a
    consensus and stops. Without this number a run that changed nothing
    would be indistinguishable from one that corrected a thousand errors,
    and "polishing complete" would be the only evidence on the object.

    **Alignment-free by design.** An aligner in the fact-gathering path
    would be a second failure surface for a number that exists to make
    failures visible. Medaka preserves contig identity and order, so a
    name-keyed comparison is well-defined. Where a contig's length changed,
    the substitutions over the shared prefix are still counted and the
    difference is reported as `polish_length_delta` rather than being
    forced into a substitution count that would be meaningless -- a
    one-base insertion would otherwise read as every downstream base having
    changed.

    A contig in the draft with no counterpart in the consensus is counted
    in `polish_contigs_unmatched` rather than raising. Degrading to a
    visible number beats discarding a consensus that is already on disk.
    """
    try:
        draft_contigs = _read_fasta(draft)
        consensus_contigs = _read_fasta(consensus)
    except OSError:
        return {}

    changed = 0
    delta = 0
    compared = 0
    unmatched = 0

    for name, draft_seq in draft_contigs.items():
        polished_seq = consensus_contigs.get(name)
        if polished_seq is None:
            unmatched += 1
            continue
        compared += 1
        delta += len(polished_seq) - len(draft_seq)
        changed += sum(
            1 for a, b in zip(draft_seq, polished_seq, strict=False) if a != b
        )

    return {
        "polish_changed_positions": changed,
        "polish_length_delta": delta,
        "polish_contigs_compared": compared,
        "polish_contigs_unmatched": unmatched,
    }
