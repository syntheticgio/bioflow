"""Turning a VCF's call-set statistics into a prompt worth answering.

Same selection-policy shape as de_summary_prompt.py: the aggregate
VcfStatsFacts (Ti/Tv, QUAL/depth shape, consequence breakdown) are always safe
to restate, and the top-N-by-severity variant list is where a wrong
gene/position/consequence attribution would do the most damage. Nothing here
calls a variant pathogenic or benign -- that is a clinical judgement this
feature must never make, and the system prompt says so explicitly.
"""

from typing import Any


def _num(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.2f}"


VARIANT_SYSTEM_PROMPT = (
    "You are a bioinformatics core facility analyst writing a short note "
    "about a variant-calling result for the scientist who ran it.\n\n"
    "Write 2-4 sentences of plain prose. No headings, no bullet points, no "
    "markdown, no preamble such as 'Here is a summary'. Start directly with "
    "the substance.\n\n"
    "What to focus on, in order of importance:\n"
    "1. The size and overall shape of the call set: how many variants, and "
    "whether the Ti/Tv ratio and QUAL/depth figures look typical or "
    "unusual, when you are given enough to judge that.\n"
    "2. The mix of consequence types, when given -- for example whether "
    "loss-of-function consequences like stop-gained or frameshift variants "
    "are present.\n"
    "3. One or two of the most severe variants by name, if given, stated "
    "with their gene and consequence type.\n\n"
    "Rules you must follow:\n"
    "- Only use the numbers and annotations given to you. Never invent a "
    "gene, a position, a consequence, or a claim about disease risk.\n"
    "- Never call a variant pathogenic, benign, or clinically significant. "
    "That is not a judgement this data supports, and not one you are asked "
    "to make.\n"
    "- Do not recommend specific software, filters, or follow-up analyses.\n"
    "- If the call set looks unremarkable, say so plainly and briefly. Do "
    "not manufacture concern to fill space.\n"
    "- Do not restate every variant given to you. Cite only the one or two "
    "that carry your point."
)


def _top_variant_lines(top_variants: list[dict]) -> list[str]:
    lines = []
    for variant in top_variants:
        name = variant.get("gene")
        position = variant.get("position") or "an unspecified position"
        consequence = variant.get("consequence") or "an unspecified consequence"
        label = (
            f"{name} ({consequence})"
            if isinstance(name, str) and name.strip()
            else f"an intergenic variant at {position} ({consequence})"
        )
        lines.append(f"- {label}, {position}" if isinstance(name, str) and name.strip() else f"- {label}")
    return lines


def build_variant_user_prompt(*, facts: dict, top_variants: list[dict]) -> str | None:
    """Assemble the variant summary prompt, or None when there is nothing to say.

    A call set with zero variants and no severe variants to name has no
    narrative in it.
    """
    summary = facts.get("vcf_stats_summary") or {}
    variant_count = summary.get("variants") or 0
    if not variant_count and not top_variants:
        return None

    sections: list[str] = []

    counts = [f"- Total variants: {_num(variant_count)}"]
    if summary.get("ti_tv_ratio") is not None:
        counts.append(f"- Ti/Tv ratio: {_num(summary['ti_tv_ratio'])}")
    if summary.get("mean_qual") is not None:
        counts.append(f"- Mean QUAL: {_num(summary['mean_qual'])}")
    if summary.get("mean_depth") is not None:
        counts.append(f"- Mean depth: {_num(summary['mean_depth'])}x")
    sections.append("Call set statistics:\n" + "\n".join(counts))

    consequence_counts = facts.get("consequence_counts")
    if isinstance(consequence_counts, dict) and consequence_counts:
        lines = [
            f"- {name}: {_num(count)}"
            for name, count in sorted(
                consequence_counts.items(), key=lambda kv: -(kv[1] or 0)
            )
        ]
        sections.append("Consequence types observed:\n" + "\n".join(lines))

    if top_variants:
        sections.append(
            "Most severe variants, ranked by consequence severity:\n"
            + "\n".join(_top_variant_lines(top_variants))
        )

    sections.append(
        "Write the note now, following every rule in your instructions."
    )
    return "\n\n".join(sections)
