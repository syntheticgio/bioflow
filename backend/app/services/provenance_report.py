"""Rendering a ProvenanceChain as a methods report.

This is the deliverable, not a fallback. It works with no AI provider
configured, and it is the artifact a user cites -- the prose version in
`provenance_prompt.py` is a second rendering of exactly these facts and can
never contain one this does not.

The governing rule: never omit a step known to have run, and never assert a
fact that is not there. A gap renders in the position the fact would have
occupied, so a reader scanning for "which version" sees the question asked
and unanswered rather than seeing nothing and assuming it did not matter.
"""

from app.services.provenance_walker import (
    Gap,
    GapKind,
    Node,
    ProvenanceChain,
)

_GAP_TEXT = {
    GapKind.VERSION_UNRECORDED: "**version not recorded**",
    GapKind.PARAMS_UNRECORDED: "**parameters not recorded**",
    GapKind.SHARE_BOUNDARY: (
        "**lineage continues in another profile and is not available here**"
    ),
    GapKind.DANGLING_PARENT: "**parent object no longer exists**",
    GapKind.DEPTH_EXCEEDED: "**ancestry truncated at the depth limit**",
}

# Gap kinds that describe the chain/traversal itself rather than a fact
# about a specific step -- these render in a dedicated "Limits of this
# record" section regardless of which node they happen to be attached to,
# since a root node has no step to attach a rendered marker to.
_CHAIN_LEVEL_KINDS = frozenset(
    {GapKind.SHARE_BOUNDARY, GapKind.DANGLING_PARENT, GapKind.DEPTH_EXCEEDED}
)

# What the History tab's "Not recorded" rail calls each gap. The rail lists
# gaps away from the step they belong to, so these read as standalone noun
# phrases ("Download tool and version") where `_GAP_TEXT` reads as an inline
# marker ("**version not recorded**") -- the same fact, but a sentence
# fragment cannot be lifted out of its sentence.
#
# Keyed by `(job_type, kind)`, and deliberately partial on the job_type axis:
# job types are an open vocabulary (`_STEP_VERBS` in the walker says why), so
# an unmapped one falls back to `_GAP_LABELS_BY_KIND`, which covers every
# `GapKind` and is what `test_provenance_report.py` asserts exhaustively. A
# missing entry here costs a generic phrase, never a missing rail row.
_GAP_LABELS: dict[tuple[str, GapKind], str] = {
    ("download_sra_run", GapKind.VERSION_UNRECORDED): "Download tool and version",
    ("download_assembly", GapKind.VERSION_UNRECORDED): "Download tool and version",
    ("download_uniprot", GapKind.VERSION_UNRECORDED): "Download tool and version",
    ("trim_reads", GapKind.VERSION_UNRECORDED): "Trimming tool version",
    ("trim_reads", GapKind.PARAMS_UNRECORDED): "Trimming parameters",
    ("align_reads", GapKind.VERSION_UNRECORDED): "Aligner version",
    ("align_reads", GapKind.PARAMS_UNRECORDED): "Alignment parameters",
    ("assemble_reads", GapKind.VERSION_UNRECORDED): "Assembler version",
    ("assemble_reads", GapKind.PARAMS_UNRECORDED): "Assembly parameters",
    ("call_variants", GapKind.VERSION_UNRECORDED): "Variant caller version",
    ("call_variants", GapKind.PARAMS_UNRECORDED): "Variant calling parameters",
    ("quantify", GapKind.VERSION_UNRECORDED): "Quantification tool version",
    ("quantify", GapKind.PARAMS_UNRECORDED): "Quantification parameters",
}

_GAP_LABELS_BY_KIND: dict[GapKind, str] = {
    GapKind.VERSION_UNRECORDED: "Tool version",
    GapKind.PARAMS_UNRECORDED: "Parameters",
    GapKind.SHARE_BOUNDARY: "Lineage before this profile",
    GapKind.DANGLING_PARENT: "A parent object that no longer exists",
    GapKind.DEPTH_EXCEEDED: "Ancestry beyond the depth limit",
}


def is_step_level(gap: Gap) -> bool:
    """Whether a gap belongs inline on a step row.

    The inverse of `_CHAIN_LEVEL_KINDS`: a share boundary or a dangling parent
    describes the traversal, not a fact the step failed to record, so it has no
    position within a step row and appears only in the rail.
    """
    return gap.kind not in _CHAIN_LEVEL_KINDS


def inline_gap_text(gap: Gap) -> str:
    """One gap as the History tab's inline marker, without markdown emphasis.

    `_GAP_TEXT` wraps each phrase in `**` for the markdown report; the tab
    styles the marker itself, so the asterisks would render literally.
    """
    return _GAP_TEXT[gap.kind].strip("*")


def gap_label(gap: Gap, job_type: str | None) -> str:
    """The rail's name for one gap."""
    if job_type is not None:
        specific = _GAP_LABELS.get((job_type, gap.kind))
        if specific is not None:
            return specific
    return _GAP_LABELS_BY_KIND[gap.kind]


def _gaps_for(chain: ProvenanceChain, node_id) -> list[Gap]:
    return [g for g in chain.gaps if g.object_id == node_id]


def _describe_step(node: Node, gaps: list[Gap]) -> str:
    step = node.produced_by
    if step is None:
        line = f"**Input:** `{node.name}`"
        # A root normally carries no step-shaped gap -- an uploaded FASTQ
        # legitimately has no producing job, so silence is correct. But if
        # a VERSION_UNRECORDED or PARAMS_UNRECORDED gap was still attached
        # to this object id (e.g. hand-built data, or a future producer
        # that predates a step), the fact must still surface rather than
        # vanish behind the "Input" label.
        kinds = {g.kind for g in gaps}
        markers = [
            _GAP_TEXT[k]
            for k in (GapKind.VERSION_UNRECORDED, GapKind.PARAMS_UNRECORDED)
            if k in kinds
        ]
        if markers:
            line += " — " + ", ".join(markers)
        return line

    kinds = {g.kind for g in gaps}
    parts = [step.verb]

    if step.tool:
        parts.append(step.tool)
    else:
        parts.append("an unrecorded tool")

    if step.tool_version:
        parts.append(step.tool_version)
    elif GapKind.VERSION_UNRECORDED in kinds:
        parts.append(f"({_GAP_TEXT[GapKind.VERSION_UNRECORDED]})")

    line = f"**{node.name}** — {' '.join(parts)}"

    if step.ran_at:
        line += f", {step.ran_at:%Y-%m-%d}"
    if step.outcome and step.outcome != "success":
        # Failures are in the chain deliberately; saying so is the point.
        line += f" — run outcome: {step.outcome}"

    if step.params:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(step.params.items()))
        line += f"\n  - Parameters: {rendered}"
    elif GapKind.PARAMS_UNRECORDED in kinds:
        line += (
            f"\n  - {_GAP_TEXT[GapKind.PARAMS_UNRECORDED]}"
            f" (job: {step.job_type})"
        )

    return line


def render_markdown(chain: ProvenanceChain) -> str:
    lines: list[str] = ["## Provenance", ""]

    if chain.gap_count:
        lines.append(f"_{chain.gap_count} facts not recorded._")
    else:
        lines.append("_All facts recorded._")
    lines.append("")

    spine = [
        chain.nodes[oid]
        for oid in chain.order
        if chain.nodes[oid].kind == "spine"
    ]
    supporting = [
        chain.nodes[oid]
        for oid in chain.order
        if chain.nodes[oid].kind == "supporting"
    ]

    lines.append("### Steps")
    lines.append("")
    for node in spine:
        lines.append(f"- {_describe_step(node, _gaps_for(chain, node.object_id))}")

    if chain.branches:
        lines.append("")
        for branch in chain.branches:
            names = ", ".join(
                f"`{chain.nodes[b].name}`" for b in branch if b in chain.nodes
            )
            lines.append(
                f"- _This step combined two inputs (a branch in the lineage): {names}._"
            )

    if supporting:
        lines.append("")
        lines.append("### Materials")
        lines.append("")
        for node in supporting:
            lines.append(
                f"- {_describe_step(node, _gaps_for(chain, node.object_id))}"
            )

    chain_level = [g for g in chain.gaps if g.kind in _CHAIN_LEVEL_KINDS]
    if chain_level:
        lines.append("")
        lines.append("### Limits of this record")
        lines.append("")
        for gap in chain_level:
            text = _GAP_TEXT[gap.kind]
            if gap.detail:
                text += f" (id: {gap.detail})"
            lines.append(f"- {text}")

    return "\n".join(lines)
