"""STAR's annotation-aware index: a distinct sidecar, resolved from the
project's GTF/GFF the same way featureCounts resolves one, and refused for
every other aligner.

`resolve_annotation` used to take a BAM only for its `project_id` -- STAR's
index build has no BAM yet at the point it needs the same resolution, which
is why it now takes a project id directly. These tests cover both callers
sharing the ambiguity-refusal logic, and the parts specific to indexing:
the distinct sidecar role, the dedup key, and the STAR-only guard.
"""

import pytest
import pytest_asyncio

from app.errors import ValidationError
from app.models import FormatKind, ObjectRole, SidecarRole
from app.pipelines.aligners import Aligner
from app.services import pipeline_service, project_service
from tests.services.helpers import TEST_OWNER, make_object

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

GCF_YEAST = "GCF_000146045.2"


async def _make_reference(project):
    reference = await make_object(project, "genome.fna")
    reference.role = ObjectRole.REFERENCE
    reference.format.kind = FormatKind.FASTA
    await reference.save()
    return reference


async def _make_gtf(project, name="genes.gtf"):
    gtf = await make_object(project, name)
    gtf.format.kind = FormatKind.GTF
    await gtf.save()
    return gtf


class TestResolveAnnotationTakesAProjectId:
    """The refactor that let STAR's index build reuse featureCounts'
    resolution: `resolve_annotation` no longer requires a BAM."""

    @pytest_asyncio.fixture(loop_scope="module")
    async def project_with_one_annotation(self):
        project = await project_service.create_project(
            name="one-annotation", owner=TEST_OWNER
        )
        gtf = await _make_gtf(project)
        return project, gtf

    async def test_auto_picks_the_lone_annotation(self, project_with_one_annotation):
        project, gtf = project_with_one_annotation
        resolved = await pipeline_service.resolve_annotation(
            project.id, None, owner=TEST_OWNER
        )
        assert resolved.id == gtf.id

    async def test_refuses_when_two_distinct_assemblies_are_present(self):
        project = await project_service.create_project(
            name="two-annotations", owner=TEST_OWNER
        )
        await _make_gtf(project, "GCF_000146045.2_R64_genomic.gtf")
        await _make_gtf(project, "GCF_000002445.2_TREU927_genomic.gtf")

        with pytest.raises(ValidationError):
            await pipeline_service.resolve_annotation(project.id, None, owner=TEST_OWNER)

    async def test_an_explicit_id_from_another_project_is_refused(self):
        project_a = await project_service.create_project(
            name="star-idx-a", owner=TEST_OWNER
        )
        project_b = await project_service.create_project(
            name="star-idx-b", owner=TEST_OWNER
        )
        gtf = await _make_gtf(project_b)

        with pytest.raises(ValidationError):
            await pipeline_service.resolve_annotation(
                project_a.id, gtf.id, owner=TEST_OWNER
            )


class TestReferenceIndexStatusReportsBothStarVariants:
    async def test_annotated_and_plain_are_independent_flags(self):
        project = await project_service.create_project(
            name="star-index-status", owner=TEST_OWNER
        )
        reference = await _make_reference(project)

        status = await pipeline_service.reference_index_status(reference)
        assert status["star"] is False
        assert status["star_annotated"] is False

        await make_object(
            project,
            "genome.fna.STARindex.annotated.SA",
            sidecar_of=reference.id,
            sidecar_role=SidecarRole.STAR_ANNOTATED_INDEX,
        )

        status = await pipeline_service.reference_index_status(reference)
        assert status["star"] is False
        assert status["star_annotated"] is True


class TestLaunchBuildIndexRefusesAnnotationForNonStar:
    async def test_bwa_mem2_with_an_annotation_id_is_refused(self):
        project = await project_service.create_project(
            name="bwa-annotation-refused", owner=TEST_OWNER
        )
        reference = await _make_reference(project)
        gtf = await _make_gtf(project)

        with pytest.raises(ValidationError):
            await pipeline_service.launch_build_index(
                reference_id=reference.id,
                owner=TEST_OWNER,
                aligner=Aligner.BWA_MEM2,
                annotation_id=gtf.id,
            )
