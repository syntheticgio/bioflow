"""Sequence extraction runner: seqkit subseq on assembly FASTA.

Answers: "Give me these regions/sequences from this assembly as a new FASTA."

**Why seqkit rather than samtools faidx**, which is already installed and
already drives other runners here: for text mode the two are interchangeable
-- `samtools faidx -r` takes the same `name:start-end` lines and handles bare
sequence names too. The difference is annotation mode (spec R4-5), which
extracts features selected from a GFF, and genes sit on both strands.
`seqkit subseq --bed` reads the strand column per feature and reverse-
complements only the minus-strand ones; `samtools faidx -i` reverse-
complements every region in the run or none, so a mixed-strand feature set
cannot be expressed as one invocation. Verified against seqkit v2.13.0 and
samtools' own `faidx --help` at implementation time.

Using one tool for both modes also keeps R4-5's promise that annotation mode
"reuses R4-3's runner path unchanged" literally true.
"""

from pathlib import Path

from app.errors import ValidationError


def build_command(fasta: Path, bed_file: Path) -> list[str]:
    """Build seqkit subseq command using a BED regions file."""
    return [
        "seqkit",
        "subseq",
        "--bed",
        str(bed_file),
        str(fasta),
    ]


def parse_query_lines(query_text: str, fai_records: dict[str, int]) -> list[tuple[str, int, int]]:
    """Parse user query text (bare sequence names or name:start-end regions).

    `fai_records` is dict of seq_name -> length.
    Returns list of (seq_name, start_0based, end_1based).
    Raises ValidationError if any line is invalid.
    """
    regions: list[tuple[str, int, int]] = []
    lines = [
        line.strip()
        for line in query_text.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not lines:
        raise ValidationError("No valid sequence names or regions provided.")

    for line in lines:
        if ":" in line:
            parts = line.split(":", 1)
            seq_name = parts[0].strip()
            range_str = parts[1].strip()
            if "-" not in range_str:
                raise ValidationError(f"Invalid region format {line!r}: expected name:start-end")
            start_str, end_str = range_str.split("-", 1)
            try:
                start_1 = int(start_str.replace(",", ""))
                end_1 = int(end_str.replace(",", ""))
            except ValueError as err:
                raise ValidationError(f"Invalid region coordinates in {line!r}") from err

            if seq_name not in fai_records:
                raise ValidationError(f"No sequence named {seq_name!r} in reference.")

            seq_len = fai_records[seq_name]
            if start_1 < 1:
                raise ValidationError(f"Start position {start_1} must be >= 1 in {line!r}")
            if end_1 < start_1:
                raise ValidationError(
                    f"End position {end_1} must be >= start {start_1} in {line!r}"
                )
            if end_1 > seq_len:
                raise ValidationError(
                    f"End {end_1:,} exceeds sequence length {seq_len:,} for {seq_name!r}"
                )

            regions.append((seq_name, start_1 - 1, end_1))
        else:
            seq_name = line.strip()
            if seq_name not in fai_records:
                raise ValidationError(f"No sequence named {seq_name!r} in reference.")
            seq_len = fai_records[seq_name]
            regions.append((seq_name, 0, seq_len))

    return regions


def write_bed_file(regions: list[tuple[str, int, int]], out_bed: Path) -> None:
    """Write (seq_name, start_0based, end_1based) to BED file."""
    lines = [f"{seq}\t{start}\t{end}\n" for seq, start, end in regions]
    out_bed.write_text("".join(lines))
