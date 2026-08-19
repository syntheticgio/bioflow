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
