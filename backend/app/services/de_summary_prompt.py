"""Turning a differential-expression run's facts into a prompt worth answering.

Same selection-policy problem as summary_prompt.py, applied to DeFacts instead
of QC facts: the aggregate counts are always safe to restate, and the top-N
gene table is where a wrong log2FC or a hallucinated gene name would do the
most damage. Every number handed to the model comes straight from
`de_runner._facts()` / the DE results object's `facts` dict -- nothing here
computes a statistic, it only selects and phrases.
"""

from typing import Any


def _num(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.2f}"


def _sci(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return f"{value:.2e}"


DE_SYSTEM_PROMPT = (
    "You are a bioinformatics core facility analyst writing a short note "
    "about a differential expression result for the scientist who ran it.\n\n"
    "Write 2-4 sentences of plain prose. No headings, no bullet points, no "
    "markdown, no preamble such as 'Here is a summary'. Start directly with "
    "the substance.\n\n"
    "What to focus on, in order of importance:\n"
    "1. How many genes were significantly up- or down-regulated, and the "
    "overall shape of the result (a handful of hits vs. a broad response).\n"
    "2. Whether the sample groups separated cleanly, when that is stated.\n"
    "3. One or two of the most significant genes by name, if genes are "
    "given, stated with their direction and magnitude.\n\n"
    "Rules you must follow:\n"
    "- Only use the numbers given to you. Never invent a gene, a fold "
    "change, a p-value, or a biological function or pathway the data does "
    "not state.\n"
    "- Do not recommend specific software, thresholds, or follow-up "
    "analyses.\n"
    "- If the result looks unremarkable, say so plainly and briefly. Do not "
    "manufacture significance to fill space.\n"
    "- Do not restate every gene given to you. Cite only the one or two "
    "that carry your point."
)


def _top_gene_lines(top_genes: list[dict]) -> list[str]:
    lines = []
    for i, gene in enumerate(top_genes, start=1):
        name = gene.get("gene")
        label = name if isinstance(name, str) and name.strip() else (
            f"an unnamed transcript, ranked {i} by significance"
        )
        log2fc = _num(gene.get("log2fc"))
        padj = _sci(gene.get("padj"))
        parts = [label]
        if log2fc is not None:
            parts.append(f"log2 fold change {log2fc}")
        if padj is not None:
            parts.append(f"adjusted p-value {padj}")
        lines.append(f"- {', '.join(parts)}")
    return lines


def build_de_user_prompt(*, facts: dict, top_genes: list[dict]) -> str | None:
    """Assemble the DE summary prompt, or None when there is nothing to say.

    A run with zero significant genes and no top genes to name has no
    narrative in it -- the aggregate counts alone ("0 up, 0 down") are not
    worth a model call.
    """
    significant_up = facts.get("significant_up") or 0
    significant_down = facts.get("significant_down") or 0
    if not significant_up and not significant_down and not top_genes:
        return None

    sections: list[str] = []

    header = []
    test = facts.get("contrast_test")
    reference = facts.get("contrast_reference")
    if test and reference:
        header.append(f"Contrast: {test} vs. {reference}")
    if facts.get("alpha") is not None:
        header.append(f"Significance threshold (alpha): {facts['alpha']}")
    if facts.get("samples") is not None:
        header.append(f"Samples: {_num(facts['samples'])}")
    by_condition = facts.get("samples_by_condition")
    if isinstance(by_condition, dict) and by_condition:
        parts = ", ".join(f"{k}: {v}" for k, v in by_condition.items())
        header.append(f"Samples per condition: {parts}")
    if header:
        sections.append("\n".join(header))

    counts = [
        f"- Genes tested: {_num(facts.get('genes_tested'))}",
        f"- Significantly upregulated: {_num(significant_up)}",
        f"- Significantly downregulated: {_num(significant_down)}",
    ]
    sections.append("Result counts:\n" + "\n".join(counts))

    if top_genes:
        sections.append(
            "Most significant genes, ranked by adjusted p-value:\n"
            + "\n".join(_top_gene_lines(top_genes))
        )

    sections.append(
        "Write the note now, following every rule in your instructions."
    )
    return "\n\n".join(sections)
