"""Medaka actually corrects planted errors, not merely exits zero.

Mirrors what #23 did for Polypolish. The bar is that the polish *changed
the right positions* -- a test asserting completion would pass for a run
that returned the draft unmodified, which is precisely the failure
`polish_changed_positions` exists to expose.

Marked slow and skipped when medaka is not installed, so it runs in the
image and does not break a host-side collection.
"""

import subprocess

import pytest

from app.config import settings
from app.pipelines import medaka_runner, tools

pytestmark = pytest.mark.slow

# Substitutions only. `count_changed_positions` reports length changes
# separately, so an indel-carrying draft would make the equality assertion
# below ill-defined -- see R5 in the spec.
PLANTED = (150, 400, 900, 1500, 2100)


def _synthetic_genome(length: int = 3000) -> str:
    """A deterministic non-repetitive sequence.

    Seeded rather than random: a flaky genome makes a failure impossible to
    reproduce, and a repetitive one makes the aligner rather than the
    polisher the thing under test.
    """
    import random

    rng = random.Random(618)
    return "".join(rng.choice("ACGT") for _ in range(length))


def _plant(seq: str, positions) -> str:
    chars = list(seq)
    for pos in positions:
        chars[pos] = "A" if chars[pos] != "A" else "C"
    return "".join(chars)


@pytest.mark.skipif(not tools.medaka().available, reason="medaka not installed")
def test_medaka_corrects_planted_errors(tmp_path):
    truth = _synthetic_genome()
    draft_seq = _plant(truth, PLANTED)

    draft = tmp_path / "draft.fasta"
    draft.write_text(f">ctg1\n{draft_seq}\n")

    # Reads are generated from the *truth*, so the planted errors exist only
    # in the draft and Medaka has consistent evidence against every one.
    reads = tmp_path / "reads.fastq"
    with open(reads, "w") as fh:
        read_len, step = 500, 25
        n = 0
        for start in range(0, len(truth) - read_len, step):
            chunk = truth[start : start + read_len]
            fh.write(f"@read{n}\n{chunk}\n+\n{'I' * len(chunk)}\n")
            n += 1

    outdir = tmp_path / "out"
    argv = medaka_runner.build_consensus_command(
        medaka_path=settings.medaka_path,
        draft=draft,
        reads=reads,
        outdir=outdir,
        threads=2,
    )
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
    assert proc.returncode == 0, proc.stderr[-3000:]

    consensus = outdir / medaka_runner.CONSENSUS_FILENAME
    assert consensus.exists() and consensus.stat().st_size > 0

    facts = medaka_runner.count_changed_positions(draft, consensus)

    # The assertion that matters: positions changed, and the count is the
    # planted count. Completion alone is explicitly not the bar.
    assert facts["polish_changed_positions"] > 0
    assert facts["polish_changed_positions"] == len(PLANTED)

    # And the corrections went the right way -- a polish that changed the
    # planted positions to the wrong bases would satisfy the count above.
    polished = medaka_runner._read_fasta(consensus)["ctg1"]
    for pos in PLANTED:
        assert polished[pos] == truth[pos].upper()
