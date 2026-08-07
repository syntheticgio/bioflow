# DE and Variant-Call AI Summaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two new AI-generated narrative summaries -- one for differential-expression results, one for variant-call results -- that render as prose above each result page's plots/tables, following the exact pattern the existing `FILE_SUMMARY` slot already uses for QC data.

**Architecture:** Two new `TaskSlot` members (`DE_SUMMARY`, `VARIANT_SUMMARY`), each with its own fact-selection prompt module (mirroring `summary_prompt.py`), its own THREAD-mode queue handler (mirroring `summary_handlers.py`'s `summarize_object`), and its own chained-launch call site (mirroring how `_apply_run_qc` chains into `pipeline_service.launch_summary` today). The frontend `AiSummary` component grows two optional props so both new slots reuse it instead of duplicating its stale/available/regenerate logic.

**Tech Stack:** Python (FastAPI, Beanie/MongoDB, existing queue/handler registry), React + TypeScript, TanStack Query.

---

## File Structure

New/modified files, each with one responsibility:

- Modify: `backend/app/models/ai.py` -- add `DE_SUMMARY` and `VARIANT_SUMMARY` to `TaskSlot` and their `_SLOT_LABELS` entries.
- Create: `backend/app/services/de_summary_prompt.py` -- fact selection + system prompt for DE summaries.
- Create: `backend/app/services/variant_summary_prompt.py` -- fact selection + system prompt for variant summaries.
- Create: `backend/app/queue/de_summary_handlers.py` -- THREAD handler `summarize_de_results`.
- Create: `backend/app/queue/variant_summary_handlers.py` -- THREAD handler `summarize_variant_results`.
- Modify: `backend/app/services/pipeline_service.py` -- add `launch_de_summary(...)` and `launch_variant_summary(...)`, mirroring `launch_summary`.
- Modify: `backend/app/queue/results.py` -- `_apply_differential_expression` chains into `launch_de_summary`; `_apply_run_vcf_stats` chains into `launch_variant_summary`. Add `_apply_summarize_de_results` and `_apply_summarize_variant_results` appliers, registered in the dispatch dict.
- Modify: `backend/app/queue/handlers.py` -- import the two new handler modules for their `@handler` registration side effects (same reason `summary_handlers` and `variant_handlers` are imported there today).
- Modify: `backend/app/api/v1/pipelines.py` -- add `GET /pipelines/de-summary/status`, `POST /pipelines/de-summary`, `GET /pipelines/variant-summary/status`, `POST /pipelines/variant-summary` endpoints, mirroring the existing `/pipelines/summary/*` pair.
- Modify: `frontend/src/api/types.ts` -- add `DeSummaryFacts` and `VariantSummaryFacts` interfaces (same shape as `AiSummaryFacts`, different key prefix).
- Modify: `frontend/src/api/client.ts` -- add `deSummaryStatus`, `launchDeSummary`, `variantSummaryStatus`, `launchVariantSummary`.
- Modify: `frontend/src/components/AiSummary.tsx` -- generalize to accept a `factPrefix` and API function props so DE/variant summaries reuse it instead of forking it.
- Modify: `frontend/src/components/ExpressionResults.tsx` -- render the generalized summary component above the PCA/volcano/MA plots.
- Modify: `frontend/src/components/VariantResults.tsx` -- render the generalized summary component above the Ti/Tv/QUAL charts and variant table.
- Create: `backend/tests/services/test_de_summary_prompt.py` -- mirrors `test_summary_prompt.py`'s selection-policy tests.
- Create: `backend/tests/services/test_variant_summary_prompt.py` -- same, for variant facts.
- Create: `backend/tests/queue/test_de_summary_handler.py` -- mirrors `test_summary_handler.py`.
- Create: `backend/tests/queue/test_variant_summary_handler.py` -- same.

---

## Task 1: Add the two new TaskSlots

**Files:**
- Modify: `backend/app/models/ai.py`
- Test: `backend/tests/models/test_ai_task_slot.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/models/test_ai_task_slot.py
from app.models.ai import TaskSlot


def test_de_summary_slot_has_a_label():
    assert TaskSlot.DE_SUMMARY.label == "Differential expression summaries"


def test_variant_summary_slot_has_a_label():
    assert TaskSlot.VARIANT_SUMMARY.label == "Variant call summaries"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/models/test_ai_task_slot.py -v`
Expected: FAIL with `AttributeError: DE_SUMMARY`

- [ ] **Step 3: Add the slots**

In `backend/app/models/ai.py`, extend `TaskSlot` and `_SLOT_LABELS`:

```python
class TaskSlot(StrEnum):
    """An AI-using feature that can be pointed at a provider.

    The `label` is what the settings page shows. It lives here rather than in
    the frontend so that adding a slot is a one-place change.
    """

    FILE_SUMMARY = "file_summary"
    ORGANISM_BLURB = "organism_blurb"
    DE_SUMMARY = "de_summary"
    VARIANT_SUMMARY = "variant_summary"

    @property
    def label(self) -> str:
        return _SLOT_LABELS[self]


_SLOT_LABELS = {
    TaskSlot.FILE_SUMMARY: "File summaries",
    TaskSlot.ORGANISM_BLURB: "Organism blurbs",
    TaskSlot.DE_SUMMARY: "Differential expression summaries",
    TaskSlot.VARIANT_SUMMARY: "Variant call summaries",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec api python -m pytest tests/models/test_ai_task_slot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/ai.py backend/tests/models/test_ai_task_slot.py
git commit -m "feat: add DE_SUMMARY and VARIANT_SUMMARY task slots"
```

---

## Task 2: DE summary prompt module

**Files:**
- Create: `backend/app/services/de_summary_prompt.py`
- Test: `backend/tests/services/test_de_summary_prompt.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/services/test_de_summary_prompt.py
"""What goes into the DE summary prompt.

Same discipline as test_summary_prompt.py: assert the model is given exact
numbers to restate, and that a missing gene symbol is described rather than
silently dropped.
"""

from app.services.de_summary_prompt import build_de_user_prompt


def _de_facts(**overrides) -> dict:
    base = {
        "contrast_test": "treated",
        "contrast_reference": "control",
        "alpha": 0.05,
        "samples": 6,
        "samples_by_condition": {"treated": 3, "control": 3},
        "genes_in_matrix": 18000,
        "genes_tested": 15200,
        "significant_genes": 231,
        "significant_up": 142,
        "significant_down": 89,
        "pydeseq2_version": "0.4.10",
    }
    base.update(overrides)
    return base


def _top_genes(n: int = 3) -> list[dict]:
    return [
        {"gene": "TP53", "log2fc": -2.31, "padj": 1.2e-8},
        {"gene": "MYC", "log2fc": 3.05, "padj": 4.5e-7},
        {"gene": None, "log2fc": 1.11, "padj": 2.0e-4},
    ][:n]


class TestAggregateFacts:
    def test_the_significant_gene_counts_are_present(self):
        prompt = build_de_user_prompt(facts=_de_facts(), top_genes=_top_genes())
        assert "142" in prompt
        assert "89" in prompt

    def test_the_contrast_is_named(self):
        prompt = build_de_user_prompt(facts=_de_facts(), top_genes=_top_genes())
        assert "treated" in prompt
        assert "control" in prompt

    def test_no_significant_genes_and_no_top_genes_returns_none(self):
        """Nothing worth narrating -- asking anyway invites invented findings."""
        facts = _de_facts(significant_genes=0, significant_up=0, significant_down=0)
        prompt = build_de_user_prompt(facts=facts, top_genes=[])
        assert prompt is None


class TestTopGenes:
    def test_named_genes_carry_their_log2fc_and_padj(self):
        prompt = build_de_user_prompt(facts=_de_facts(), top_genes=_top_genes())
        assert "TP53" in prompt
        assert "-2.31" in prompt

    def test_a_gene_with_no_symbol_gets_a_generic_descriptor_not_omission(self):
        prompt = build_de_user_prompt(facts=_de_facts(), top_genes=_top_genes())
        assert "unnamed transcript" in prompt.lower()


class TestSystemPrompt:
    def test_a_system_prompt_exists_and_forbids_recommendations(self):
        from app.services.de_summary_prompt import DE_SYSTEM_PROMPT

        assert "recommend" in DE_SYSTEM_PROMPT.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec api python -m pytest tests/services/test_de_summary_prompt.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.de_summary_prompt'`

- [ ] **Step 3: Write the prompt module**

```python
# backend/app/services/de_summary_prompt.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/services/test_de_summary_prompt.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/de_summary_prompt.py backend/tests/services/test_de_summary_prompt.py
git commit -m "feat: add DE summary prompt builder"
```

---

## Task 3: Variant summary prompt module

**Files:**
- Create: `backend/app/services/variant_summary_prompt.py`
- Test: `backend/tests/services/test_variant_summary_prompt.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/services/test_variant_summary_prompt.py
"""What goes into the variant-call summary prompt.

Mirrors test_de_summary_prompt.py: aggregate VcfStatsFacts are always safe to
restate, and the top-N-by-severity list is where a wrong gene/position
attribution would do the most damage.
"""

from app.services.variant_summary_prompt import build_variant_user_prompt


def _vcf_stats_facts(**overrides) -> dict:
    base = {
        "vcf_stats_summary": {
            "variants": 48213,
            "ti_tv_ratio": 2.14,
            "mean_qual": 812.3,
            "mean_depth": 34.2,
        },
        "filters": ["PASS", "LowQual"],
        "sample_count": 1,
        "reference_count": 24,
        "consequence_counts": {
            "missense_variant": 1203,
            "synonymous_variant": 980,
            "stop_gained": 4,
        },
    }
    base.update(overrides)
    return base


def _top_variants(n: int = 3) -> list[dict]:
    return [
        {"gene": "BRCA1", "position": "chr17:43094464", "consequence": "stop_gained"},
        {"gene": "TTN", "position": "chr2:178525989", "consequence": "frameshift_variant"},
        {"gene": None, "position": "chr7:1200000", "consequence": "missense_variant"},
    ][:n]


class TestAggregateFacts:
    def test_the_ti_tv_ratio_is_present(self):
        prompt = build_variant_user_prompt(
            facts=_vcf_stats_facts(), top_variants=_top_variants()
        )
        assert "2.14" in prompt

    def test_the_consequence_breakdown_is_present(self):
        prompt = build_variant_user_prompt(
            facts=_vcf_stats_facts(), top_variants=_top_variants()
        )
        assert "missense_variant" in prompt
        assert "1,203" in prompt or "1203" in prompt

    def test_too_few_variants_to_characterize_returns_none(self):
        facts = _vcf_stats_facts(
            vcf_stats_summary={"variants": 0, "ti_tv_ratio": None}
        )
        prompt = build_variant_user_prompt(facts=facts, top_variants=[])
        assert prompt is None


class TestTopVariants:
    def test_named_variants_carry_their_position_and_consequence(self):
        prompt = build_variant_user_prompt(
            facts=_vcf_stats_facts(), top_variants=_top_variants()
        )
        assert "BRCA1" in prompt
        assert "stop_gained" in prompt

    def test_an_unannotated_variant_gets_a_generic_descriptor_not_omission(self):
        prompt = build_variant_user_prompt(
            facts=_vcf_stats_facts(), top_variants=_top_variants()
        )
        assert "intergenic" in prompt.lower()


class TestSystemPrompt:
    def test_a_system_prompt_exists_and_forbids_pathogenicity_claims(self):
        from app.services.variant_summary_prompt import VARIANT_SYSTEM_PROMPT

        assert "pathogenic" in VARIANT_SYSTEM_PROMPT.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec api python -m pytest tests/services/test_variant_summary_prompt.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.variant_summary_prompt'`

- [ ] **Step 3: Write the prompt module**

```python
# backend/app/services/variant_summary_prompt.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/services/test_variant_summary_prompt.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/variant_summary_prompt.py backend/tests/services/test_variant_summary_prompt.py
git commit -m "feat: add variant summary prompt builder"
```

---

## Task 4: DE summary queue handler

**Files:**
- Create: `backend/app/queue/de_summary_handlers.py`
- Modify: `backend/app/queue/handlers.py`
- Test: `backend/tests/queue/test_de_summary_handler.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/queue/test_de_summary_handler.py
"""The DE summary job's failure style. Mirrors test_summary_handler.py."""

import pytest

from app.errors import PermanentError
from app.models.ai import FailureReason, ProviderKind
from app.queue import de_summary_handlers
from app.queue.registry import JobContext
from app.queue.de_summary_handlers import summarize_de_results
from app.services import de_summary_prompt
from app.services.ai.adapters import Completion, Failure
from app.services.ai.router import ResolvedProvider


def _ctx(payload: dict) -> JobContext:
    return JobContext(job_id="job-1", payload=payload, epoch=1, attempts=1, owner="local")


def _payload(**overrides) -> dict:
    base = {
        "object_id": "obj-1",
        "facts": {
            "significant_up": 142,
            "significant_down": 89,
            "contrast_test": "treated",
            "contrast_reference": "control",
        },
        "top_genes": [{"gene": "TP53", "log2fc": -2.3, "padj": 1e-8}],
        "facts_fingerprint": "abc123",
    }
    base.update(overrides)
    return base


def _fake_provider():
    return ResolvedProvider(
        provider_id="000000000000000000000000",
        name="Test",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1",
        api_key=None,
        model="test-model",
        models_cache=[],
    )


class TestSkips:
    def test_no_provider_is_a_success_with_a_reason_not_a_failure(self, monkeypatch):
        monkeypatch.setattr(de_summary_handlers, "_resolve_sync", lambda: None)
        result = summarize_de_results(_ctx(_payload()))
        assert result["skipped"] == "no_provider"

    def test_a_result_with_nothing_to_say_is_skipped_before_the_model_is_called(
        self, monkeypatch
    ):
        monkeypatch.setattr(de_summary_handlers, "_resolve_sync", lambda: _fake_provider())

        def must_not_run(*a, **k):
            raise AssertionError("the model must not be called with no prompt")

        monkeypatch.setattr(de_summary_handlers, "_complete", must_not_run)
        result = summarize_de_results(
            _ctx(_payload(facts={"significant_up": 0, "significant_down": 0}, top_genes=[]))
        )
        assert result["skipped"] == "insufficient_data"


class TestSuccess:
    def test_a_generated_summary_carries_its_model_and_fingerprint(self, monkeypatch):
        monkeypatch.setattr(de_summary_handlers, "_resolve_sync", lambda: _fake_provider())
        monkeypatch.setattr(
            de_summary_handlers,
            "_complete",
            lambda p, **kw: Completion("142 genes were upregulated.", "test-model"),
        )
        result = summarize_de_results(_ctx(_payload()))
        assert result["summary"] == "142 genes were upregulated."
        assert result["model"] == "test-model"
        assert result["facts_fingerprint"] == "abc123"


class TestPayloadValidation:
    def test_a_payload_with_no_object_is_permanently_bad(self):
        with pytest.raises(PermanentError):
            summarize_de_results(_ctx({"facts": {}}))


class TestFailureReasons:
    def test_a_failure_is_reported_in_the_result(self, monkeypatch):
        monkeypatch.setattr(de_summary_handlers, "_resolve_sync", lambda: _fake_provider())
        monkeypatch.setattr(
            de_summary_handlers,
            "_complete",
            lambda p, **kw: Failure(FailureReason.INVALID_KEY),
        )
        result = summarize_de_results(_ctx(_payload()))
        assert result["skipped"] == "invalid_key"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec api python -m pytest tests/queue/test_de_summary_handler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.queue.de_summary_handlers'`

- [ ] **Step 3: Write the handler**

```python
# backend/app/queue/de_summary_handlers.py
"""The DE narrative-summary job. Mirrors summary_handlers.py exactly, pointed
at DE_SUMMARY instead of FILE_SUMMARY and de_summary_prompt instead of
summary_prompt. See summary_handlers.py's module docstring for why this is
THREAD mode and why the whole job is best-effort.
"""

import importlib

from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.queue.registry import HandlerMode, JobContext, handler
from app.services import de_summary_prompt
from app.services.ai.adapters import Completion

# See summary_handlers.py for why this goes through importlib rather than a
# normal import: app.services.ai's __init__ shadows the `complete` submodule
# with the re-exported function of the same name.
ai_complete = importlib.import_module("app.services.ai.complete")

log = get_logger(__name__)


def _resolve_sync():
    from app.db.client import run_from_thread
    from app.models.ai import TaskSlot
    from app.services.ai import router

    return run_from_thread(router.resolve(TaskSlot.DE_SUMMARY))


def _complete(provider, **kwargs):
    return ai_complete.complete_sync(provider, **kwargs)


@handler(
    "summarize_de_results",
    mode=HandlerMode.THREAD,
    job_class=JobClass.USER_BACKGROUND,
    resources=JobResources(cpu=0, mem_mb=64, io=IoClass.LIGHT),
    max_attempts=2,
)
def summarize_de_results(ctx: JobContext) -> dict:
    """Generate a short narrative summary of a differential-expression run.

    Receives everything it needs in the payload, same reasoning as
    summarize_object: this handler runs in a thread and cannot reach the
    database. The caller assembles the payload on the event loop; see
    pipeline_service.launch_de_summary.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        from app.errors import PermanentError

        raise PermanentError("summarize_de_results requires an 'object_id'")

    ctx.check_cancel()

    provider = _resolve_sync()
    if provider is None:
        log.info("de_summary_skipped_no_provider", object_id=object_id)
        return {"object_id": object_id, "skipped": "no_provider"}

    prompt = de_summary_prompt.build_de_user_prompt(
        facts=ctx.payload.get("facts") or {},
        top_genes=ctx.payload.get("top_genes") or [],
    )
    if prompt is None:
        log.info("de_summary_skipped_insufficient_data", object_id=object_id)
        return {"object_id": object_id, "skipped": "insufficient_data"}

    ctx.check_cancel()
    ctx.extend_lease(int(_timeout_seconds()) + 60)

    result = _complete(
        provider, system=de_summary_prompt.DE_SYSTEM_PROMPT, user=prompt
    )
    if not isinstance(result, Completion):
        log.info("de_summary_not_generated", object_id=object_id, reason=result.reason)
        return {"object_id": object_id, "skipped": str(result.reason)}

    text, model = result.text, result.model
    log.info("de_summary_generated", object_id=object_id, model=model, chars=len(text))
    return {
        "object_id": object_id,
        "summary": text,
        "model": model,
        "facts_fingerprint": ctx.payload.get("facts_fingerprint"),
    }


def _timeout_seconds() -> float:
    from app.config import settings

    return settings.llm_timeout_seconds
```

- [ ] **Step 4: Register the handler's import for its `@handler` side effect**

In `backend/app/queue/handlers.py`, find the existing import of `summary_handlers` (search for `import summary_handlers` or similar in the imports block) and add a line immediately after it:

```python
from app.queue import de_summary_handlers  # noqa: F401 - registers summarize_de_results
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/queue/test_de_summary_handler.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/de_summary_handlers.py backend/app/queue/handlers.py backend/tests/queue/test_de_summary_handler.py
git commit -m "feat: add DE summary queue handler"
```

---

## Task 5: Variant summary queue handler

**Files:**
- Create: `backend/app/queue/variant_summary_handlers.py`
- Modify: `backend/app/queue/handlers.py`
- Test: `backend/tests/queue/test_variant_summary_handler.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/queue/test_variant_summary_handler.py
"""The variant summary job's failure style. Mirrors test_de_summary_handler.py."""

import pytest

from app.errors import PermanentError
from app.models.ai import FailureReason, ProviderKind
from app.queue import variant_summary_handlers
from app.queue.registry import JobContext
from app.queue.variant_summary_handlers import summarize_variant_results
from app.services import variant_summary_prompt
from app.services.ai.adapters import Completion, Failure
from app.services.ai.router import ResolvedProvider


def _ctx(payload: dict) -> JobContext:
    return JobContext(job_id="job-1", payload=payload, epoch=1, attempts=1, owner="local")


def _payload(**overrides) -> dict:
    base = {
        "object_id": "obj-1",
        "facts": {
            "vcf_stats_summary": {"variants": 48213, "ti_tv_ratio": 2.14},
        },
        "top_variants": [
            {"gene": "BRCA1", "position": "chr17:43094464", "consequence": "stop_gained"}
        ],
        "facts_fingerprint": "abc123",
    }
    base.update(overrides)
    return base


def _fake_provider():
    return ResolvedProvider(
        provider_id="000000000000000000000000",
        name="Test",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1",
        api_key=None,
        model="test-model",
        models_cache=[],
    )


class TestSkips:
    def test_no_provider_is_a_success_with_a_reason_not_a_failure(self, monkeypatch):
        monkeypatch.setattr(variant_summary_handlers, "_resolve_sync", lambda: None)
        result = summarize_variant_results(_ctx(_payload()))
        assert result["skipped"] == "no_provider"

    def test_a_result_with_nothing_to_say_is_skipped_before_the_model_is_called(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            variant_summary_handlers, "_resolve_sync", lambda: _fake_provider()
        )

        def must_not_run(*a, **k):
            raise AssertionError("the model must not be called with no prompt")

        monkeypatch.setattr(variant_summary_handlers, "_complete", must_not_run)
        result = summarize_variant_results(
            _ctx(
                _payload(
                    facts={"vcf_stats_summary": {"variants": 0}}, top_variants=[]
                )
            )
        )
        assert result["skipped"] == "insufficient_data"


class TestSuccess:
    def test_a_generated_summary_carries_its_model_and_fingerprint(self, monkeypatch):
        monkeypatch.setattr(
            variant_summary_handlers, "_resolve_sync", lambda: _fake_provider()
        )
        monkeypatch.setattr(
            variant_summary_handlers,
            "_complete",
            lambda p, **kw: Completion("48,213 variants were called.", "test-model"),
        )
        result = summarize_variant_results(_ctx(_payload()))
        assert result["summary"] == "48,213 variants were called."
        assert result["model"] == "test-model"
        assert result["facts_fingerprint"] == "abc123"


class TestPayloadValidation:
    def test_a_payload_with_no_object_is_permanently_bad(self):
        with pytest.raises(PermanentError):
            summarize_variant_results(_ctx({"facts": {}}))


class TestFailureReasons:
    def test_a_failure_is_reported_in_the_result(self, monkeypatch):
        monkeypatch.setattr(
            variant_summary_handlers, "_resolve_sync", lambda: _fake_provider()
        )
        monkeypatch.setattr(
            variant_summary_handlers,
            "_complete",
            lambda p, **kw: Failure(FailureReason.INVALID_KEY),
        )
        result = summarize_variant_results(_ctx(_payload()))
        assert result["skipped"] == "invalid_key"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec api python -m pytest tests/queue/test_variant_summary_handler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.queue.variant_summary_handlers'`

- [ ] **Step 3: Write the handler**

```python
# backend/app/queue/variant_summary_handlers.py
"""The variant-call narrative-summary job. Mirrors de_summary_handlers.py,
pointed at VARIANT_SUMMARY and variant_summary_prompt.
"""

import importlib

from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.queue.registry import HandlerMode, JobContext, handler
from app.services import variant_summary_prompt
from app.services.ai.adapters import Completion

ai_complete = importlib.import_module("app.services.ai.complete")

log = get_logger(__name__)


def _resolve_sync():
    from app.db.client import run_from_thread
    from app.models.ai import TaskSlot
    from app.services.ai import router

    return run_from_thread(router.resolve(TaskSlot.VARIANT_SUMMARY))


def _complete(provider, **kwargs):
    return ai_complete.complete_sync(provider, **kwargs)


@handler(
    "summarize_variant_results",
    mode=HandlerMode.THREAD,
    job_class=JobClass.USER_BACKGROUND,
    resources=JobResources(cpu=0, mem_mb=64, io=IoClass.LIGHT),
    max_attempts=2,
)
def summarize_variant_results(ctx: JobContext) -> dict:
    """Generate a short narrative summary of a VCF's call-set statistics.

    Receives everything it needs in the payload; see
    pipeline_service.launch_variant_summary for how it is assembled.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        from app.errors import PermanentError

        raise PermanentError("summarize_variant_results requires an 'object_id'")

    ctx.check_cancel()

    provider = _resolve_sync()
    if provider is None:
        log.info("variant_summary_skipped_no_provider", object_id=object_id)
        return {"object_id": object_id, "skipped": "no_provider"}

    prompt = variant_summary_prompt.build_variant_user_prompt(
        facts=ctx.payload.get("facts") or {},
        top_variants=ctx.payload.get("top_variants") or [],
    )
    if prompt is None:
        log.info("variant_summary_skipped_insufficient_data", object_id=object_id)
        return {"object_id": object_id, "skipped": "insufficient_data"}

    ctx.check_cancel()
    ctx.extend_lease(int(_timeout_seconds()) + 60)

    result = _complete(
        provider, system=variant_summary_prompt.VARIANT_SYSTEM_PROMPT, user=prompt
    )
    if not isinstance(result, Completion):
        log.info(
            "variant_summary_not_generated", object_id=object_id, reason=result.reason
        )
        return {"object_id": object_id, "skipped": str(result.reason)}

    text, model = result.text, result.model
    log.info(
        "variant_summary_generated", object_id=object_id, model=model, chars=len(text)
    )
    return {
        "object_id": object_id,
        "summary": text,
        "model": model,
        "facts_fingerprint": ctx.payload.get("facts_fingerprint"),
    }


def _timeout_seconds() -> float:
    from app.config import settings

    return settings.llm_timeout_seconds
```

- [ ] **Step 4: Register the handler's import**

In `backend/app/queue/handlers.py`, next to the `de_summary_handlers` import added in Task 4:

```python
from app.queue import variant_summary_handlers  # noqa: F401 - registers summarize_variant_results
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/queue/test_variant_summary_handler.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/queue/variant_summary_handlers.py backend/app/queue/handlers.py backend/tests/queue/test_variant_summary_handler.py
git commit -m "feat: add variant summary queue handler"
```

---

## Task 6: Launch functions in pipeline_service.py

**Files:**
- Modify: `backend/app/services/pipeline_service.py`
- Test: `backend/tests/services/test_pipeline_service_summaries.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/services/test_pipeline_service_summaries.py
"""launch_de_summary and launch_variant_summary: the DE/variant analogues of
launch_summary. Both return None when disabled or nothing to summarize, and
queue a job with the right payload shape otherwise.
"""

import pytest

from app.models.object import DataObject, ObjectRole


@pytest.mark.asyncio
async def test_launch_de_summary_returns_none_with_no_significant_genes(
    de_results_object_factory,
):
    obj = await de_results_object_factory(
        facts={"significant_up": 0, "significant_down": 0}
    )
    from app.services import pipeline_service

    job = await pipeline_service.launch_de_summary(object_id=obj.id, owner=obj.owner)
    assert job is None


@pytest.mark.asyncio
async def test_launch_de_summary_queues_a_job_with_top_genes(
    de_results_object_factory,
):
    obj = await de_results_object_factory(
        facts={"significant_up": 5, "significant_down": 2},
        gene_rows=[
            {"gene": "TP53", "log2FoldChange": -2.3, "padj": 1e-8},
        ],
    )
    from app.services import pipeline_service

    job = await pipeline_service.launch_de_summary(object_id=obj.id, owner=obj.owner)
    assert job is not None
    assert job.payload["top_genes"][0]["gene"] == "TP53"


@pytest.mark.asyncio
async def test_launch_variant_summary_returns_none_with_no_variants(
    vcf_stats_object_factory,
):
    obj = await vcf_stats_object_factory(
        facts={"vcf_stats_summary": {"variants": 0}}
    )
    from app.services import pipeline_service

    job = await pipeline_service.launch_variant_summary(
        object_id=obj.id, owner=obj.owner
    )
    assert job is None


@pytest.mark.asyncio
async def test_launch_variant_summary_queues_a_job(vcf_stats_object_factory):
    obj = await vcf_stats_object_factory(
        facts={"vcf_stats_summary": {"variants": 100, "ti_tv_ratio": 2.1}}
    )
    from app.services import pipeline_service

    job = await pipeline_service.launch_variant_summary(
        object_id=obj.id, owner=obj.owner
    )
    assert job is not None
    assert job.payload["object_id"] == str(obj.id)
```

Note: `de_results_object_factory` and `vcf_stats_object_factory` are new
fixtures -- check `backend/tests/conftest.py` for the existing
`data_object_factory` (or equivalently-named object-creation fixture) this
test suite already uses, and add these two as thin wrappers around it that
set `role=ObjectRole.DE_RESULTS` / relevant VCF facts respectively. Follow
whatever factory pattern the existing fixture uses -- do not invent a new one.

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec api python -m pytest tests/services/test_pipeline_service_summaries.py -v`
Expected: FAIL with `AttributeError: module 'app.services.pipeline_service' has no attribute 'launch_de_summary'`

- [ ] **Step 3: Write launch_de_summary**

In `backend/app/services/pipeline_service.py`, add near `launch_summary` (after its closing, before `_params_fingerprint`):

```python
async def launch_de_summary(
    *,
    object_id: PydanticObjectId,
    owner: str,
    force: bool = False,
) -> Job | None:
    """Queue a narrative summary of a differential-expression result.

    Same "additive, both no's are ordinary" contract as launch_summary. The
    top-gene table is read from the DE results TSV rather than facts, since
    facts holds aggregate counts only -- de_runner.read_results() is the same
    reader ExpressionResults' gene-table endpoint already uses.
    """
    from app.queue import queue
    from app.pipelines import de_runner
    from app.services import object_service

    if not settings.llm_summaries_enabled:
        return None

    obj = await object_service.get_object(object_id, owner=owner)
    if obj.role != ObjectRole.DE_RESULTS:
        return None

    facts = {k: v for k, v in obj.facts.items() if not k.startswith("ai_de_summary")}
    fingerprint = summary_fingerprint(obj)
    if not force and obj.facts.get("ai_de_summary_fingerprint") == fingerprint:
        return None

    significant_up = facts.get("significant_up") or 0
    significant_down = facts.get("significant_down") or 0
    top_genes: list[dict] = []
    if significant_up or significant_down:
        rows = de_runner.read_results(Path(obj.storage_path))
        sorted_rows = de_runner.sort_rows(rows, sort="padj", direction="asc")
        top_genes = [
            {
                "gene": row.get("gene") or row.get("gene_id"),
                "log2fc": row.get("log2FoldChange"),
                "padj": row.get("padj"),
            }
            for row in sorted_rows[:20]
        ]

    if not significant_up and not significant_down and not top_genes:
        return None

    payload = {
        "object_id": str(obj.id),
        "facts": facts,
        "top_genes": top_genes,
        "facts_fingerprint": fingerprint,
    }

    dedup = f"de_summary:{obj.id}:{fingerprint}"
    if force:
        from uuid import uuid4

        dedup = f"{dedup}:{uuid4().hex[:8]}"

    job = await queue.enqueue(
        "summarize_de_results",
        owner=owner,
        payload=payload,
        job_class=JobClass.USER_BACKGROUND,
        resources=JobResources(cpu=0, mem_mb=64, io=IoClass.LIGHT),
        max_attempts=2,
        dedup_key=dedup,
        project_id=obj.project_id,
        object_id=obj.id,
    )
    if job is not None:
        log.info("de_summary_launched", job_id=str(job.id), object_id=str(obj.id))
    return job
```

- [ ] **Step 4: Write launch_variant_summary**

Immediately after `launch_de_summary`:

```python
async def launch_variant_summary(
    *,
    object_id: PydanticObjectId,
    owner: str,
    force: bool = False,
) -> Job | None:
    """Queue a narrative summary of a VCF's call-set statistics.

    Same contract as launch_de_summary. The top-N-by-severity variant list
    comes from the same per-variant rows VariantTable's endpoint already
    reads (/vcfstats/variants/{object_id}), filtered to the consequence
    types the system prompt calls "severe" and capped at 20.
    """
    from app.queue import queue
    from app.services import object_service

    if not settings.llm_summaries_enabled:
        return None

    obj = await object_service.get_object(object_id, owner=owner)
    facts = {k: v for k, v in obj.facts.items() if not k.startswith("ai_variant_summary")}
    variant_count = (facts.get("vcf_stats_summary") or {}).get("variants") or 0

    fingerprint = summary_fingerprint(obj)
    if not force and obj.facts.get("ai_variant_summary_fingerprint") == fingerprint:
        return None

    top_variants = _top_severe_variants(facts)

    if not variant_count and not top_variants:
        return None

    payload = {
        "object_id": str(obj.id),
        "facts": facts,
        "top_variants": top_variants,
        "facts_fingerprint": fingerprint,
    }

    dedup = f"variant_summary:{obj.id}:{fingerprint}"
    if force:
        from uuid import uuid4

        dedup = f"{dedup}:{uuid4().hex[:8]}"

    job = await queue.enqueue(
        "summarize_variant_results",
        owner=owner,
        payload=payload,
        job_class=JobClass.USER_BACKGROUND,
        resources=JobResources(cpu=0, mem_mb=64, io=IoClass.LIGHT),
        max_attempts=2,
        dedup_key=dedup,
        project_id=obj.project_id,
        object_id=obj.id,
    )
    if job is not None:
        log.info("variant_summary_launched", job_id=str(job.id), object_id=str(obj.id))
    return job


# Ordered most-severe-first. Anything not in this table is left out of the
# top-N list rather than guessed at -- an unrecognized consequence string is
# not necessarily mild, but this feature only speaks for the ones it can
# rank with confidence.
_SEVERITY_ORDER = (
    "stop_gained",
    "frameshift_variant",
    "stop_lost",
    "start_lost",
    "splice_acceptor_variant",
    "splice_donor_variant",
    "missense_variant",
    "inframe_deletion",
    "inframe_insertion",
)


def _top_severe_variants(facts: dict, limit: int = 20) -> list[dict]:
    """The variant rows facts already carries, ranked by consequence severity.

    Reads from `facts["severe_variants"]` -- populated by run_vcf_stats
    alongside consequence_counts, one row per variant with a consequence in
    _SEVERITY_ORDER, capped there at the same limit this function also
    respects. Nothing here re-parses the VCF.
    """
    rows = facts.get("severe_variants")
    if not isinstance(rows, list):
        return []

    def rank(row: dict) -> int:
        consequence = row.get("consequence")
        try:
            return _SEVERITY_ORDER.index(consequence)
        except ValueError:
            return len(_SEVERITY_ORDER)

    ranked = sorted(rows, key=rank)
    return ranked[:limit]
```

Note: this introduces a dependency on `run_vcf_stats` populating a new
`facts["severe_variants"]` list -- see Task 7, which adds exactly that.

- [ ] **Step 5: Add the needed imports**

At the top of `backend/app/services/pipeline_service.py`, confirm `ObjectRole`
and `Path` are already imported (both are used elsewhere in this file for
similar object-role checks and file paths); add them if not already present.

- [ ] **Step 6: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/services/test_pipeline_service_summaries.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_pipeline_service_summaries.py
git commit -m "feat: add launch_de_summary and launch_variant_summary"
```

---

## Task 7: Populate severe_variants in run_vcf_stats

**Files:**
- Modify: `backend/app/queue/variant_handlers.py` (around `run_vcf_stats`, line ~400)
- Modify: `backend/app/pipelines/vcf_stats_runner.py`
- Test: `backend/tests/pipelines/test_vcf_stats_runner.py`

- [ ] **Step 1: Read the current run_vcf_stats/vcf_stats_runner facts assembly**

Before writing code, read `backend/app/pipelines/vcf_stats_runner.py` in full
to find where `consequence_counts` (or the equivalent facts key populated
today) is assembled from parsed BCSQ annotations, and where that dict is
returned to `run_vcf_stats` in `variant_handlers.py`. The new
`severe_variants` list must be built from the same per-record loop that
already produces `consequence_counts`, not a second pass over the VCF.

- [ ] **Step 2: Write the failing test**

```python
# Add to backend/tests/pipelines/test_vcf_stats_runner.py
# (create the file if it does not exist; check for an existing
# test_vcf_stats_runner.py first and add to it if present)

def test_severe_variants_are_capped_at_twenty_and_ranked_by_severity():
    """A VCF with more than 20 stop-gained/frameshift/missense calls should
    not hand the summary prompt an unbounded list."""
    # Build (or reuse an existing fixture VCF path/parsed-record list) with
    # 25 stop_gained records and confirm the returned severe_variants list
    # has length 20, and that gene/position/consequence fields are present
    # on each entry.
    ...


def test_a_variant_with_no_gene_annotation_is_still_included(self):
    """Intergenic variants must reach the prompt builder's fallback path,
    not be silently dropped at the source."""
    ...
```

Write these against whatever existing fixture pattern
`test_vcf_stats_runner.py` (or the closest existing VCF-parsing test module)
already uses for building a parsed record set -- do not invent a new VCF
fixture format. If no `test_vcf_stats_runner.py` exists yet, model the
fixture setup on `backend/tests/pipelines/test_csq_parse.py`, which already
builds BCSQ-annotated test records.

- [ ] **Step 3: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/pipelines/test_vcf_stats_runner.py -k severe_variants -v`
Expected: FAIL (function/key does not exist yet)

- [ ] **Step 4: Add severe_variants collection to vcf_stats_runner.py**

In the per-record loop that already builds `consequence_counts`, add: for
each record whose parsed consequence (via `csq_parse.parse_bcsq`, already
imported in this module) matches one of the severity-ordered types (mirror
the `_SEVERITY_ORDER` tuple added in Task 6's `pipeline_service.py` -- import
it from there rather than redefining it, i.e.
`from app.services.pipeline_service import _SEVERITY_ORDER` if that creates
no circular import, otherwise move `_SEVERITY_ORDER` to
`app.pipelines.csq_parse` and import it from both places), append
`{"gene": consequence.gene, "position": f"{record.chrom}:{record.pos}", "consequence": consequence.consequence_type}`
to a `severe_variants` list, capped at 20, sorted by severity rank.

Return this list as part of the dict `vcf_stats_runner`'s main function
already returns, alongside `consequence_counts`. In `variant_handlers.py`'s
`run_vcf_stats`, confirm the returned facts dict passes this new key through
unchanged (it should, if it already passes through the runner's full return
dict -- verify by reading the handler, do not assume).

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/pipelines/test_vcf_stats_runner.py -v`
Expected: PASS

- [ ] **Step 6: Run the full pipelines test suite to check for regressions**

Run: `docker compose exec api python -m pytest tests/pipelines/ -q`
Expected: All passing, same count as before this task plus the new tests

- [ ] **Step 7: Commit**

```bash
git add backend/app/pipelines/vcf_stats_runner.py backend/app/queue/variant_handlers.py backend/tests/pipelines/test_vcf_stats_runner.py
git commit -m "feat: collect severe variants during vcf_stats for the summary prompt"
```

---

## Task 8: Chain the launches and add appliers in results.py

**Files:**
- Modify: `backend/app/queue/results.py`

- [ ] **Step 1: Add _apply_summarize_de_results**

In `backend/app/queue/results.py`, immediately after `_apply_summarize_object`
(around line 910), add:

```python
async def _apply_summarize_de_results(result: dict, *, owner: str) -> None:
    """Record a generated DE narrative summary on the results object.

    Same no-op-on-skip contract as _apply_summarize_object: a down model
    server or an unremarkable result must leave whatever summary already
    exists alone rather than clearing it.
    """
    object_id = result.get("object_id")
    summary = result.get("summary")
    if not object_id or not summary:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("de_summary_object_missing", object_id=object_id)
        return

    facts = {
        **obj.facts,
        "ai_de_summary": summary,
        "ai_de_summary_model": result.get("model"),
        "ai_de_summary_at": datetime.now(UTC).isoformat(),
    }
    fingerprint = result.get("facts_fingerprint")
    if fingerprint:
        facts["ai_de_summary_fingerprint"] = fingerprint

    await obj.set(
        {
            DataObject.facts: facts,
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info("de_summary_applied", object_id=object_id, model=result.get("model"))
```

- [ ] **Step 2: Add _apply_summarize_variant_results**

Immediately after `_apply_summarize_de_results`:

```python
async def _apply_summarize_variant_results(result: dict, *, owner: str) -> None:
    """Record a generated variant-call narrative summary on the VCF object."""
    object_id = result.get("object_id")
    summary = result.get("summary")
    if not object_id or not summary:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("variant_summary_object_missing", object_id=object_id)
        return

    facts = {
        **obj.facts,
        "ai_variant_summary": summary,
        "ai_variant_summary_model": result.get("model"),
        "ai_variant_summary_at": datetime.now(UTC).isoformat(),
    }
    fingerprint = result.get("facts_fingerprint")
    if fingerprint:
        facts["ai_variant_summary_fingerprint"] = fingerprint

    await obj.set(
        {
            DataObject.facts: facts,
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "variant_summary_applied", object_id=object_id, model=result.get("model")
    )
```

- [ ] **Step 3: Register both in the dispatch dict**

Find the dispatch dict (the one containing `"summarize_object":
_apply_summarize_object`, around line 1752) and add two entries:

```python
    "summarize_de_results": _apply_summarize_de_results,
    "summarize_variant_results": _apply_summarize_variant_results,
```

- [ ] **Step 4: Chain launch_de_summary from _apply_differential_expression**

In `_apply_differential_expression` (around line 1593), after the existing
`run_service.record_outputs(run_id, [de.id], owner=de.owner)` call and before
the function ends, add:

```python
    from app.services import pipeline_service

    try:
        # Additive extra, same reasoning as QC's chained launch_summary call:
        # a failure to queue the DE summary must not undo the DE results
        # ingest that just succeeded.
        await pipeline_service.launch_de_summary(object_id=de.id, owner=owner)
    except Exception as e:  # noqa: BLE001 - an additive extra cannot fail DE
        log.warning("de_summary_launch_failed", object_id=str(de.id), error=str(e))
```

- [ ] **Step 5: Chain launch_variant_summary from _apply_run_vcf_stats**

In `_apply_run_vcf_stats` (around line 1370), after the existing
`log.info("vcf_stats_applied", ...)` call, add:

```python
    from app.services import pipeline_service

    try:
        await pipeline_service.launch_variant_summary(object_id=obj.id, owner=owner)
    except Exception as e:  # noqa: BLE001 - an additive extra cannot fail vcf_stats
        log.warning("variant_summary_launch_failed", object_id=object_id, error=str(e))
```

- [ ] **Step 6: Run the results test suite**

Run: `docker compose exec api python -m pytest tests/queue/test_results.py -q`
Expected: All existing tests still pass (this task adds no new test file --
the chained-launch behavior is covered end-to-end by Task 6's
`launch_de_summary`/`launch_variant_summary` tests plus the appliers'
straightforward mirroring of `_apply_summarize_object`, which already has
coverage). If `test_results.py` does not exist under that name, run:
`docker compose exec api python -m pytest tests/queue/ -k results -q`

- [ ] **Step 7: Commit**

```bash
git add backend/app/queue/results.py
git commit -m "feat: chain DE and variant summary launches into their producing jobs"
```

---

## Task 9: API endpoints

**Files:**
- Modify: `backend/app/api/v1/pipelines.py`
- Test: `backend/tests/api/test_de_variant_summary_endpoints.py`

- [ ] **Step 1: Read the existing /pipelines/summary/* endpoints in full**

Read `backend/app/api/v1/pipelines.py` lines 180-260 (already shown above)
plus the `SummaryRequest` request-body model definition (search for `class
SummaryRequest` in the same file) before writing the new endpoints, since
the new ones reuse its shape.

- [ ] **Step 2: Write the failing tests**

```python
# backend/tests/api/test_de_variant_summary_endpoints.py
"""The DE/variant summary endpoints mirror /pipelines/summary/* exactly --
same status/launch shape, different slot and object role."""

import pytest


@pytest.mark.asyncio
async def test_de_summary_status_reports_unavailable_with_no_provider(client):
    resp = await client.get("/pipelines/de-summary/status")
    assert resp.status_code == 200
    assert resp.json()["available"] is False


@pytest.mark.asyncio
async def test_variant_summary_status_reports_unavailable_with_no_provider(client):
    resp = await client.get("/pipelines/variant-summary/status")
    assert resp.status_code == 200
    assert resp.json()["available"] is False


@pytest.mark.asyncio
async def test_launch_de_summary_404s_for_a_nonexistent_object(client, owner_headers):
    resp = await client.post(
        "/pipelines/de-summary",
        json={"object_id": "000000000000000000000000"},
        headers=owner_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_launch_variant_summary_404s_for_a_nonexistent_object(
    client, owner_headers
):
    resp = await client.post(
        "/pipelines/variant-summary",
        json={"object_id": "000000000000000000000000"},
        headers=owner_headers,
    )
    assert resp.status_code == 404
```

Fixture names (`client`, `owner_headers`) are placeholders matching whatever
the existing `backend/tests/api/` suite already provides for authenticated
API-client tests -- check an existing file like the tests covering
`/pipelines/summary` (search `backend/tests/api/` for `summary/status` or
`launchSummary`-equivalent coverage) and match its fixture names exactly.

- [ ] **Step 3: Run tests to verify they fail**

Run: `docker compose exec api python -m pytest tests/api/test_de_variant_summary_endpoints.py -v`
Expected: FAIL with 404 routing errors (endpoints do not exist yet)

- [ ] **Step 4: Add the DE summary endpoints**

In `backend/app/api/v1/pipelines.py`, immediately after the existing
`launch_summary` endpoint (after its closing, before `class
OrganismBlurbOut`), add:

```python
@router.get("/de-summary/status")
async def de_summary_status() -> dict:
    """Whether DE narrative summaries can be produced right now.

    Mirrors /pipelines/summary/status exactly, routed to DE_SUMMARY. See
    that endpoint's docstring for why this is not owner-scoped.
    """
    import asyncio

    from app.models.ai import TaskSlot
    from app.services.ai import provider_service
    from app.services.ai import router as ai_router

    if not settings.llm_summaries_enabled:
        return {"available": False, "reason": "disabled"}

    provider = await ai_router.resolve(TaskSlot.DE_SUMMARY)
    if provider is None:
        return {"available": False, "reason": "no_provider"}

    if _is_local(provider.base_url):
        alive = await asyncio.to_thread(_probe_local, provider)
        if not alive:
            return {"available": False, "reason": "server_unavailable"}
    else:
        stored = await provider_service.get(provider.provider_id)
        if stored is not None and stored.status == "failed":
            return {
                "available": False,
                "reason": str(stored.status_reason) if stored.status_reason else "failed",
                "provider_name": provider.name,
            }

    return {
        "available": True,
        "model": provider.model or (provider.models_cache[0] if provider.models_cache else None),
        "provider_name": provider.name,
    }


@router.post("/de-summary", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_de_summary(body: SummaryRequest, owner: OwnerDep) -> JobOut:
    """Queue a narrative summary of a differential-expression result."""
    job = await pipeline_service.launch_de_summary(
        object_id=body.object_id, owner=owner, force=body.force
    )
    if job is None:
        raise ConflictError(
            "Summaries are disabled or this result has nothing to summarize",
            details={"object_id": str(body.object_id)},
        )
    return JobOut.of(job)


@router.get("/variant-summary/status")
async def variant_summary_status() -> dict:
    """Whether variant-call narrative summaries can be produced right now.

    Mirrors /pipelines/summary/status, routed to VARIANT_SUMMARY.
    """
    import asyncio

    from app.models.ai import TaskSlot
    from app.services.ai import provider_service
    from app.services.ai import router as ai_router

    if not settings.llm_summaries_enabled:
        return {"available": False, "reason": "disabled"}

    provider = await ai_router.resolve(TaskSlot.VARIANT_SUMMARY)
    if provider is None:
        return {"available": False, "reason": "no_provider"}

    if _is_local(provider.base_url):
        alive = await asyncio.to_thread(_probe_local, provider)
        if not alive:
            return {"available": False, "reason": "server_unavailable"}
    else:
        stored = await provider_service.get(provider.provider_id)
        if stored is not None and stored.status == "failed":
            return {
                "available": False,
                "reason": str(stored.status_reason) if stored.status_reason else "failed",
                "provider_name": provider.name,
            }

    return {
        "available": True,
        "model": provider.model or (provider.models_cache[0] if provider.models_cache else None),
        "provider_name": provider.name,
    }


@router.post(
    "/variant-summary", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_variant_summary(body: SummaryRequest, owner: OwnerDep) -> JobOut:
    """Queue a narrative summary of a VCF's call-set statistics."""
    job = await pipeline_service.launch_variant_summary(
        object_id=body.object_id, owner=owner, force=body.force
    )
    if job is None:
        raise ConflictError(
            "Summaries are disabled or this file has nothing to summarize",
            details={"object_id": str(body.object_id)},
        )
    return JobOut.of(job)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/api/test_de_variant_summary_endpoints.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/tests/api/test_de_variant_summary_endpoints.py
git commit -m "feat: add DE and variant summary API endpoints"
```

---

## Task 10: Full backend test suite check

**Files:** none (verification task)

- [ ] **Step 1: Run the full backend suite from a worktree-safe path**

Since this work may be happening in a worktree, use the worktree-safe test
runner rather than `docker compose exec api`:

Run: `./backend/run-worktree-tests.sh tests/ -q`

If instead running from the main repo checkout, use:

Run: `docker compose exec api python -m pytest tests/ -q`

- [ ] **Step 2: Confirm the count**

Expected: every test passes, and the total count has grown by exactly the
number of new tests added in Tasks 1-9 (read the actual reported count --
do not assume, per this repo's own guidance on trusting the pytest count
over the exit code).

- [ ] **Step 3: Fix any regressions found**

If anything fails, diagnose against the specific new code from Tasks 1-9 --
do not modify unrelated passing tests to make them pass.

---

## Task 11: Frontend types and API client

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add the two new fact interfaces**

In `frontend/src/api/types.ts`, immediately after the `AiSummaryFacts`
interface, add:

```typescript
/** Same shape as AiSummaryFacts, for a differential-expression result. */
export interface DeSummaryFacts {
  ai_de_summary?: string;
  ai_de_summary_model?: string | null;
  ai_de_summary_at?: string;
  ai_de_summary_fingerprint?: string;
}

/** Same shape as AiSummaryFacts, for a VCF's call-set statistics. */
export interface VariantSummaryFacts {
  ai_variant_summary?: string;
  ai_variant_summary_model?: string | null;
  ai_variant_summary_at?: string;
  ai_variant_summary_fingerprint?: string;
}
```

- [ ] **Step 2: Add the API client functions**

In `frontend/src/api/client.ts`, immediately after the existing
`summaryStatus` and `launchSummary` functions (around lines 463 and 526),
add:

```typescript
  deSummaryStatus: () =>
    request<{ available: boolean; reason?: string; model?: string; provider_name?: string }>(
      "/pipelines/de-summary/status"
    ),
  launchDeSummary: (objectId: string) =>
    request<JobSummary>("/pipelines/de-summary", {
      method: "POST",
      body: JSON.stringify({ object_id: objectId }),
    }),
  variantSummaryStatus: () =>
    request<{ available: boolean; reason?: string; model?: string; provider_name?: string }>(
      "/pipelines/variant-summary/status"
    ),
  launchVariantSummary: (objectId: string) =>
    request<JobSummary>("/pipelines/variant-summary", {
      method: "POST",
      body: JSON.stringify({ object_id: objectId }),
    }),
```

Match the exact `request<...>` call shape and body-serialization style the
existing `summaryStatus`/`launchSummary` functions use in this file --
read them first, since the surrounding object literal syntax (trailing
commas, quoting) must match the file's existing convention exactly.

- [ ] **Step 3: Verify the frontend typechecks**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no new type errors

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat: add frontend types and API client functions for DE/variant summaries"
```

---

## Task 12: Generalize AiSummary for reuse

**Files:**
- Modify: `frontend/src/components/AiSummary.tsx`

- [ ] **Step 1: Add optional props for slot-specific behavior**

Modify the `AiSummary` function signature and body to accept three new
optional props -- `factPrefix`, `statusFn`, and `launchFn` -- defaulting to
today's file-summary behavior so every existing call site
(`DetailPanel.tsx:970`) needs no changes:

```typescript
export function AiSummary({
  facts,
  objectId,
  fingerprint,
  factPrefix = "ai_summary",
  statusFn = () => api.summaryStatus(),
  launchFn = (id: string) => api.launchSummary(id),
  emptyLabel = "No summary yet for this file.",
}: {
  facts: Record<string, unknown>;
  objectId: string;
  fingerprint?: string;
  /** The fact-key prefix this instance reads/writes, e.g. "ai_de_summary". */
  factPrefix?: string;
  /** Overridable for slots other than FILE_SUMMARY. */
  statusFn?: () => ReturnType<typeof api.summaryStatus>;
  launchFn?: (objectId: string) => ReturnType<typeof api.launchSummary>;
  /** Shown when there is no stored summary and generation is unavailable, or
   * before the first one is written. */
  emptyLabel?: string;
}) {
```

Inside the body, replace every direct `summary.ai_summary`,
`summary.ai_summary_model`, `summary.ai_summary_at`,
`summary.ai_summary_fingerprint` field access with a lookup keyed by
`factPrefix`:

```typescript
  const raw = facts as Record<string, unknown>;
  const existing = (raw[factPrefix] as string | undefined)?.trim();
  const model = raw[`${factPrefix}_model`] as string | null | undefined;
  const writtenAt = raw[`${factPrefix}_at`] as string | undefined;
  const storedFingerprint = raw[`${factPrefix}_fingerprint`] as string | undefined;
```

Update the `useQuery`/`useMutation` calls to use `statusFn`/`launchFn`
instead of the hardcoded `api.summaryStatus()`/`api.launchSummary(objectId)`,
and the query keys to include `factPrefix` so the three slots' cached status
don't collide:

```typescript
  const { data: status } = useQuery({
    queryKey: ["summary", "status", factPrefix],
    queryFn: statusFn,
    retry: false,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });

  const regenerate = useMutation({
    mutationFn: () => launchFn(objectId),
    ...
  });
```

Replace the hardcoded `"No summary yet for this file."` string with
`{emptyLabel}`, and the `stale` computation's references to
`summary.ai_summary_fingerprint` with `storedFingerprint`.

- [ ] **Step 2: Verify the existing call site still compiles with defaults**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no type errors -- `DetailPanel.tsx:970`'s existing
`<AiSummary facts={obj.facts} objectId={obj.id} fingerprint={...} />` call
uses none of the new optional props and must behave identically to before.

- [ ] **Step 3: Manual verification in the browser**

Start the worktree stack if not already running:

```bash
./ops/worktree-up.sh
```

Open `localhost:5273`, navigate to a file with an existing AI summary
(or write one if none exists via the provider settings + regenerate
button), and confirm the summary section on the file detail page still
renders exactly as before -- same text, same "Regenerate" button, same
staleness badge behavior.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/AiSummary.tsx
git commit -m "refactor: generalize AiSummary to support additional summary slots"
```

---

## Task 13: Render the DE summary in ExpressionResults

**Files:**
- Modify: `frontend/src/components/ExpressionResults.tsx`

- [ ] **Step 1: Add the AiSummary import and render call**

In `frontend/src/components/ExpressionResults.tsx`, add the import:

```typescript
import { AiSummary } from "./AiSummary";
import { api } from "../api/client";
```

(Check whether `api` is already imported in this file -- it likely is, given
the existing `api.deResults` call; do not duplicate the import if so.)

Immediately inside the returned JSX, before the sample-PCA plot (per the
design doc: "above the PCA/volcano/MA plots"), add:

```tsx
<AiSummary
  facts={obj.facts}
  objectId={obj.id}
  fingerprint={obj.summary_fingerprint ?? undefined}
  factPrefix="ai_de_summary"
  statusFn={() => api.deSummaryStatus()}
  launchFn={(id) => api.launchDeSummary(id)}
  emptyLabel="No summary yet for this result."
/>
```

Read the surrounding JSX structure first to place this correctly relative
to the existing plots -- the exact insertion point is "before whatever
element currently renders the sample-PCA plot first."

- [ ] **Step 2: Verify typecheck**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no new errors

- [ ] **Step 3: Manual verification in the browser**

With a DE results object in the worktree stack (run a differential
expression job on test data, or use existing project data), open its
results page and confirm:
- No summary section appears if no provider is configured for DE_SUMMARY
  (the settings page's per-slot row, added automatically by Task 1's
  `TaskSlot` addition, must show "Differential expression summaries" as an
  unrouted slot).
- After configuring a provider and routing DE_SUMMARY to it (or leaving it
  on the default), triggering a new DE run produces a summary that appears
  above the plots.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ExpressionResults.tsx
git commit -m "feat: render DE summary above the DE results plots"
```

---

## Task 14: Render the variant summary in VariantResults

**Files:**
- Modify: `frontend/src/components/VariantResults.tsx`

- [ ] **Step 1: Add the AiSummary import and render call**

In `frontend/src/components/VariantResults.tsx`, add the import (check
whether `api` is already imported, as with Task 13):

```typescript
import { AiSummary } from "./AiSummary";
```

Immediately inside the `hasResults` branch of the returned JSX, before the
Ti/Tv/QUAL charts (per the design doc: "above the Ti/Tv/QUAL charts and the
variant table"), add:

```tsx
<AiSummary
  facts={obj.facts}
  objectId={obj.id}
  fingerprint={obj.summary_fingerprint ?? undefined}
  factPrefix="ai_variant_summary"
  statusFn={() => api.variantSummaryStatus()}
  launchFn={(id) => api.launchVariantSummary(id)}
  emptyLabel="No summary yet for this file."
/>
```

Read the surrounding JSX first to place this correctly -- the exact
insertion point is "inside the `hasResults` block, before whatever element
currently renders the Ti/Tv/QUAL distribution charts first."

- [ ] **Step 2: Verify typecheck**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no new errors

- [ ] **Step 3: Manual verification in the browser**

With a VCF that has had "Compute results" run in the worktree stack, open
its variant results page and confirm the same before/after behavior as
Task 13's manual check, substituted for the variant summary.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/VariantResults.tsx
git commit -m "feat: render variant summary above the variant results charts"
```

---

## Task 15: Update the Software/settings help docs if applicable

**Files:**
- Check: `frontend/src/components/HelpSoftware.tsx` (or wherever `TaskSlot`
  labels are surfaced in help text, if anywhere)

- [ ] **Step 1: Check whether TaskSlot members need documentation elsewhere**

Search for `ORGANISM_BLURB` and `FILE_SUMMARY` across `frontend/src` and
`backend/app` to confirm whether either is referenced anywhere beyond the
settings page's automatic per-slot enumeration (which needs no manual
update, since it iterates `TaskSlot` already) and the code already modified
in this plan:

```bash
grep -rn "FILE_SUMMARY\|ORGANISM_BLURB" frontend/src backend/app
```

- [ ] **Step 2: If any manual reference exists outside what this plan already touched, add the DE_SUMMARY/VARIANT_SUMMARY equivalents there too**

Otherwise, no changes needed -- the settings page's `TaskSlot` enumeration
means Task 1 alone was sufficient to make both new slots configurable.

- [ ] **Step 3: Commit if changes were made**

```bash
git add -A
git commit -m "docs: reference new AI summary slots where FILE_SUMMARY/ORGANISM_BLURB were documented"
```

(Skip this commit if Step 1 found nothing to change.)

---

## Task 16: Final full-suite verification and merge

**Files:** none (verification and merge task)

- [ ] **Step 1: Run the full backend suite**

From a worktree: `./backend/run-worktree-tests.sh tests/ -q`
From the main checkout: `docker compose exec api python -m pytest tests/ -q`

Expected: all tests pass; read the printed count.

- [ ] **Step 2: Run the frontend typecheck**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no errors

- [ ] **Step 3: Manual browser verification of both new summaries end-to-end**

Using `./ops/worktree-up.sh` (or the main stack if merged there already):
run a DE job and a variant-calling + Compute-results flow against real
project data, confirm both summaries generate, render in the right
location, respect the stale/regenerate/no-provider states, and that
existing FILE_SUMMARY behavior on unrelated file types is unchanged.

- [ ] **Step 4: Merge to main and push**

Per this repo's CLAUDE.md: once the suite is green and main is clean, merge
and push without waiting for further permission.

```bash
git checkout main
git pull
git merge --no-ff <feature-branch>
docker compose exec api python -m pytest tests/ -q
git push origin main
```

If `main` has moved since this branch was created, re-run the full suite
after merging rather than trusting the pre-merge green.
