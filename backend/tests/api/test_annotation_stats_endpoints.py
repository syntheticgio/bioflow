"""The annotation feature table's routes.

Structured like test_vcf_stats_report.py: exercised through the real route on
a bare FastAPI app with `object_service.get_object` stubbed and
`settings.bioinfo_home` pointed at a temp directory, rather than standing up
Mongo. The fixture builds a real features.db with `build_annotation_db` so a
schema drift between the builder and the query routes is also caught.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import pipelines as pipelines_api
from app.api.v1.pipelines import router
from app.config import settings
from app.errors import register_exception_handlers
from app.pipelines.annotation_db import build_annotation_db
from app.pipelines.annotation_hierarchy import build_gene_table, resolve_hierarchy
from app.pipelines.annotation_parse import Feature
from tests.api.bare_app import override_owner, stub_get_object

OBJECT_ID = "507f1f77bcf86cd799439011"
OTHER_ID = "507f191e810c19729de860ea"


def _feature(feature_id, parent=None, start=100, ftype="gene"):
    return Feature(
        contig="chr1",
        start=start,
        end=start + 50,
        type=ftype,
        strand="+",
        score=None,
        name=feature_id,
        feature_id=feature_id,
        parents=(parent,) if parent is not None else (),
        biotype="protein_coding",
        attributes=f"ID={feature_id}",
    )


FEATURES = [
    _feature("g1", start=100, ftype="gene"),
    _feature("e1", parent="g1", start=110, ftype="exon"),
]

# A superset of FEATURES that also carries a record whose declared parent
# does not exist anywhere in the file -- the Unresolved view and the genes
# table both need at least one row like this to be worth testing.
FEATURES_WITH_DANGLING = [
    *FEATURES,
    _feature("orphan1", parent="does-not-exist", start=200, ftype="exon"),
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)

    db_path = tmp_path / "annotation_stats" / OBJECT_ID / "features.db"
    build_annotation_db(rows=iter(FEATURES), db_path=db_path)

    # Both ids resolve: these tests are about pagination, filtering, and the
    # "no computed results" 404 -- not ownership scoping, which is covered
    # elsewhere against the real app. See test_vcf_stats_report.py.
    stub_get_object(monkeypatch, pipelines_api, known={OBJECT_ID, OTHER_ID})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


@pytest.fixture
def hierarchy_client(tmp_path, monkeypatch):
    """Like `client`, but the database has a dangling record and has been run
    through resolve_hierarchy/build_gene_table -- what the compute job does in
    production, which the base `client` fixture skips since the original six
    tests didn't need the hierarchy or genes tables populated."""
    monkeypatch.setattr(settings, "bioinfo_home", tmp_path)

    db_path = tmp_path / "annotation_stats" / OBJECT_ID / "features.db"
    build_annotation_db(rows=iter(FEATURES_WITH_DANGLING), db_path=db_path)
    resolve_hierarchy(db_path=db_path)
    build_gene_table(db_path=db_path)

    stub_get_object(monkeypatch, pipelines_api, known={OBJECT_ID, OTHER_ID})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


def get_features(client, object_id: str = OBJECT_ID, **params):
    return client.get(
        f"/pipelines/annotationstats/features/{object_id}",
        params=params,
        follow_redirects=False,
    )


def get_children(client, object_id: str = OBJECT_ID, **params):
    return client.get(
        f"/pipelines/annotationstats/children/{object_id}",
        params=params,
        follow_redirects=False,
    )


class TestFeaturesRoute:
    def test_returns_a_page_with_a_total(self, client):
        r = get_features(client)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert len(body["rows"]) == 1
        assert body["rows"][0]["feature_id"] == "g1"

    def test_skip_count_omits_the_total(self, client):
        r = get_features(client, skip_count=True)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] is None
        assert len(body["rows"]) == 1

    def test_type_filter_reaches_children(self, client):
        r = get_features(client, feature_type="gene")
        assert r.status_code == 200
        body = r.json()
        ids = {row["feature_id"] for row in body["rows"]}
        assert ids == {"g1"}

        r = get_features(client, feature_type="exon")
        body = r.json()
        ids = {row["feature_id"] for row in body["rows"]}
        assert ids == {"e1"}

    def test_404_when_not_computed(self, client):
        r = get_features(client, object_id=OTHER_ID)
        assert r.status_code == 404
        assert "Compute results first" in r.json()["message"]


class TestChildrenRoute:
    def test_returns_children(self, client):
        r = get_children(client, parent_id="g1")
        assert r.status_code == 200
        body = r.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["feature_id"] == "e1"

    def test_empty_for_unknown_parent(self, client):
        r = get_children(client, parent_id="nope")
        assert r.status_code == 200
        assert r.json()["rows"] == []

    def test_reports_the_depth_cap(self, client):
        from app.pipelines.annotation_hierarchy import DEPTH_CAP

        r = get_children(client, parent_id="g1")
        assert r.status_code == 200
        assert r.json()["depth_cap"] == DEPTH_CAP


def get_genes(client, object_id: str = OBJECT_ID, **params):
    return client.get(
        f"/pipelines/annotationstats/genes/{object_id}",
        params=params,
        follow_redirects=False,
    )


class TestUnresolvedView:
    def test_unresolved_view_returns_only_broken_records(self, hierarchy_client):
        """The bug this feature fixes: a dangling record is reachable."""
        r = get_features(hierarchy_client, view="unresolved")
        assert r.status_code == 200
        rows = r.json()["rows"]
        assert rows
        assert all(
            row["parent_status"] in ("dangling", "ambiguous", "self", "cyclic")
            for row in rows
        )
        assert {row["feature_id"] for row in rows} == {"orphan1"}

    def test_all_view_applies_no_status_filter(self, hierarchy_client):
        r = get_features(hierarchy_client, view="all")
        assert r.status_code == 200
        statuses = {row["parent_status"] for row in r.json()["rows"]}
        assert "root" in statuses

    def test_invalid_view_value_returns_422(self, hierarchy_client):
        r = get_features(hierarchy_client, view="bogus")
        assert r.status_code == 422


class TestGenesRoute:
    def test_genes_route_returns_gene_rows(self, hierarchy_client):
        r = get_genes(hierarchy_client)
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] in ("typed", "fallback")
        assert body["rows"][0]["descendant_count"] >= 0

    def test_genes_route_404s_without_computed_results(self, hierarchy_client):
        r = get_genes(hierarchy_client, object_id=OTHER_ID)
        assert r.status_code == 404
        assert "Compute results first" in r.json()["message"]
