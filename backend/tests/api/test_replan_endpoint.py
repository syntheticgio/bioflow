"""POST /pipelines/replan -- the Auto re-plan button's data source."""

import pytest


@pytest.mark.asyncio
async def test_returns_a_proposal_for_a_tunable_over_budget_alignment(client):
    resp = await client.post(
        "/api/v1/pipelines/replan",
        json={
            "job_type": "align_reads",
            "params": {
                "aligner": "minimap2",
                "threads": 64,
                "sort_memory_mb": 4096,
                # _propose_align (and its verifier, _align_estimate) read
                # these two keys directly off the dict with no default --
                # the same gap Task 5 already found on the pipeline_service
                # side. Without them the endpoint would 500 on a KeyError
                # instead of returning a tagged result.
                "reference_bases": 3_100_000_000,
                "building_index": False,
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] in {"proposal", "infeasible", "no_knobs"}


@pytest.mark.asyncio
async def test_align_params_missing_reference_bases_is_no_knobs_not_500(client):
    """A caller that sends only AlignParams -- no reference_bases/building_index
    -- is a real shape, not a hypothetical: AlignDialog's replan query did
    exactly this before it was fixed to enrich the params (see the comment
    on its queryFn), and hit this endpoint's proposer with a bare
    dict.__getitem__ that raised KeyError as an unhandled 500. `replan()`
    now catches that and degrades to `no_knobs` rather than crashing, so a
    future caller with the same gap gets a clean tagged result instead of a
    stack trace in the browser network tab.
    """
    resp = await client.post(
        "/api/v1/pipelines/replan",
        json={
            "job_type": "align_reads",
            "params": {
                "aligner": "minimap2",
                "threads": 64,
                "sort_memory_mb": 4096,
                # Deliberately no reference_bases / building_index.
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"kind": "no_knobs"}


@pytest.mark.asyncio
async def test_unregistered_job_type_reports_no_knobs(client):
    resp = await client.post(
        "/api/v1/pipelines/replan",
        json={"job_type": "run_qc", "params": {}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"kind": "no_knobs"}


@pytest.mark.asyncio
async def test_the_client_cannot_state_its_own_budget(client):
    """A client that names its budget can name a larger one, which turns the
    feasibility test into a formality. Extra keys must be ignored, not honoured."""
    resp = await client.post(
        "/api/v1/pipelines/replan",
        json={
            "job_type": "align_reads",
            "params": {
                "aligner": "minimap2",
                "threads": 64,
                "sort_memory_mb": 4096,
                "reference_bases": 3_100_000_000,
                "building_index": False,
            },
            "budget_mb": 10_000_000,
        },
    )
    assert resp.status_code == 200
    # Ignored: the response must match the no-budget call above.
    baseline = await client.post(
        "/api/v1/pipelines/replan",
        json={
            "job_type": "align_reads",
            "params": {
                "aligner": "minimap2",
                "threads": 64,
                "sort_memory_mb": 4096,
                "reference_bases": 3_100_000_000,
                "building_index": False,
            },
        },
    )
    assert resp.json() == baseline.json()
