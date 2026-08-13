"""The export route, and the filter builder it shares with the page route."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import pipelines as pipelines_api
from app.api.v1.pipelines import build_feature_filters, router
from app.errors import register_exception_handlers
from app.models import Job, JobClass, JobState
from tests.api.bare_app import override_owner


class TestSharedFilterBuilder:
    """One definition of what a filter means. The page count and the export
    must agree, or the matched-versus-exported counts are meaningless."""

    def test_type_filter_clears_top_level_only(self):
        """Every exon has a parent, so leaving the flag set returns an empty
        table on a perfectly good GFF3."""
        f = build_feature_filters(feature_type="exon", view="all")
        assert f.top_level_only is False

    def test_no_type_filter_keeps_top_level_only(self):
        f = build_feature_filters(view="all")
        assert f.top_level_only is True

    def test_unresolved_view_clears_top_level_only(self):
        f = build_feature_filters(view="unresolved")
        assert f.top_level_only is False

    def test_unresolved_view_sets_parent_status(self):
        from app.pipelines import annotation_hierarchy

        f = build_feature_filters(view="unresolved")
        assert f.parent_status == annotation_hierarchy.UNRESOLVED_STATUSES

    def test_passes_through_the_plain_filters(self):
        f = build_feature_filters(
            contig="chr1", biotype="protein_coding", strand="+",
            name_query="BRCA", start_min=10, start_max=20, view="all",
        )
        assert f.contig == "chr1"
        assert f.biotype == "protein_coding"
        assert f.strand == "+"
        assert f.name_query == "BRCA"
        assert f.start_min == 10
        assert f.start_max == 20


OBJECT_ID = "507f1f77bcf86cd799439011"


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


def _fake_job() -> Job:
    return Job(
        type="annotation_subset_export",
        owner="test-owner",
        job_class=JobClass.USER_BACKGROUND,
        state=JobState.PENDING,
        payload={"object_id": OBJECT_ID},
    )


class TestExportRoute:
    """Route-level coverage: request wiring, launcher call, and response
    shape/status -- the part TestSharedFilterBuilder above never touches
    since it calls build_feature_filters() directly rather than going
    through HTTP."""

    def test_success_returns_a_201_job(self, monkeypatch, client, beanie_models):
        async def fake_launch(*, object_id, filters, owner):
            assert str(object_id) == OBJECT_ID
            assert filters["feature_type"] == "exon"
            return _fake_job()

        monkeypatch.setattr(
            pipelines_api.pipeline_service,
            "launch_annotation_subset_export",
            fake_launch,
        )

        resp = client.post(
            "/pipelines/annotationstats/export",
            json={"object_id": OBJECT_ID, "feature_type": "exon", "view": "all"},
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["type"] == "annotation_subset_export"
        assert body["state"] == "pending"

    def test_missing_object_id_is_a_422(self, client):
        resp = client.post(
            "/pipelines/annotationstats/export",
            json={"feature_type": "exon"},
        )

        assert resp.status_code == 422

    def test_malformed_object_id_is_a_422(self, client):
        resp = client.post(
            "/pipelines/annotationstats/export",
            json={"object_id": "not-an-object-id"},
        )

        assert resp.status_code == 422
