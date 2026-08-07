"""Command builder and output parser for GCI continuity inspection.

Pure functions only: no I/O, no subprocess, no database.

**GCI never invokes an aligner.** It consumes finished BAM/PAF files
through `--hifi` and `--nano`. Its README marks winnowmap "(optional, but
wanted for mapping)" -- the same parenthetical it gives minimap2 -- so
every aligner in its Requirements list is a suggestion for producing input,
not a dependency. This module therefore builds no alignment command and the
handler runs no aligner.

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

**GCI's N50s are its own.** `assembly_continuity_expected_n50` and
`_observed_n50` are computed from filtered read depth, not from contig
lengths, and must never be written into the `sequence_*` namespace that
`_parse_fasta` fills at ingest. Two facts that are supposed to agree, on
one object, is the bug the epic recorded when it deleted `assembly_n50`.
"""

from __future__ import annotations

from pathlib import Path


def build_gci_command(
    *,
    gci_path: str,
    assembly: Path,
    hifi_bam: Path | None,
    nano_bam: Path | None,
    out_dir: Path,
    prefix: str,
    threads: int = 8,
    map_qual: int = 30,
    plot: bool = False,
) -> list[str]:
    """Build the `GCI.py` invocation.

    At least one of `hifi_bam` / `nano_bam` must be set; reaching here with
    neither is a caller bug rather than a user error, same as
    `craq_runner.build_craq_command` -- there is no short-read slot to
    degrade into.

    `map_qual` is passed explicitly rather than left to GCI's default so
    that the value is always recorded as a fact: upstream is explicit that
    lowering it admits multi-mapping reads from repetitive regions, which
    makes runs at different thresholds incomparable.
    """
    if hifi_bam is None and nano_bam is None:
        raise ValueError("GCI needs at least one of hifi_bam or nano_bam")

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
    if hifi_bam is not None:
        cmd += ["--hifi", str(hifi_bam)]
    if nano_bam is not None:
        cmd += ["--nano", str(nano_bam)]
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
