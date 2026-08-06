"""Walking a data object's ancestry into a fact set a methods section can use.

The output of this module is consumed by two renderers that never touch the
database (`provenance_report.py`, `provenance_prompt.py`). Keeping all I/O
here is what lets those two be tested as pure functions over hand-built
chains -- which matters because they are where the anti-fabrication rules
live, and those need exhaustive cheap tests.

Steps come from `produced_by_job`, not from the `*_provenance` fact keys in
`queue/results.py`. Several appliers set `produced_by_job` while passing
`facts=` with no named provenance builder at all, so a facts-first walk would
drop those steps silently. Anchoring on the job means the worst case is a
step rendered with an explicit "parameters not recorded" gap.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

from beanie import PydanticObjectId

from app.models.object import ObjectRole

# How deep an ancestry may be before we assume the data is cyclic. Real
# chains are 3-6 deep; this is a guard against hand-edited documents, not an
# expected limit.
MAX_DEPTH = 64


class GapKind(StrEnum):
    """Why a fact a methods section wants is not available.

    Five of these are states a real chain reaches, and they are kept distinct
    because they have different remedies: a missing version is a probe bug, a
    missing parameter set is a historical artifact from before that step had
    a provenance builder, and a share boundary is permanent and expected.
    Collapsing them into one "unknown" would tell the user nothing about
    whether they can fix it.
    """

    VERSION_UNRECORDED = "version_unrecorded"
    PARAMS_UNRECORDED = "params_unrecorded"
    SHARE_BOUNDARY = "share_boundary"
    DANGLING_PARENT = "dangling_parent"
    DEPTH_EXCEEDED = "depth_exceeded"


@dataclass(frozen=True)
class Gap:
    kind: GapKind
    # Which object the gap is attached to, so the renderer can place it in
    # the position the fact would have occupied.
    object_id: PydanticObjectId | None = None
    detail: str | None = None


@dataclass(frozen=True)
class Step:
    """One job that produced an object."""

    job_type: str
    verb: str
    tool: str | None = None
    tool_version: str | None = None
    params: dict = field(default_factory=dict)
    ran_at: datetime | None = None
    outcome: str | None = None
    gaps: tuple[Gap, ...] = ()


@dataclass(frozen=True)
class Node:
    object_id: PydanticObjectId
    name: str
    role: ObjectRole | None
    kind: Literal["spine", "supporting"]
    produced_by: Step | None
    parents: tuple[PydanticObjectId, ...] = ()


@dataclass(frozen=True)
class ProvenanceChain:
    target: Node
    nodes: dict[PydanticObjectId, Node]
    order: tuple[PydanticObjectId, ...]
    gaps: tuple[Gap, ...]
    branches: tuple[tuple[PydanticObjectId, ...], ...] = ()

    @property
    def gap_count(self) -> int:
        return len(self.gaps)


# Handler name -> how a methods section says it happened.
#
# Verified against `registry.all_handlers()` rather than recalled. The
# companion frozenset below covers every registered handler this dict
# deliberately omits; `test_provenance_verbs.py` asserts the two partition
# the registry exactly.
#
# These strings are prose, and nothing can mechanically check that
# "quantified with" is the right phrase for `quantify` -- the same staleness
# risk `ToolMeta.usage` carries elsewhere in this codebase. What the test does guarantee is that a
# wrong-but-present verb is the worst case, never a silently absent step.
_STEP_VERBS: dict[str, str] = {
    "trim_reads": "trimmed with",
    "align_reads": "aligned with",
    "call_variants": "variant-called with",
    "annotate_variants": "annotated with",
    "assemble_reads": "assembled with",
    "assemble_upload": "assembled with",
    "polish_assembly": "polished with",
    "scaffold_assembly": "scaffolded with",
    "quantify": "quantified with",
    "differential_expression": "tested for differential expression with",
    "consensus_from_alignment": "called a consensus with",
    "run_qc": "quality-checked with",
    "assess_completeness": "assessed for completeness with",
    "assess_misassemblies": "assessed for misassemblies with",
    "download_sra_run": "downloaded from the SRA",
    "download_assembly": "downloaded from NCBI",
    "download_uniprot": "downloaded from UniProt",
    "download_lineage": "downloaded",
}

# Registered handlers that legitimately produce no narrative step.
_NO_NARRATIVE_STEP: frozenset[str] = frozenset(
    {
        # Sidecar-only outputs, excluded at traversal anyway.
        "build_index",
        "index_bam",
        # Statistics written back onto an existing object rather than
        # producing one; the numbers already show in the file panel.
        "run_bam_stats",
        "run_vcf_stats",
        "ingest_headers",
        # Bookkeeping on bytes already ingested.
        "hash_blob",
        "register_hash",
        "verify_blob",
        "verify_files",
        # Infrastructure; touches no project data.
        "install_tool",
        "uninstall_tool",
        # Housekeeping reapers and GC.
        "gc_blobs",
        "reap_pipeline_scratch",
        "reap_report_dirs",
        "reap_uploads",
        # AI features that write a field rather than producing an object.
        "summarize_object",
        "answer_project_question",
        # Test-only.
        "noop",
        "sleep_test",
    }
)

# Fact-key convention: `<verb>_by` names the tool that did a step. This is an
# open vocabulary on purpose -- a future `polished_by` renders generically
# rather than needing a table entry. Forcing every key to have a hand-written
# phrase would turn "we have no phrase for this" into a wrong guess, which is
# worse than a clumsy sentence.
GENERIC_VERB = "processed with"
