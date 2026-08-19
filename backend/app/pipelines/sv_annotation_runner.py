"""Structural Variant Gene Overlap Annotator.

Pure functions and runners for intersecting SV intervals [POS, END] against reference GFF/GTF annotations.

Provides Option A annotation for Issue #652, attaching gene overlap information to symbolic SV records (e.g. <DEL>, <INS>, <DUP>) where small-variant tools like bcftools csq cannot produce predictions.
"""

import bisect
import gzip
import re
from pathlib import Path

from app.pipelines import annotation_parse
from app.pipelines.annotation_parse import parse_gff_line, parse_gtf_line

_BCSQ_HEADER = (
    '##INFO=<ID=BCSQ,Number=.,Type=String,Description="Consequence annotations'
    ' from bcftools csq or SV gene overlap">\n'
)

# Standard SV symbolic ALTs or INFO tags
_SYMBOLIC_ALT = re.compile(r"^<[A-Z0-9:]+>$", re.IGNORECASE)


def _open_text(path: Path):
    """Gzip-aware line reader."""
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


def _open_writer(path: Path):
    """Gzip-aware line writer."""
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt")
    return open(path, "wt")


def build_gene_index(gff_path: Path) -> dict[str, list[tuple[int, int, str]]]:
    """Parse a GFF3/GTF file into a contig -> sorted list of (start, end, gene_name) intervals.

    Includes gene-level features ('gene', 'ncRNA_gene', 'pseudogene', 'mRNA', 'transcript')
    or any feature carrying a valid name attribute.
    """
    raw_index: dict[str, list[tuple[int, int, str]]] = {}
    is_gtf = str(gff_path).endswith(".gtf")
    parser = parse_gtf_line if is_gtf else parse_gff_line

    with _open_text(gff_path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            feat = parser(line)
            if feat is None or not feat.name:
                continue

            # Target gene-level or primary transcript features
            ftype = (feat.type or "").lower()
            if ftype in (
                "gene",
                "ncrna_gene",
                "pseudogene",
                "protein_coding_gene",
                "mrna",
                "transcript",
            ) or (ftype == "cds" and feat.name):
                contig = feat.contig
                if contig not in raw_index:
                    raw_index[contig] = []
                raw_index[contig].append((feat.start, feat.end, feat.name))

    # Deduplicate and sort intervals by start position for binary search
    index: dict[str, list[tuple[int, int, str]]] = {}
    for contig, intervals in raw_index.items():
        unique_intervals = list(set(intervals))
        unique_intervals.sort(key=lambda x: (x[0], x[1], x[2]))
        index[contig] = unique_intervals

    return index


def _resolve_contig_intervals(
    gene_index: dict[str, list[tuple[int, int, str]]], contig: str
) -> list[tuple[int, int, str]]:
    """Look up contig in index, falling back to 'chr' prefix adding/stripping."""
    if contig in gene_index:
        return gene_index[contig]
    if contig.startswith("chr") and contig[3:] in gene_index:
        return gene_index[contig[3:]]
    if not contig.startswith("chr") and f"chr{contig}" in gene_index:
        return gene_index[f"chr{contig}"]
    return []


def find_overlapping_genes(
    gene_index: dict[str, list[tuple[int, int, str]]], contig: str, start: int, end: int
) -> list[str]:
    """Find all unique gene names overlapping interval [start, end] (1-based inclusive)."""
    intervals = _resolve_contig_intervals(gene_index, contig)
    if not intervals:
        return []

    s_pos = min(start, end)
    e_pos = max(start, end)

    # Binary search for first interval that could overlap (interval.end >= s_pos)
    # We locate insertion point where interval.start > e_pos to stop search
    matching_genes: set[str] = set()
    starts = [inv[0] for inv in intervals]
    idx = bisect.bisect_left(starts, s_pos - 100_000)  # Safe back-scan buffer for long genes
    if idx < 0:
        idx = 0

    for i in range(idx, len(intervals)):
        g_start, g_end, g_name = intervals[i]
        if g_start > e_pos:
            # All remaining intervals start after e_pos
            break
        if g_start <= e_pos and g_end >= s_pos:
            matching_genes.add(g_name)

    return sorted(matching_genes)


def extract_sv_interval(pos: int, ref: str, info_str: str) -> tuple[int, int]:
    """Extract 1-based [start, end] coordinates from VCF fields."""
    end = pos + len(ref) - 1
    if info_str and info_str != ".":
        # Check END=...
        end_match = re.search(r"(?:^|;)END=(\d+)(?:;|$)", info_str)
        if end_match:
            end = int(end_match.group(1))
        else:
            # Check SVLEN=...
            svlen_match = re.search(r"(?:^|;)SVLEN=(-?\d+)(?:;|$)", info_str)
            if svlen_match:
                svlen = abs(int(svlen_match.group(1)))
                end = max(pos, pos + svlen - 1)

    return min(pos, end), max(pos, end)


def annotate_sv_vcf(vcf_in: Path, vcf_out: Path, gff_path: Path) -> dict[str, int]:
    """Annotate structural variants in a VCF file with gene overlap (Option A).

    Injects BCSQ INFO tag formatted for BioFlow compatibility.
    """
    gene_index = build_gene_index(gff_path)
    annotated_count = 0
    total_count = 0

    vcf_out.parent.mkdir(parents=True, exist_ok=True)

    with _open_text(vcf_in) as fin, _open_writer(vcf_out) as fout:
        has_bcsq_header = False

        for line in fin:
            if line.startswith("##"):
                if "ID=BCSQ," in line:
                    has_bcsq_header = True
                fout.write(line)
                continue
            if line.startswith("#CHROM"):
                if not has_bcsq_header:
                    fout.write(_BCSQ_HEADER)
                fout.write(line)
                continue

            stripped = line.rstrip("\r\n")
            if not stripped:
                continue

            parts = stripped.split("\t")
            if len(parts) < 8:
                fout.write(line)
                continue

            total_count += 1
            chrom, pos_str, _id, ref, alt, _qual, _filt, info = parts[:8]
            try:
                pos = int(pos_str)
            except ValueError:
                fout.write(line)
                continue

            # Determine if record already has a valid BCSQ annotation
            has_bcsq = "BCSQ=" in info and not re.search(r"BCSQ=\.(?:;|$)", info)

            # SV check: symbolic ALT, explicit SVTYPE, or missing BCSQ
            is_symbolic = bool(_SYMBOLIC_ALT.match(alt)) or "SVTYPE=" in info

            if not has_bcsq or is_symbolic:
                start, end = extract_sv_interval(pos, ref, info)
                genes = find_overlapping_genes(gene_index, chrom, start, end)

                if genes:
                    gene_str = ",".join(genes)
                    csq_tag = f"BCSQ=structural_variant_overlap|{gene_str}||protein_coding"
                else:
                    csq_tag = "BCSQ=intergenic||||||"

                # Update or append BCSQ tag in INFO column
                if "BCSQ=" in info:
                    info = re.sub(r"BCSQ=[^;]+", csq_tag, info)
                elif info == "." or not info:
                    info = csq_tag
                else:
                    info = f"{info};{csq_tag}"

                parts[7] = info
                annotated_count += 1
                fout.write("\t".join(parts) + "\n")
            else:
                fout.write(line)

    return {"annotated": annotated_count, "total": total_count}
