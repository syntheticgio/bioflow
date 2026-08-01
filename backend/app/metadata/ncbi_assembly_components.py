"""What an assembly offers for download, and what each component becomes.

The component table is the single place that maps an NCBI `--include` name to
the object role its file lands as. It is deliberately one table rather than
knowledge spread across the handler, the applier and the dialog: the
consequence of disagreement is a CDS FASTA roled as a reference genome,
sitting in the aligner's reference picker.

Availability has two sources, in order of preference:

1. `datasets ... --preview`, which reports per-component file counts and exact
   sizes without transferring anything. Preferred because it is the same tool
   that will perform the download and it answers per-component.
2. The presence of `annotation_info` in a Datasets API report. Coarser -- it
   says "this assembly has annotation" without distinguishing GFF3 from
   protein from CDS -- and used only when the CLI is unavailable.

Both degrade to "genome only", which is always true: every assembly has a
genome sequence.
"""

import json
from dataclasses import dataclass

from app.logging import get_logger

log = get_logger(__name__)

# Megabytes as NCBI reports them: decimal, not binary. Converted here once so
# the dialog and the disk pre-flight cannot disagree about a factor of 1.05.
_MB = 1_000_000


@dataclass(frozen=True)
class ComponentSpec:
    """One downloadable part of an assembly, and what it becomes on ingest."""

    key: str  # the `--include` name
    label: str
    role: str  # the ObjectRole value its file lands as
    # The `included_data_files` key `--preview` reports it under. These names
    # do not match the --include names (`gff3` is reported as `genome_gff`),
    # which is exactly why this mapping is written down.
    preview_key: str
    # `dataset_catalog.json`'s fileType for this component -- the primary
    # labeling source after extraction.
    file_type: str
    # The `sequence_type` metadata value its file lands with. Set from the
    # component rather than guessed from the filename the way an uploaded FASTA
    # is: here we already know what NCBI was asked for, and knowing beats
    # inferring. None for components that are not sequence at all (GFF3).
    sequence_type: str | None = None
    mandatory: bool = False


COMPONENTS: dict[str, ComponentSpec] = {
    "genome": ComponentSpec(
        key="genome",
        label="Genome FASTA",
        role="reference",
        preview_key="all_genomic_fasta",
        file_type="GENOMIC_NUCLEOTIDE_FASTA",
        sequence_type="Genomic",
        # Not selectable-off: every other component describes coordinates or
        # products of this sequence.
        mandatory=True,
    ),
    "gff3": ComponentSpec(
        key="gff3",
        label="Annotation (GFF3)",
        role="annotation",
        preview_key="genome_gff",
        file_type="GFF3",
    ),
    "protein": ComponentSpec(
        key="protein",
        label="Protein FASTA",
        role="protein",
        preview_key="prot_fasta",
        file_type="PROTEIN_FASTA",
        sequence_type="Protein",
    ),
    "cds": ComponentSpec(
        key="cds",
        label="CDS FASTA",
        role="transcript",
        preview_key="cds_fasta",
        file_type="CDS_NUCLEOTIDE_FASTA",
        sequence_type="CDS",
    ),
}

# Ordered for display: genome first because it is mandatory, then annotation
# (the most-wanted extra), then the two sequence sets.
COMPONENT_ORDER = ("genome", "gff3", "protein", "cds")


@dataclass
class ComponentAvailability:
    key: str
    label: str
    role: str
    available: bool
    size_bytes: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "role": self.role,
            "available": self.available,
            "size_bytes": self.size_bytes,
            "reason": self.reason,
        }


def parse_preview(body: str) -> list[ComponentAvailability] | None:
    """Component availability from `datasets ... --preview` output.

    Returns None when the output cannot be parsed at all, which the caller
    must distinguish from "parsed, and nothing is available": the first means
    fall back to the API report, the second is a real answer about a
    genome-only assembly.
    """
    try:
        payload = json.loads(body)
        files = payload["included_data_files"]
    except (ValueError, KeyError, TypeError):
        log.debug("assembly_preview_unparseable")
        return None
    if not isinstance(files, dict):
        return None

    out: list[ComponentAvailability] = []
    for key in COMPONENT_ORDER:
        spec = COMPONENTS[key]
        entry = files.get(spec.preview_key)
        entry = entry if isinstance(entry, dict) else {}
        count = entry.get("file_count") or 0
        size_mb = entry.get("size_mb") or 0
        available = bool(count)
        out.append(
            ComponentAvailability(
                key=spec.key,
                label=spec.label,
                role=spec.role,
                available=available,
                size_bytes=int(size_mb * _MB) if size_mb else None,
                reason=None if available else _unavailable_reason(spec, None),
            )
        )
    return out


def from_report(report: dict) -> dict[str, ComponentAvailability]:
    """Availability inferred from a Datasets API report.

    The fallback path. `annotation_info` presence is the only signal, so all
    three non-genome components share one answer -- coarser than `--preview`,
    but enough to avoid offering annotation for an assembly that has none.
    """
    report = report if isinstance(report, dict) else {}
    annotated = isinstance(report.get("annotation_info"), dict)
    paired = report.get("paired_accession")
    paired = paired if isinstance(paired, str) and paired.strip() else None

    out: dict[str, ComponentAvailability] = {}
    for key in COMPONENT_ORDER:
        spec = COMPONENTS[key]
        available = spec.mandatory or annotated
        out[key] = ComponentAvailability(
            key=spec.key,
            label=spec.label,
            role=spec.role,
            available=available,
            reason=None if available else _unavailable_reason(spec, paired),
        )
    return out


def _unavailable_reason(spec: ComponentSpec, paired: str | None) -> str:
    """Why a component is greyed out, in terms the user can act on.

    A GenBank assembly usually has no annotation while its RefSeq twin does,
    so naming the paired accession turns a dead end into a next step.
    """
    if paired and paired.upper().startswith("GCF_"):
        return (
            f"Not available for this assembly. The RefSeq version "
            f"({paired}) has annotation."
        )
    return "Not available for this assembly."


def include_argument(keys: list[str]) -> str:
    """The `--include` value for the selected components.

    Genome is forced in: it is mandatory, and a request that omits it is a
    frontend bug rather than an intent worth honoring.
    """
    selected = [k for k in COMPONENT_ORDER if k in set(keys)]
    if "genome" not in selected:
        selected.insert(0, "genome")
    return ",".join(selected)
