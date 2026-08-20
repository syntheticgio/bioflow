"""Variants in regions: bedtools intersect between a VCF and an annotation.

Answers: "Where do my variants land relative to annotated features?"
Uses sorted inputs + genome file + -sorted per spec RS-5.
"""

from pathlib import Path

from app.pipelines.feature_coverage_runner import _gff_name

MAX_FEATURES_IN_REPORT = 10_000


def build_command(vcf: Path, annotation: Path, genome_file: Path) -> list[str]:
    """Command builder for bedtools intersect VCF vs Annotation.

    `-wao` writes the original VCF record (A) and annotation record (B)
    plus overlap length (0 if no overlap).
    """
    return [
        "bedtools",
        "intersect",
        "-sorted",
        "-g",
        str(genome_file),
        "-a",
        str(vcf),
        "-b",
        str(annotation),
        "-wao",
    ]


def parse_output(stdout_path: Path, annotation_format: str = "gff") -> dict:
    """Parse bedtools intersect -wao output.

    Outputs summary stats:
    - total_variants: total unique VCF variants
    - variants_in_features: count of VCF variants inside >= 1 feature
    - feature_type_counts: dict of feature type -> count of variant hits
    - feature_variant_counts: list of features with variant counts
    """
    all_variants: set[tuple[str, str, str, str]] = set()
    hit_variants: set[tuple[str, str, str, str]] = set()
    type_counts: dict[str, int] = {}
    feature_hits: dict[tuple[str, str, str], int] = {}  # (name, type, seq_id) -> count

    with stdout_path.open() as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 5:
                continue

            # VCF A columns: chrom=cols[0], pos=cols[1], id=cols[2], ref=cols[3], alt=cols[4]
            variant_key = (cols[0], cols[1], cols[3], cols[4])
            all_variants.add(variant_key)

            overlap_raw = cols[-1]
            is_neg_digit = overlap_raw.startswith("-") and overlap_raw[1:].isdigit()
            overlap_bp = (
                int(overlap_raw)
                if overlap_raw.isdigit() or is_neg_digit
                else 0
            )
            if overlap_bp <= 0:
                continue

            hit_variants.add(variant_key)

            # Extract B feature details from cols
            if annotation_format in ("gff", "gtf"):
                # B has 9 columns: cols[-10:-1]
                b_cols = cols[-10:-1]
                seq_id = b_cols[0]
                ftype = b_cols[2]
                attrs = b_cols[8]
                name = _gff_name(attrs)
            else:
                # BED format
                b_cols = cols[:-1]
                seq_id = b_cols[0]
                ftype = "region"
                name = b_cols[3] if len(b_cols) > 3 else f"{seq_id}:{b_cols[1]}-{b_cols[2]}"

            type_counts[ftype] = type_counts.get(ftype, 0) + 1
            feat_key = (name, ftype, seq_id)
            feature_hits[feat_key] = feature_hits.get(feat_key, 0) + 1

    feature_list = [
        {"name": k[0], "type": k[1], "seq_id": k[2], "variant_count": count}
        for k, count in feature_hits.items()
    ]
    feature_list.sort(key=lambda x: (-x["variant_count"], x["name"]))

    return {
        "total_variants": len(all_variants),
        "variants_in_features": len(hit_variants),
        "feature_type_counts": type_counts,
        "truncated": len(feature_list) > MAX_FEATURES_IN_REPORT,
        "feature_variant_counts": feature_list[:MAX_FEATURES_IN_REPORT],
    }
