"""Bakta command construction and GFF3 parsing.

Same split ``quast_runner`` and ``ragtag_runner`` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.
"""

from __future__ import annotations

from pathlib import Path

# Reused rather than duplicated: the windowing scheme gc_tracks.py established
# in #151 -- 500 windows per contig, floored at 100 bp, longest 50 contigs.
from app.pipelines.gc_tracks import MIN_WINDOW_BASES, WINDOW_COUNT
from app.storage.parsers import MAX_STORED_CONTIGS


def build_bakta_command(
    *,
    bakta_path: str,
    assembly: Path,
    out_dir: Path,
    threads: int,
    genus: str | None = None,
    species: str | None = None,
    strain: str | None = None,
) -> list[str]:
    """The argv for ``bakta`` on a single FASTA assembly.

    ``--genus``, ``--species``, and ``--strain`` are passed through only when
    non-None -- Bakta works without them, but annotation quality improves when
    the organism is known.
    """
    cmd = [
        bakta_path,
        str(assembly),
        "--output",
        str(out_dir),
        "--threads",
        str(threads),
    ]
    if genus is not None:
        cmd.extend(["--genus", genus])
    if species is not None:
        cmd.extend(["--species", species])
    if strain is not None:
        cmd.extend(["--strain", strain])
    return cmd


def parse_gff3(text: str) -> dict[str, list[dict]]:
    """Extract gene coordinates from Bakta's GFF3 output.

    Returns a dict mapping contig name to a list of gene dicts, each with
    ``start``, ``end``, and ``strand``.  Only features of type ``gene`` are
    kept -- tRNA, rRNA, CDS, and other feature types are skipped, since CDS
    is a child of gene and would double-count each locus.

    Returns ``{}`` for anything unparseable rather than raising, the same
    posture ``quast_runner.parse_report_tsv`` documents: coordinates that
    cannot be read must not fail a run that already produced real output.
    """
    genes: dict[str, list[dict]] = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 9:
            continue
        seqid, _source, ftype, start, end, _score, strand, _phase, _attrs = fields
        if ftype != "gene":
            continue
        try:
            gene = {
                "start": int(start),
                "end": int(end),
                "strand": strand,
            }
        except ValueError:
            continue
        genes.setdefault(seqid, []).append(gene)
    return genes


def compute_gene_density(
    genes: dict[str, list[dict]],
    contig_lengths: dict[str, int],
    *,
    window_count: int = WINDOW_COUNT,
) -> dict:
    """Bin gene positions into per-window density tracks.

    ``genes`` is the output of ``parse_gff3``: a dict mapping contig name to a
    list of per-gene dicts (each with ``start`` and ``end``).  Genes are binned
    by their midpoint into windows, and density is expressed as genes per
    kilobase within each window.

    ``contig_lengths`` is the ``sequence_lengths`` fact on the assembly --
    names mapped to base-pair lengths.  Contigs that appear in lengths but have
    zero genes get ``null`` windows (never ``0``: unannotated and zero-genes
    are different claims).

    Returns a dict matching #151's ``gc_tracks`` shape with ``density`` and
    ``count`` parallel arrays, plus ``gene_density_partial`` when contigs were
    truncated at ``MAX_STORED_CONTIGS``.
    """
    # Bin gene midpoints into windows.
    binned: dict[str, list[int]] = {}
    for contig, gene_list in genes.items():
        counts: list[int] = []
        for gene in gene_list:
            midpoint = (gene["start"] + gene["end"]) // 2
            counts.append(midpoint)
        binned[contig] = counts

    # Resolve per contig.
    resolved: list[dict] = []
    for name, total_length in contig_lengths.items():
        n_windows = min(window_count, total_length // MIN_WINDOW_BASES)
        if n_windows == 0:
            continue
        window_bases = total_length // n_windows
        if window_bases == 0:
            continue

        hits = binned.get(name, [])
        if not hits:
            resolved.append({
                "name": name,
                "length": total_length,
                "window_bases": window_bases,
                "density": [None] * n_windows,
                "count": [None] * n_windows,
            })
            continue

        # Count genes per window by their midpoint.
        gene_counts = [0] * n_windows
        for pos in hits:
            wi = min(pos // window_bases, n_windows - 1)
            gene_counts[wi] += 1

        density: list[float | None] = []
        count_out: list[int | None] = []
        for wi in range(n_windows):
            c = gene_counts[wi]
            count_out.append(c)
            # Density per kb within this window.
            density.append(round(c / (window_bases / 1000.0), 4))

        resolved.append({
            "name": name,
            "length": total_length,
            "window_bases": window_bases,
            "density": density,
            "count": count_out,
        })

    if not resolved:
        return {}

    # Keep longest contigs.
    resolved.sort(key=lambda c: c["length"], reverse=True)
    partial = len(resolved) > MAX_STORED_CONTIGS
    if partial:
        resolved = resolved[:MAX_STORED_CONTIGS]

    result: dict = {
        "window_count": window_count,
        "contigs": resolved,
    }
    if partial:
        result["gene_density_partial"] = True
    return result
