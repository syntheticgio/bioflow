"""API surface for quantification requests, and what the dialog is told.

The interesting test here is `TestDefaultsWhenTheAnnotationIsAmbiguous`. The
rest is request-shape coverage of the same kind `test_pipelines_variant.py`
carries.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.pipelines import QuantifyRequest, router
from app.errors import ValidationError, register_exception_handlers
from app.models import FormatKind, ObjectStatus
from tests.api.bare_app import override_owner


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    override_owner(app)
    return TestClient(app)


class TestQuantifyRequestShape:
    def test_bam_id_is_the_only_required_field(self):
        req = QuantifyRequest(bam_id="000000000000000000000001")
        assert req.annotation_id is None
        assert req.params == {}

    def test_accepts_an_explicit_annotation(self):
        """The escape hatch for a project holding more than one assembly's
        annotation, which the server refuses to guess between."""
        req = QuantifyRequest(
            bam_id="000000000000000000000001",
            annotation_id="000000000000000000000002",
        )
        assert req.annotation_id is not None

    def test_rejects_a_malformed_id(self):
        with pytest.raises(ValueError):
            QuantifyRequest(bam_id="not-an-object-id")


def _bam(facts=None):
    return SimpleNamespace(
        id="000000000000000000000001",
        name="sample.bam",
        format=SimpleNamespace(kind=FormatKind.BAM),
        facts=facts if facts is not None else {},
        metadata={},
        status=ObjectStatus.READY,
        project_id="000000000000000000000009",
        owner="local",
    )


def _annotation(obj_id, name, kind=FormatKind.GTF):
    return SimpleNamespace(
        id=obj_id,
        name=name,
        format=SimpleNamespace(kind=kind),
        status=ObjectStatus.READY,
        project_id="000000000000000000000009",
        owner="local",
    )


class TestDefaultsWhenTheAnnotationIsAmbiguous:
    """The dialog must still be told what it will actually do.

    A project holding two assemblies' annotations makes `resolve_annotation`
    refuse to choose -- correctly, since counting against the wrong assembly
    returns a full file with almost nothing assigned. That refusal used to
    also skip deriving the parameters, leaving `params` empty, and the dialog
    rendered its own fallbacks as if they were facts about the file: it told
    the user "this alignment looks single-end" about a BAM with 1.9M
    properly-paired reads, while the server went on to count it as paired.

    Strandedness and pairing come from the *alignment*, so neither depends on
    which annotation wins -- there is no reason to withhold them.
    """

    def _get(self, client, annotations):
        paired_facts = {"properly_paired_reads": 1885414}
        with (
            patch(
                "app.api.v1.pipelines.object_service.get_object",
                new=AsyncMock(return_value=_bam(paired_facts)),
            ),
            patch(
                "app.services.pipeline_service.annotations_for_project",
                new=AsyncMock(return_value=annotations),
            ),
            patch(
                "app.services.pipeline_service.resolve_annotation",
                new=AsyncMock(
                    side_effect=ValidationError("more than one annotation")
                ),
            ),
            patch(
                "app.services.pipeline_service.default_count_params",
                new=AsyncMock(
                    return_value={
                        "threads": 4,
                        "strandedness": 0,
                        "strandedness_label": "unstranded",
                        "paired": True,
                        "feature_type": "exon",
                        "attribute": "gene_id",
                        "count_multi_mapping": False,
                    }
                ),
            ),
        ):
            return client.get(
                "/pipelines/quantify/defaults/000000000000000000000001"
            )

    def test_params_are_still_derived(self, client):
        resp = self._get(
            client,
            [
                _annotation("1", "GCF_x_genomic.gtf"),
                _annotation("2", "GCA_x_genomic.gtf"),
            ],
        )
        assert resp.status_code == 200
        assert resp.json()["params"]["paired"] is True

    def test_the_choice_is_still_reported_as_needed(self, client):
        """Deriving the parameters must not paper over the ambiguity -- the
        dialog still has to ask which annotation."""
        resp = self._get(
            client,
            [
                _annotation("1", "GCF_x_genomic.gtf"),
                _annotation("2", "GCA_x_genomic.gtf"),
            ],
        )
        body = resp.json()
        assert body["needs_annotation"] is True
        assert body["annotation_id"] is None
        assert len(body["annotations"]) == 2

    def test_no_annotations_at_all_leaves_params_empty(self, client):
        """The one case where there is genuinely nothing to derive: no
        annotation means no counting, and the dialog suppresses the derived
        lines rather than inventing them."""
        with (
            patch(
                "app.api.v1.pipelines.object_service.get_object",
                new=AsyncMock(return_value=_bam()),
            ),
            patch(
                "app.services.pipeline_service.annotations_for_project",
                new=AsyncMock(return_value=[]),
            ),
        ):
            resp = client.get(
                "/pipelines/quantify/defaults/000000000000000000000001"
            )
        assert resp.status_code == 200
        assert resp.json()["params"] == {}
        assert resp.json()["needs_annotation"] is True
