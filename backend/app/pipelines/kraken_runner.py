"""Kraken2/Bracken command construction and report parsing.

Same split ``quast_runner`` and ``bakta_runner`` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.
"""

from __future__ import annotations

from pathlib import Path


def build_kraken2_command(
    *,
    kraken2_path: str,
    db_dir: Path,
    reads: Path,
    mate: Path | None,
    report: Path,
    output: Path,
    threads: int,
    gzipped: bool,
) -> list[str]:
    """The argv for ``kraken2`` over one read set.

    ``--memory-mapping`` is deliberately absent: it trades a full in-RAM
    load for page-in on every access, which is the slow path.  Memory is
    budgeted honestly instead, via the registry's per-database ``mem_mb``
    (spec K2-C3).  ``output`` is normally /dev/null -- the per-read
    assignments are enormous and nothing here consumes them; the report is
    the deliverable.
    """
    cmd = [
        kraken2_path,
        "--db", str(db_dir),
        "--threads", str(threads),
        "--report", str(report),
        "--output", str(output),
    ]
    if gzipped:
        cmd.append("--gzip-compressed")
    if mate is not None:
        cmd.append("--paired")
        cmd.extend([str(reads), str(mate)])
    else:
        cmd.append(str(reads))
    return cmd


def build_bracken_command(
    *,
    bracken_path: str,
    db_dir: Path,
    report: Path,
    output: Path,
    read_len: int,
) -> list[str]:
    """The argv for ``bracken`` over an existing Kraken2 report.

    ``-l S``: species-level re-estimation, the rank the taxonomy fact and
    the mismatch check consume.  ``read_len`` comes from the reads object's
    stored stats, defaulting to 100 (spec K2-R3) -- Bracken only accepts
    lengths its database distribution was built for, and the pre-built
    databases ship distributions for 50..300 in steps of 50, so the caller
    rounds to the nearest of those.
    """
    return [
        bracken_path,
        "-d", str(db_dir),
        "-i", str(report),
        "-o", str(output),
        "-r", str(read_len),
        "-l", "S",
    ]


def parse_kraken_report(text: str) -> list[dict]:
    """Rows of Kraken2's six-column report.

    Columns: percentage of reads in the clade, clade read count, reads
    assigned directly to this taxon, rank code, NCBI taxid, and the name
    indented two spaces per tree level (stripped here -- the report is a
    flat fact source, not a tree render).

    Returns ``[]`` for anything unparseable rather than raising, the
    posture ``quast_runner.parse_report_tsv`` documents: a report that
    cannot be read must not fail a run that already produced real output.
    """
    rows: list[dict] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        try:
            rows.append(
                {
                    "pct": float(fields[0]),
                    "clade_reads": int(fields[1]),
                    "direct_reads": int(fields[2]),
                    "rank": fields[3].strip(),
                    "taxid": int(fields[4]),
                    "name": fields[5].strip(),
                }
            )
        except ValueError:
            continue
    return rows


def parse_bracken_output(text: str) -> list[dict]:
    """Rows of Bracken's species table: name, taxid, abundance fraction.

    Same empty-on-garbage posture as ``parse_kraken_report``.
    """
    rows: list[dict] = []
    lines = text.splitlines()
    for line in lines[1:]:  # skip header
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        try:
            rows.append(
                {
                    "name": fields[0].strip(),
                    "taxid": int(fields[1]),
                    "fraction": float(fields[6]),
                }
            )
        except ValueError:
            continue
    return rows


# The fact payload's selection rule (spec K2-R6): enough taxa to see the
# picture, few enough to stay a fact rather than a report.
_TOP_N = 10
_MIN_PCT = 1.0
# A taxon is "dominant" for the mismatch check at >= 5% of clade reads
# (spec K2-R7): low enough to catch a heavily contaminated sample, high
# enough that trace noise never accuses the metadata.
_DOMINANT_PCT = 5.0


def top_taxa(kraken_rows: list[dict], bracken_rows: list[dict]) -> dict:
    """The ``taxonomy`` fact payload.

    Bracken's species fractions are preferred; Kraken2's own species rows
    (rank ``S``) are the fallback when Bracken was skipped (spec K2-R6).
    Selection: top 10 by abundance, plus every taxon at >= 1% -- which for
    a clean single-organism sample is one row, and for a contaminated one
    is the evidence.
    """
    unclassified = next(
        (r["pct"] for r in kraken_rows if r["rank"] == "U"), 0.0
    )
    if bracken_rows:
        candidates = [
            {
                "name": r["name"],
                "rank": "S",
                "taxid": r["taxid"],
                "pct": round(r["fraction"] * 100, 2),
            }
            for r in bracken_rows
        ]
        used = True
    else:
        candidates = [
            {"name": r["name"], "rank": r["rank"], "taxid": r["taxid"], "pct": r["pct"]}
            for r in kraken_rows
            if r["rank"] == "S"
        ]
        used = False

    candidates.sort(key=lambda t: t["pct"], reverse=True)
    taxa = [t for i, t in enumerate(candidates) if i < _TOP_N or t["pct"] >= _MIN_PCT]
    return {"taxa": taxa, "unclassified_pct": unclassified, "bracken_used": used}


def derive_bin_taxonomy(
    kraken_rows: list[dict], *, min_dominance_pct: float = 50.0
) -> dict:
    """Derive dominant taxon and fractions for bin identification (spec L2/R2/R3).

    Returns a dict with:
    - bin_taxon_label: dominant species name if >= min_dominance_pct,
      "mixed" if leading species < min_dominance_pct, or "unclassified" if none.
    - bin_taxon_fraction: float fraction (0.0..1.0) of leading species (or 0.0).
    - bin_unclassified_fraction: float fraction (0.0..1.0) of unclassified sequences.
    """
    unclassified_pct = next(
        (r["pct"] for r in kraken_rows if r["rank"] == "U"), 0.0
    )
    unclassified_fraction = round(unclassified_pct / 100.0, 4)

    species = [
        r for r in kraken_rows
        if r["rank"] == "S"
    ]
    species.sort(key=lambda r: r["pct"], reverse=True)

    if not species:
        label = (
            "unclassified"
            if unclassified_pct >= min_dominance_pct or not kraken_rows
            else "mixed"
        )
        return {
            "bin_taxon_label": label,
            "bin_taxon_fraction": 0.0,
            "bin_unclassified_fraction": unclassified_fraction,
        }

    top = species[0]
    fraction = round(top["pct"] / 100.0, 4)
    if top["pct"] >= min_dominance_pct:
        label = top["name"]
    else:
        label = "mixed"

    return {
        "bin_taxon_label": label,
        "bin_taxon_fraction": fraction,
        "bin_unclassified_fraction": unclassified_fraction,
    }


def organism_mismatch(
    metadata_organism: str | None, kraken_rows: list[dict]
) -> dict | None:
    """Whether the reads disagree with ``metadata["organism"]``.

    Genus-level on purpose: strain and species names in metadata are too
    free-form to match reliably, and a genus-level miss is already a real
    problem.  Absent metadata means no check and no fact -- "not stated"
    and "wrong" are different claims (spec K2-R7).  Returns the evidence
    dict for the ``taxonomy_mismatch`` fact, or None.
    """
    if not metadata_organism or not metadata_organism.strip():
        return None
    claimed_genus = metadata_organism.strip().split()[0].lower()

    dominant = [
        r for r in kraken_rows
        if r["rank"] == "S" and r["pct"] >= _DOMINANT_PCT
    ]
    if not dominant:
        # Nothing classified confidently enough to accuse the metadata.
        return None
    for row in dominant:
        if row["name"].strip().split()[0].lower() == claimed_genus:
            return None
    return {
        "claimed": metadata_organism.strip(),
        "dominant": [{"name": r["name"], "pct": r["pct"]} for r in dominant],
    }

