"""launch_de_summary and launch_variant_summary: the DE/variant analogues of
launch_summary. Both return None when disabled or nothing to summarize, and
queue a job with the right payload shape otherwise.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.object import DataObject, ObjectRole

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.usefixtures("beanie_models"),
]


async def test_launch_de_summary_returns_none_with_no_significant_genes(
    de_results_object_factory,
):
    obj = await de_results_object_factory(
        facts={"significant_up": 0, "significant_down": 0}
    )
    from app.services import pipeline_service

    job = await pipeline_service.launch_de_summary(object_id=obj.id, owner=obj.owner)
    assert job is None


async def test_launch_de_summary_queues_a_job_with_top_genes(
    de_results_object_factory,
):
    obj = await de_results_object_factory(
        facts={"significant_up": 5, "significant_down": 2},
        gene_rows=[
            {"gene": "TP53", "log2_fold_change": -2.3, "padj": 1e-8},
        ],
    )
    from app.services import pipeline_service

    enqueued = {}

    async def _enqueue(job_type, **kwargs):
        enqueued["type"] = job_type
        enqueued.update(kwargs)
        return SimpleNamespace(id="job1", payload=kwargs["payload"])

    with patch("app.queue.queue.enqueue", _enqueue):
        job = await pipeline_service.launch_de_summary(object_id=obj.id, owner=obj.owner)

    assert job is not None
    assert enqueued["type"] == "summarize_de_results"
    assert job.payload["top_genes"][0]["gene"] == "TP53"


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


async def test_launch_variant_summary_queues_a_job(vcf_stats_object_factory):
    obj = await vcf_stats_object_factory(
        facts={"vcf_stats_summary": {"variants": 100, "ti_tv_ratio": 2.1}}
    )
    from app.services import pipeline_service

    enqueued = {}

    async def _enqueue(job_type, **kwargs):
        enqueued["type"] = job_type
        enqueued.update(kwargs)
        return SimpleNamespace(id="job1", payload=kwargs["payload"])

    with patch("app.queue.queue.enqueue", _enqueue):
        job = await pipeline_service.launch_variant_summary(
            object_id=obj.id, owner=obj.owner
        )

    assert job is not None
    assert enqueued["type"] == "summarize_variant_results"
    assert job.payload["object_id"] == str(obj.id)


async def test_summary_fingerprint_ignores_de_summary_bookkeeping_keys(
    de_results_object_factory,
):
    """A stored ai_de_summary_fingerprint must remain valid dedup state.

    launch_de_summary compares summary_fingerprint(obj) against
    obj.facts["ai_de_summary_fingerprint"] to decide whether to skip a
    re-run. If summary_fingerprint() hashed the ai_de_summary* bookkeeping
    keys a results-applier writes alongside that fingerprint (ai_de_summary,
    ai_de_summary_model, ai_de_summary_at, ai_de_summary_fingerprint itself),
    the fingerprint computed after those keys exist would never match the one
    computed before they existed -- the same numbers would report as changed
    forever, and force=False would never dedup again.
    """
    from app.services import pipeline_service

    base_facts = {"significant_up": 5, "significant_down": 2}
    obj = await de_results_object_factory(facts=dict(base_facts))
    fingerprint_before = pipeline_service.summary_fingerprint(obj)

    obj.facts = {
        **base_facts,
        "ai_de_summary": "Five genes went up, two went down.",
        "ai_de_summary_model": "test-model",
        "ai_de_summary_at": "2026-08-07T00:00:00+00:00",
        "ai_de_summary_fingerprint": fingerprint_before,
    }
    fingerprint_after = pipeline_service.summary_fingerprint(obj)

    assert fingerprint_after == fingerprint_before


async def test_summary_fingerprint_ignores_variant_summary_bookkeeping_keys(
    vcf_stats_object_factory,
):
    """Same as above for launch_variant_summary's ai_variant_summary_* keys."""
    from app.services import pipeline_service

    base_facts = {"vcf_stats_summary": {"variants": 100, "ti_tv_ratio": 2.1}}
    obj = await vcf_stats_object_factory(facts=dict(base_facts))
    fingerprint_before = pipeline_service.summary_fingerprint(obj)

    obj.facts = {
        **base_facts,
        "ai_variant_summary": "100 variants called.",
        "ai_variant_summary_model": "test-model",
        "ai_variant_summary_at": "2026-08-07T00:00:00+00:00",
        "ai_variant_summary_fingerprint": fingerprint_before,
    }
    fingerprint_after = pipeline_service.summary_fingerprint(obj)

    assert fingerprint_after == fingerprint_before
