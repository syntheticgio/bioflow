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
