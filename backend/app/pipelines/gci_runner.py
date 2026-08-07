"""Command builder and output parser for GCI continuity inspection.

Pure functions only: no I/O, no subprocess, no database.

**GCI never invokes an aligner.** It consumes finished BAM/PAF files
through `--hifi` and `--nano`. Its README marks winnowmap "(optional, but
wanted for mapping)" -- the same parenthetical it gives minimap2 -- so
every aligner in its Requirements list is a suggestion for producing input,
not a dependency. This module therefore builds no alignment command and the
handler runs no aligner.

**`--hifi`/`--nano` each take a list, natively.** Read from the installed
`/opt/gci/GCI.py:1041-1042`: both are `nargs='+'`, documented as "at least
one bam file". So pairing minimap2 with winnowmap needs no merge step and
no second GCI invocation -- both BAMs go on the same command line, in the
same slot. GCI's own README (line 157) states the intended shape directly:
"We recommend to input only one alignment file per software (minimap2 and
winnowmap) using the same set of long reads."

**Two aligners are upstream's recommendation, and the score records which
were used.** GCI's FAQ reports that WM2+MM2 and VM+MM2 yield similar issues
and scores, and recommends WM2+MM2 because two aligners cross-check in
repetitive regions. A minimap2-only run is a supported invocation that is
less sensitive there -- so it is stored, with
`assembly_continuity_aligners` saying what produced it. That is a
deliberate divergence from CRAQ's omission rule: CRAQ omits CSE on NGS-only
runs because upstream says it is "hardly detected", while upstream here says
the scores are similar. The rule is driven by what upstream says the
degraded mode measures, not by a general preference for omitting things.

**`--mq-cutoff` and `-op`/`--ovlp-percent` only mean something in multi-BAM
mode.** GCI's own help text: `--mq-cutoff` is "only used when inputting more
than one alignment file", and `--ovlp-percent` is "minimum overlapping
percentage of the same read alignment if inputting more than one alignment
file". `-op` is how GCI actually cross-checks two aligners -- a read is kept
only when both alignments place it compatibly -- so the same reasoning that
makes `-mq`/`map_qual` explicit here applies to these two: passing them in
single-BAM mode would record a filter GCI's own help says never ran, so they
are appended only when a slot carries more than one BAM.

**GCI's N50s are its own.** `assembly_continuity_expected_n50` and
`_observed_n50` are computed from filtered read depth, not from contig
lengths, and must never be written into the `sequence_*` namespace that
`_parse_fasta` fills at ingest. Two facts that are supposed to agree, on
one object, is the bug the epic recorded when it deleted `assembly_n50`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

# GCI's own defaults for these two flags (`GCI.py:1053,1055`), used only
# when they are actually passed -- see build_gci_command's docstring for why
# that is conditional on more than one BAM being in a slot.
DEFAULT_MQ_CUTOFF = 50
DEFAULT_OVLP_PERCENT = 0.9


def build_gci_command(
    *,
    gci_path: str,
    assembly: Path,
    hifi_bams: Sequence[Path] = (),
    nano_bams: Sequence[Path] = (),
    out_dir: Path,
    prefix: str,
    threads: int = 8,
    map_qual: int = 30,
    mq_cutoff: int = DEFAULT_MQ_CUTOFF,
    ovlp_percent: float = DEFAULT_OVLP_PERCENT,
    plot: bool = False,
) -> list[str]:
    """Build the `GCI.py` invocation.

    At least one BAM across `hifi_bams`/`nano_bams` must be given; reaching
    here with both empty is a caller bug rather than a user error, same as
    `craq_runner.build_craq_command` -- there is no short-read slot to
    degrade into.

    `map_qual` is passed explicitly rather than left to GCI's default so
    that the value is always recorded as a fact: upstream is explicit that
    lowering it admits multi-mapping reads from repetitive regions, which
    makes runs at different thresholds incomparable. `mq_cutoff` and
    `ovlp_percent` get the identical treatment, but only reach the command
    line when a slot has more than one BAM -- see the module docstring for
    why passing them otherwise would misrecord a filter that never ran.
    """
    if not hifi_bams and not nano_bams:
        raise ValueError("GCI needs at least one of hifi_bams or nano_bams")

    cmd = [
        gci_path,
        "-r",
        str(assembly),
        "-d",
        str(out_dir),
        "-o",
        prefix,
        "-t",
        str(threads),
        "-mq",
        str(map_qual),
    ]
    if hifi_bams:
        cmd += ["--hifi", *(str(b) for b in hifi_bams)]
    if nano_bams:
        cmd += ["--nano", *(str(b) for b in nano_bams)]
    if len(hifi_bams) > 1 or len(nano_bams) > 1:
        cmd += ["--mq-cutoff", str(mq_cutoff), "-op", str(ovlp_percent)]
    if plot:
        cmd += ["-p", "-it", "pdf"]
    return cmd


def parse_gci(text: str) -> dict[str, float | int]:
    """Parse GCI's `<prefix>.gci` summary.

    Verified against a real run: GCI v1.0 on the Zenodo `example.tar.gz`
    dataset (`GCI.py -r MH63.fasta --hifi ... -o MH63`) writes a leading
    chemistry label line (`"HiFi:"` or `"ONT:"`), then a tab-separated
    header row, one row per chromosome, the whole-assembly `"Genome"`
    aggregate row, and a trailing line of dashes. Only the `Genome` row is
    read. The label line and the per-chromosome rows are skipped by the
    same `fields[0] != "Genome"` filter `craq_runner.parse_final_report`
    uses for its own whole-assembly row (the label line's single field
    never equals `"Genome"` either); the trailing dash line is skipped by
    the `len(fields) < 6` check since it has no tabs at all. Column
    headers read "Theoretical maximum N50" / "Curated N50" / "Theoretical
    minimum contigs number" / "Curated contigs number" / "GCI score" in
    the real output, not the README's prose names, but header text is
    never parsed -- only column position -- so this is cosmetic.

    Counts and N50s parse as int, the continuity index as float. Asserting
    the type rather than the value is what catches the class of bug QUAST's
    slice shipped, where `2 == 2.0` hid a wrong type for a whole release.

    Unlike `craq_runner._float`, a bad numeric field here raises rather than
    being dropped and the row treated as partial. CRAQ's fields are each
    independently meaningful facts, so a summary that cannot be read in full
    must not fail a run that already produced real output. GCI's whole
    point is producing these five numbers together as its score; a
    `Genome` row with one unparseable field means something went wrong
    upstream, and a partial GCI result is not a valid GCI score at all.
    """
    rows = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    ]

    for row in rows[1:]:
        fields = row.split("\t")
        if len(fields) < 6 or fields[0] != "Genome":
            continue
        try:
            return {
                "assembly_continuity_expected_n50": int(float(fields[1])),
                "assembly_continuity_observed_n50": int(float(fields[2])),
                "assembly_continuity_expected_contigs": int(float(fields[3])),
                "assembly_continuity_observed_contigs": int(float(fields[4])),
                "assembly_continuity_gci": float(fields[5]),
            }
        except ValueError as exc:
            raise ValueError(f"could not parse GCI Genome row: {row!r}") from exc

    raise ValueError("no parseable data row found in GCI output")
