"""Per-feature read coverage: bedtools coverage over an annotation and a BAM.

Pure functions only -- the queue handler owns the subprocess call, mirroring
counts_runner's split. `-sorted` with an explicit genome file is
non-negotiable: without it bedtools loads the whole BAM into memory, which is
the OOM shape job_timings exists to catch (spec RS-5).
"""

from pathlib import Path

FEATURE_COLUMNS: tuple[str, ...] = (
    "name", "type", "seq_id", "start", "end", "strand",
    "read_count", "bases_covered", "length", "breadth",
)

# The unique-feature table stays bounded regardless of annotation size;
# summary numbers always cover every row. Same rationale as spec R3-3.
MAX_FEATURES_IN_REPORT = 10_000


def build_genome_file(fai_path: Path, out_path: Path) -> Path:
    """bedtools' genome file: name<TAB>length, in the .fai's own order.

    Order matters: -sorted requires -a, -b, and -g to agree on contig
    order, and the .fai's order is the reference's own.
    """
    lines = []
    for raw in fai_path.read_text().splitlines():
        if not raw.strip():
            continue
        name, length = raw.split("\t")[:2]
        lines.append(f"{name}\t{length}\n")
    out_path.write_text("".join(lines))
    return out_path


def build_command(annotation: Path, bam: Path, genome_file: Path) -> list[str]:
    return [
        "bedtools", "coverage",
        "-sorted",
        "-g", str(genome_file),
        "-a", str(annotation),
        "-b", str(bam),
    ]


def _gff_name(attributes: str) -> str:
    """Name= wins, then ID=, then the raw attribute string truncated.

    Handles both attribute-string punctuations feature_coverage's positional
    parser can receive: GFF3's `key=value;key2=value2` and GTF's
    `key "value"; key2 "value2";` (see _FEATURE_COVERAGE_ANNOTATION_FORMATS
    in pipeline_service.py for why GTF reaches this parser at all -- it is
    position-compatible with GFF3 even though its attribute punctuation
    differs). The two are told apart by whether any `=` is present: a GTF
    attribute string has none, so the `key=value` split below yields nothing
    and the GTF-style fallback runs instead.
    """
    fields = dict(
        part.split("=", 1) for part in attributes.split(";") if "=" in part
    )
    if fields:
        name = fields.get("Name") or fields.get("ID") or attributes
        return name.removeprefix("gene-")

    # GTF style: `key "value"; key2 "value2";` -- split on `;`, then each
    # piece on the first whitespace to get key/value, stripping quotes.
    gtf_fields: dict[str, str] = {}
    for part in attributes.split(";"):
        part = part.strip()
        if not part or " " not in part:
            continue
        key, _, value = part.partition(" ")
        gtf_fields[key] = value.strip().strip('"')
    name = gtf_fields.get("gene_name") or gtf_fields.get("gene_id") or attributes
    return name.removeprefix("gene-")


def parse_coverage(stdout_path: Path, annotation_format: str) -> dict:
    features: list[dict] = []
    zero = 0
    with stdout_path.open() as fh:
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 4 or line.startswith("#"):
                continue
            read_count, bases, length, breadth = cols[-4:]
            row = _row_from_annotation_cols(cols[:-4], annotation_format)
            row.update(
                read_count=int(read_count),
                bases_covered=int(bases),
                length=int(length),
                breadth=float(breadth),
            )
            if row["read_count"] == 0:
                zero += 1
            features.append(row)
    features.sort(key=lambda r: (r["breadth"], r["name"]))
    breadths = sorted(r["breadth"] for r in features)
    n = len(breadths)
    median = 0.0 if n == 0 else (
        breadths[n // 2] if n % 2 else (breadths[n // 2 - 1] + breadths[n // 2]) / 2
    )
    return {
        "feature_count": len(features),
        "features_zero_coverage": zero,
        "median_breadth": median,
        "truncated": len(features) > MAX_FEATURES_IN_REPORT,
        "features": features[:MAX_FEATURES_IN_REPORT],
    }


def _row_from_annotation_cols(cols: list[str], fmt: str) -> dict:
    if fmt == "bed":
        name = cols[3] if len(cols) > 3 else f"{cols[0]}:{cols[1]}-{cols[2]}"
        return {
            "name": name, "type": "region", "seq_id": cols[0],
            "start": int(cols[1]), "end": int(cols[2]),
            "strand": cols[5] if len(cols) > 5 else ".",
        }
    # GFF/GTF: 9 columns
    return {
        "name": _gff_name(cols[8]) if len(cols) > 8 else "",
        "type": cols[2], "seq_id": cols[0],
        "start": int(cols[3]), "end": int(cols[4]), "strand": cols[6],
    }
