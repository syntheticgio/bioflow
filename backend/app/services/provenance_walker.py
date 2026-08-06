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

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

from beanie import PydanticObjectId

from app.models.job import Job
from app.models.object import DataObject, ObjectRole
from app.services import object_service, timing_service

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

def extract_tool_facts(facts: dict) -> tuple[str | None, str | None]:
    """The tool that produced an object and its version, read by convention.

    The `*_provenance` builders in `queue/results.py` all follow the same
    shape: `<verb>_by` names the tool, and a sibling key ending `_version`
    carries its version. Reading by convention rather than from a fixed key
    list means a builder added later still surfaces its tool here.
    """
    tool = None
    for key, value in sorted(facts.items()):
        if not key.endswith("_by"):
            continue
        if isinstance(value, str) and value:
            tool = value
            break

    version = None
    for key, value in sorted(facts.items()):
        if key.endswith("_version") and isinstance(value, str) and value:
            version = value
            break

    return tool, version


def extract_params(facts: dict) -> dict:
    """Parameters recorded for the step that produced an object.

    Same convention: `align_params`, `trim_params`, `count_params`.
    """
    for key, value in sorted(facts.items()):
        if key.endswith("_params") and isinstance(value, dict) and value:
            return value
    return {}


# Fact keys naming a parent that is a material rather than a step. These are
# written by the `*_provenance` builders, which already distinguish their
# parents by role -- this reads a distinction the data already makes rather
# than inventing one.
_SUPPORTING_PARENT_KEYS = ("reference_object_id", "annotation_object_id")

# Roles that are materials when the facts do not say. Used only as a
# fallback, for objects predating their step's provenance builder.
_SUPPORTING_ROLES = frozenset({ObjectRole.REFERENCE, ObjectRole.ANNOTATION})


def classify_parent(
    parent_id: PydanticObjectId,
    *,
    facts: dict,
    role: ObjectRole | None,
) -> Literal["spine", "supporting"]:
    """Whether a parent is part of the specimen lineage or a material used.

    Facts win over role: a step that recorded `reference_object_id` is making
    an explicit claim about how that parent was used, while role is a
    property of the object in isolation.

    The default is `spine`. That direction matters: a misclassified spine
    parent appears in the materials list (visible, mildly wrong), while a
    misclassified material would drop out of the step sequence entirely.
    """
    for key in _SUPPORTING_PARENT_KEYS:
        value = facts.get(key)
        if value and str(value) == str(parent_id):
            return "supporting"

    if role is not None and role in _SUPPORTING_ROLES:
        return "supporting"

    return "spine"


async def walk(
    object_id: PydanticObjectId, *, owner: str
) -> ProvenanceChain:
    """Everything that produced `object_id`, walked to the roots.

    Breadth-first up `derived_from`, memoized by object id. The memo is not
    an optimization: the DAG reconverges (a reference is reachable via both
    the alignment and the variant call) and a node must render once.

    `object_service.get_object` authorizes before anything else is read --
    ancestors are fetched by id without their own owner check, so the target
    check is what stands between a request and another profile's lineage.
    """
    target_obj = await object_service.get_object(object_id, owner=owner)

    gaps: list[Gap] = []
    branches: list[tuple[PydanticObjectId, ...]] = []
    order: list[PydanticObjectId] = []
    # object_id -> (name, role, produced_by, parents), assembled first and
    # turned into Nodes only after every edge has been seen -- a node's own
    # kind can be decided by an edge encountered after the node itself was
    # visited (the reconvergent-reference case), so kind is resolved from
    # `kinds` at the very end rather than baked in during the BFS.
    raw: dict[PydanticObjectId, tuple[str, ObjectRole | None, Step | None, tuple[PydanticObjectId, ...]]] = {}

    queue: deque[tuple[DataObject, int]] = deque([(target_obj, 0)])
    seen: set[PydanticObjectId] = {target_obj.id}
    # Every node starts as spine; a parent edge can demote it to supporting.
    kinds: dict[PydanticObjectId, Literal["spine", "supporting"]] = {
        target_obj.id: "spine"
    }

    while queue:
        obj, depth = queue.popleft()

        if depth >= MAX_DEPTH:
            gaps.append(Gap(kind=GapKind.DEPTH_EXCEEDED, object_id=obj.id))
            continue

        step, step_gaps = await _step_for(obj)
        gaps.extend(step_gaps)

        # A shared copy has its lineage deliberately cleared: `derived_from`
        # and `produced_by_job` name objects and jobs in the sender's
        # partition, which owner-scoped lookups here can never resolve.
        if obj.shared_from is not None:
            gaps.append(Gap(kind=GapKind.SHARE_BOUNDARY, object_id=obj.id))

        parent_ids: list[PydanticObjectId] = []
        spine_parents: list[PydanticObjectId] = []

        for parent_id in obj.derived_from:
            parent = await DataObject.get(parent_id)
            if parent is None:
                gaps.append(
                    Gap(
                        kind=GapKind.DANGLING_PARENT,
                        object_id=obj.id,
                        detail=str(parent_id),
                    )
                )
                continue

            # Biologically inert scaffolding: a methods paragraph mentioning
            # BAM index construction is noise. Skipped here rather than
            # filtered at render, so it never reaches the prompt.
            if parent.sidecar_of is not None:
                continue

            parent_ids.append(parent_id)
            kind = classify_parent(parent_id, facts=obj.facts, role=parent.role)

            # Supporting wins if any edge says so: a reference reached once as
            # a material is a material, however else it is reachable.
            if parent_id not in kinds or kind == "supporting":
                kinds[parent_id] = kind
            if kind == "spine":
                spine_parents.append(parent_id)

            if parent_id not in seen:
                seen.add(parent_id)
                queue.append((parent, depth + 1))

        if len(spine_parents) > 1:
            branches.append(tuple(spine_parents))

        raw[obj.id] = (obj.name, obj.role, step, tuple(parent_ids))
        order.append(obj.id)

    nodes: dict[PydanticObjectId, Node] = {
        oid: Node(
            object_id=oid,
            name=name,
            role=role,
            kind=kinds.get(oid, "spine"),
            produced_by=step,
            parents=parents,
        )
        for oid, (name, role, step, parents) in raw.items()
    }

    return ProvenanceChain(
        target=nodes[target_obj.id],
        nodes=nodes,
        # Oldest first: a methods section reads forward through time.
        order=tuple(reversed(order)),
        gaps=tuple(gaps),
        branches=tuple(branches),
    )


async def _step_for(obj: DataObject) -> tuple[Step | None, list[Gap]]:
    """The job that produced `obj`, if any, plus whatever it failed to record.

    Returns `(None, [])` for a root. A root is not a gap: an uploaded FASTQ
    legitimately has no producing job, and counting it would inflate the gap
    number with something no user can act on.
    """
    if obj.produced_by_job is None:
        return None, []

    gaps: list[Gap] = []
    job = await Job.get(obj.produced_by_job)
    job_type = job.type if job is not None else "unknown"

    tool, version = extract_tool_facts(obj.facts)
    params = extract_params(obj.facts)

    if tool is not None and version is None:
        gaps.append(Gap(kind=GapKind.VERSION_UNRECORDED, object_id=obj.id))
    if not params:
        gaps.append(Gap(kind=GapKind.PARAMS_UNRECORDED, object_id=obj.id))

    ran_at = None
    outcome = None
    rows = await timing_service.records_for_object(str(obj.id), limit=50)
    for row in rows:
        # `records_for_object` includes failures deliberately -- a step that
        # failed and was retried is exactly what a methods reader needs.
        if row.job_id == str(obj.produced_by_job):
            ran_at = row.finished_at
            outcome = row.outcome
            if tool is None:
                tool = row.tool
            if version is None:
                version = row.tool_version
            break

    step = Step(
        job_type=job_type,
        verb=_STEP_VERBS.get(job_type, GENERIC_VERB),
        tool=tool,
        tool_version=version,
        params=params,
        ran_at=ran_at,
        outcome=outcome,
        gaps=tuple(gaps),
    )
    return step, gaps
