"""Command builders and output parsers for Merqury k-mer QV assessment.

Pure functions only: no I/O, no subprocess, no database. The handler in
`app.queue.assembly_qc_handlers` does the running; this module decides what
to run and what the output means, which is what makes both testable without
a tool installed.

**Input filenames never reach a command line under their own names.**
`merqury.sh` derives every output filename from its input basenames, and
`util/util.sh`'s `link` symlinks inputs under those names -- so an object
named `ev<img src=x>.fasta` would put that string into an output path. The
handler links every input under a fixed name and this module's builders take
already-fixed paths. That is QUAST's stored-XSS lesson applied before the
bug exists, the same way `craq_runner` applies it for shell metacharacters.

**k is a property of the database, not of a run.** `eval/qv.sh` reads k back
out of the read database rather than taking it as an argument:

    k=`meryl print $read_db | head -n 2 | tail -n 1 | awk '{print length($1)}'`

So a database built at one k cannot serve a run that wants another. The
caller records k alongside the cached database and rebuilds on mismatch
rather than silently reusing it.
"""

from __future__ import annotations

from pathlib import Path


def build_meryl_count_command(
    *,
    meryl_path: str,
    k: int,
    reads: list[Path],
    output: Path,
    threads: int = 4,
) -> list[str]:
    """`meryl count` over one or more read files into a single database.

    Multiple read files are deliberate: paired-end reads are two files whose
    k-mers belong in one database, because the QV denominator is the whole
    read set rather than one mate.
    """
    return [
        meryl_path,
        "count",
        f"k={k}",
        f"threads={threads}",
        "output",
        str(output),
        *(str(r) for r in reads),
    ]


def build_merqury_command(
    *,
    merqury_path: str,
    read_db: Path,
    assembly: Path,
    out_prefix: str,
) -> list[str]:
    """The top-level `merqury.sh <read.meryl> <asm.fasta> <out>` call.

    One invocation produces QV, k-mer completeness, and the spectra-cn
    plots. Trio mode (maternal/paternal hapmer databases) is never used:
    BioFlow has no parental read-set concept, and inventing one would be a
    second feature.
    """
    return [merqury_path, str(read_db), str(assembly), out_prefix]


def parse_qv(text: str) -> dict[str, float]:
    """Parse Merqury's `<out>.qv`.

    Tab-separated, no header, one row per assembly scored:

        <asm>  <asm-only kmers>  <total kmers>  <QV>  <error rate>

    Every numeric field parses as float. QV is a log-scaled quality score
    and the error rate is a fraction -- neither is ever an integer, and
    asserting the type rather than the value is what catches the class of
    bug QUAST's slice shipped, where `2 == 2.0` hid a wrong type.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 5:
            continue
        return {
            "assembly_qv": float(fields[3]),
            "assembly_qv_error_rate": float(fields[4]),
        }
    raise ValueError("no parseable row found in Merqury .qv output")


def parse_completeness(text: str) -> dict[str, float]:
    """Parse Merqury's `<out>.completeness.stats`.

    Tab-separated, no header:

        <asm>  <set>  <solid kmers found>  <total solid kmers>  <completeness %>

    The `all` row is the whole read set; per-haplotype rows appear only in
    trio mode, which this slice never runs.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 5:
            continue
        return {"assembly_qv_completeness_pct": float(fields[4])}
    raise ValueError("no parseable row found in Merqury completeness.stats")
