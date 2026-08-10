"""launch_transcript_qc's GTF validation.

The GTF is caller-chosen (see launch_transcript_qc's docstring), so it needs
the same validation `resolve_annotation` already gives STAR's explicit
annotation_id path -- format, readiness, and project match -- rather than
the bare `get_object` + manual project-id check this once did, which let any
object from the right project through (wrong format, wrong status) and
surfaced the mistake only later, inside pysam's GTF parse.

Mirrors test_star_annotated_index.py's DB-backed pattern, since this launch
function (like launch_build_index) needs a real project/object graph to
exercise resolve_annotation, not just the pure decision functions covered by
test_bam_stats_launch.py's FakeObject approach.
"""

import pytest
import pytest_asyncio

from app.errors import ValidationError
from app.models import FormatKind
from app.services import pipeline_service, project_service
from tests.services.helpers import TEST_OWNER, make_object

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def _make_rna_bam(project, name="aligned.bam"):
    bam = await make_object(project, name)
    bam.format.kind = FormatKind.BAM
    bam.facts = {"sort_order": "coordinate"}
    bam.metadata = {"molecule_type": "RNA"}
    await bam.save()
    return bam


async def _make_gtf(project, name="genes.gtf"):
    gtf = await make_object(project, name)
    gtf.format.kind = FormatKind.GTF
    await gtf.save()
    return gtf


class TestLaunchTranscriptQcValidatesTheAnnotation:
    @pytest_asyncio.fixture(loop_scope="module")
    async def project_with_bam(self, request):
        project = await project_service.create_project(
            name=f"transcript-qc-launch-{request.node.name}", owner=TEST_OWNER
        )
        bam = await _make_rna_bam(project)
        return project, bam

    async def test_rejects_an_object_that_is_not_a_gtf(self, project_with_bam):
        project, bam = project_with_bam
        fasta = await make_object(project, "genome.fna")
        fasta.format.kind = FormatKind.FASTA
        await fasta.save()

        with pytest.raises(ValidationError, match="not a GTF or GFF"):
            await pipeline_service.launch_transcript_qc(
                object_id=bam.id, gtf_object_id=fasta.id, owner=TEST_OWNER
            )

    async def test_rejects_a_gtf_from_a_different_project(self, project_with_bam, request):
        project, bam = project_with_bam
        other_project = await project_service.create_project(
            name=f"transcript-qc-launch-other-{request.node.name}", owner=TEST_OWNER
        )
        gtf = await _make_gtf(other_project)

        with pytest.raises(ValidationError, match="same project"):
            await pipeline_service.launch_transcript_qc(
                object_id=bam.id, gtf_object_id=gtf.id, owner=TEST_OWNER
            )

    # No "accepts a valid GTF" case here: getting past this point exercises
    # _resolve_readable against a real managed blob on disk, which is
    # storage infrastructure this test file doesn't otherwise set up (see
    # test_bam_stats_launch.py's note that launch_* wrappers with a
    # DB-and-storage round trip are covered by manual verification rather
    # than a unit test). The two rejection cases above are what the
    # resolve_annotation switch is actually meant to fix.
