"""Annotation comparison: bedtools jaccard and bedtools intersect -v.

Answers: "How much do these two annotations of the same assembly agree?"

Neither command below carries `-sorted`/`-g`, unlike the other bedtools
consumers here, and that is deliberate rather than an RS-5 omission:
`jaccard` requires sorted input unconditionally and rejects anything else
with a non-zero exit, so the flag would add nothing it does not already
enforce. Sorting is still mandatory -- the handler sorts both annotations
before calling either builder -- it is simply enforced by bedtools rather
than requested by a flag.
"""

from pathlib import Path

from app.pipelines.feature_coverage_runner import _gff_name

MAX_UNIQUE_FEATURES_IN_REPORT = 10_000


def build_jaccard_command(anno_a: Path, anno_b: Path) -> list[str]:
    return ["bedtools", "jaccard", "-a", str(anno_a), "-b", str(anno_b)]


def build_subtract_command(anno_a: Path, anno_b: Path) -> list[str]:
    """Features in A that have no overlap in B."""
    return ["bedtools", "intersect", "-a", str(anno_a), "-b", str(anno_b), "-v"]


def parse_jaccard_output(stdout_path: Path) -> dict:
    """Parse bedtools jaccard stdout.

    Output format:
    intersection\tunion\tjaccard\tn_intersections
    1234\t5678\t0.21732\t42
    """
    intersection = 0
    union = 0
    jaccard = 0.0
    n_intersections = 0

    with stdout_path.open() as fh:
        for line in fh:
            if line.startswith("intersection") or not line.strip():
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 3:
                intersection = int(cols[0])
                union = int(cols[1])
                jaccard = float(cols[2])
                if len(cols) >= 4:
                    n_intersections = int(cols[3])
                break

    return {
        "intersection_bp": intersection,
        "union_bp": union,
        "jaccard": jaccard,
        "n_intersections": n_intersections,
    }


def parse_subtract_output(stdout_path: Path, annotation_format: str = "gff") -> list[dict]:
    """Parse bedtools intersect -v stdout to list unique features."""
    unique_features: list[dict] = []
    with stdout_path.open() as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 4:
                continue

            if annotation_format in ("gff", "gtf"):
                name = _gff_name(cols[8]) if len(cols) > 8 else cols[0]
                ftype = cols[2] if len(cols) > 2 else "feature"
                seq_id = cols[0]
                start = int(cols[3]) if len(cols) > 3 else 0
                end = int(cols[4]) if len(cols) > 4 else 0
            else:
                seq_id = cols[0]
                start = int(cols[1])
                end = int(cols[2])
                name = cols[3] if len(cols) > 3 else f"{seq_id}:{start}-{end}"
                ftype = "region"

            unique_features.append({
                "name": name,
                "type": ftype,
                "seq_id": seq_id,
                "start": start,
                "end": end,
            })

    return unique_features
