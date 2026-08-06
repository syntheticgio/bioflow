"""QUAST command construction and output parsing.

Same split `ragtag_runner` and `polypolish_runner` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.

Verified against a real `quast 5.3.0` install on 2026-08-05 (real yeast
GCA_000146045.2, chopped into contigs carrying deliberate junction errors,
against the GCF_000146045.2 reference). Findings from that run shape this
module and are recorded in detail on the design doc
(`docs/superpowers/specs/2026-08-05-remaining-post-assembly-qc-design.md`) and
the implementation plan
(`docs/superpowers/plans/2026-08-05-quast-misassembly-qc.md`):

- **The `-l` label is a security control, not cosmetics.** QUAST sanitizes
  contig names (`qutils.correct_name`, `[^\\w\\._\\-]` -> `_`) but not the
  assembly label (`qutils.correct_asm_label`, strip and truncate only), and
  the label defaults to the input filename. An input named
  `ev<img src=x onerror=alert(7)>.fasta` puts that tag verbatim and unescaped
  into `report.html` -- confirmed by exploiting it. Every caller of
  `build_quast_command` must pass a fixed, non-user-derived `label` and link
  the input under a matching fixed filename; see `assess_misassemblies` in
  `queue/assembly_qc_handlers.py`, which does both.
- **Contiguity numbers are deliberately not parsed.** `report.tsv` also
  reports N50, N90, L50, L90, auN and total length, all already computed for
  every FASTA at ingest by `storage/parsers._parse_fasta`, from the whole
  assembly rather than QUAST's `--min-contig`-filtered subset. Two facts that
  are supposed to agree, from different code paths with different cutoffs, is
  the bug `assembly_n50` was deleted for in the 2026-08-02 post-assembly QC
  design. Only reference-derived rows -- ones with no ingest-time twin -- are
  parsed here.
"""

from pathlib import Path


def build_quast_command(
    *,
    quast_path: str,
    assembly: Path,
    reference: Path,
    out_dir: Path,
    threads: int,
    min_contig: int = 500,
    label: str = "assembly",
) -> list[str]:
    """The argv for `quast.py` in reference-based mode only.

    `<assembly>` is positional, `-r <reference>` is a flag -- unlike RagTag
    there is no argument-order transposition trap here.

    No `--gene-finding`, `--rna-finding` or `--conserved-genes-finding`: none
    of the tooling those need is installed (see install-quast.sh), and none
    of it is reference-based misassembly detection, which is the one thing
    this application uses QUAST for.

    `label` must be a fixed, non-user-derived string -- see the module
    docstring. It is a required keyword rather than defaulted to the
    assembly's own name specifically so a caller has to choose it rather than
    inherit whatever the input file happened to be called.
    """
    return [
        quast_path,
        "-o",
        str(out_dir),
        "-t",
        str(threads),
        "-r",
        str(reference),
        "--min-contig",
        str(min_contig),
        "-l",
        label,
        str(assembly),
    ]


# `report.tsv` rows this application stores, mapped to their fact names.
# Reference-derived only -- see the module docstring for why N50/N90/L50/
# L90/auN/total length are deliberately absent despite QUAST reporting them.
_REPORT_TSV_FACTS: dict[str, str] = {
    "# misassemblies": "assembly_misassembly_total",
    "# misassembled contigs": "assembly_misassembly_contigs",
    "Misassembled contigs length": "assembly_misassembly_contigs_length",
    "# local misassemblies": "assembly_misassembly_local",
    "Genome fraction (%)": "assembly_reference_genome_fraction_pct",
    "Duplication ratio": "assembly_reference_duplication_ratio",
    "# mismatches per 100 kbp": "assembly_reference_mismatches_per_100kbp",
    "# indels per 100 kbp": "assembly_reference_indels_per_100kbp",
    "# unaligned contigs": "assembly_reference_unaligned_contigs",
    "Unaligned length": "assembly_reference_unaligned_length",
    "NGA50": "assembly_reference_nga50",
    "NGA90": "assembly_reference_nga90",
}

# Rows whose value is a plain int rather than a float or passthrough string.
_INT_FACTS = {
    "assembly_misassembly_total",
    "assembly_misassembly_contigs",
    "assembly_misassembly_contigs_length",
    "assembly_misassembly_local",
    "assembly_reference_unaligned_contigs",
    "assembly_reference_unaligned_length",
    "assembly_reference_nga50",
    "assembly_reference_nga90",
}


def parse_report_tsv(text: str) -> dict:
    """Reference-derived facts from `report.tsv`, a two-column TSV of
    `<row name>\\t<value>` with one assembly column.

    Returns `{}` for anything that fails to parse rather than raising, the
    posture `ragtag_runner.parse_stats` documents: a summary that cannot be
    read must not fail a run that already produced real output.

    `# unaligned contigs` is reported as `"0 + 0 part"` (whole + partial);
    only the whole count is stored, since a partial-unaligned count with no
    accompanying length is not independently useful and QUAST does not
    report one. `NGA50`/`NGA90` are `"-"` when the reference has no declared
    genome size for the tool to compute them against, in which case they are
    omitted rather than stored as a sentinel.
    """
    facts: dict = {}
    for line in text.strip().splitlines():
        if "\t" not in line:
            continue
        row, _, value = line.partition("\t")
        row = row.strip()
        value = value.strip()
        key = _REPORT_TSV_FACTS.get(row)
        if key is None or not value or value == "-":
            continue

        if row == "# unaligned contigs":
            value = value.split()[0]

        try:
            facts[key] = int(value) if key in _INT_FACTS else float(value)
        except ValueError:
            continue

    return facts


# `misassemblies_report.tsv` rows this application stores. Indented in the
# file itself (e.g. "    # c. relocations") -- matched here on the stripped
# key, since the leading whitespace is layout, not part of the row name.
_MISASSEMBLIES_REPORT_FACTS: dict[str, str] = {
    "# c. relocations": "assembly_misassembly_relocations",
    "# c. translocations": "assembly_misassembly_translocations",
    "# c. inversions": "assembly_misassembly_inversions",
}


def parse_misassemblies_report(text: str) -> dict:
    """The relocation/translocation/inversion breakdown from
    `contigs_reports/misassemblies_report.tsv`, which `report.tsv` does not
    carry.

    Returns `{}` on anything unparseable, same posture as `parse_report_tsv`.

    An internal inversion scores **two** misassemblies, one per junction --
    verified against a real run (a single 50 kb reverse-complemented segment
    inside one contig produced `# c. inversions: 2`). So this total is a
    breakpoint count, not a contig count; `assembly_misassembly_contigs`
    from `parse_report_tsv` is the contig count, and the two are not
    interchangeable.
    """
    facts: dict = {}
    for line in text.strip().splitlines():
        if "\t" not in line:
            continue
        row, _, value = line.partition("\t")
        key = _MISASSEMBLIES_REPORT_FACTS.get(row.strip())
        if key is None:
            continue
        try:
            facts[key] = int(value.strip())
        except ValueError:
            continue

    return facts
