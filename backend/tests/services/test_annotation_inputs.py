"""Which VCFs can be annotated, and why not when they cannot.

Every unavailable reason is asserted, because the reason *is* the feature: a
card that says "cannot annotate" tells the user nothing, and all three real
projects on this machine are blocked on a different input.
"""

import pytest
import pytest_asyncio

from app.models import ObjectRole
from app.services import pipeline_service, project_service
from tests.services.helpers import make_object

# `beanie_models` is module-scoped and holds a Motor connection bound to that
# scope's loop, so the tests (and the fixtures that touch the database) have
# to run on the same one -- see tests/api/test_object_download.py for the
# same note.
pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

GCF_TBRUCEI = "GCF_000002445.2"
GCF_YEAST = "GCF_000146045.2"


@pytest_asyncio.fixture(loop_scope="module")
async def annotatable_vcf():
    """Reference and matching GFF3 both present, variants called: the
    everything-is-fine case."""
    project = await project_service.create_project(name="annotatable")

    bam = await make_object(project, "sample.bam")

    reference = await make_object(project, "tbrucei.fna")
    reference.role = ObjectRole.REFERENCE
    reference.facts = {"ncbi_assembly_accession": GCF_TBRUCEI}
    await reference.save()

    annotation = await make_object(project, "tbrucei.gff3")
    annotation.role = ObjectRole.ANNOTATION
    annotation.facts = {"ncbi_assembly_accession": GCF_TBRUCEI}
    await annotation.save()

    vcf = await make_object(project, "sample.vcf.gz")
    vcf.role = ObjectRole.VARIANTS
    vcf.derived_from = [bam.id, reference.id]
    vcf.facts = {"vcf_stats_summary": {"variants": 6641}}
    await vcf.save()

    return vcf


@pytest_asyncio.fixture(loop_scope="module")
async def empty_vcf():
    """The real T. brucei case: reference and GFF3 both present, but the
    caller found nothing to call."""
    project = await project_service.create_project(name="empty-vcf")

    bam = await make_object(project, "sample.bam")

    reference = await make_object(project, "tbrucei.fna")
    reference.role = ObjectRole.REFERENCE
    reference.facts = {"ncbi_assembly_accession": GCF_TBRUCEI}
    await reference.save()

    annotation = await make_object(project, "tbrucei.gff3")
    annotation.role = ObjectRole.ANNOTATION
    annotation.facts = {"ncbi_assembly_accession": GCF_TBRUCEI}
    await annotation.save()

    vcf = await make_object(project, "empty.vcf.gz")
    vcf.role = ObjectRole.VARIANTS
    vcf.derived_from = [bam.id, reference.id]
    vcf.facts = {"vcf_stats_summary": {"variants": 0}}
    await vcf.save()

    return vcf


@pytest_asyncio.fixture(loop_scope="module")
async def vcf_without_gff():
    """The real yeast case: a usable reference, no GFF3 anywhere in the
    project."""
    project = await project_service.create_project(name="no-gff")

    bam = await make_object(project, "sample.bam")

    reference = await make_object(project, "yeast.fna")
    reference.role = ObjectRole.REFERENCE
    reference.facts = {"ncbi_assembly_accession": GCF_YEAST}
    await reference.save()

    vcf = await make_object(project, "yeast.vcf.gz")
    vcf.role = ObjectRole.VARIANTS
    vcf.derived_from = [bam.id, reference.id]
    vcf.facts = {"vcf_stats_summary": {"variants": 6641}}
    await vcf.save()

    return vcf


@pytest_asyncio.fixture(loop_scope="module")
async def vcf_without_reference():
    """The reference id in derived_from points nowhere in this project --
    e.g. it was deleted after the VCF was called."""
    project = await project_service.create_project(name="no-reference")

    bam = await make_object(project, "sample.bam")

    vcf = await make_object(project, "orphan.vcf.gz")
    vcf.role = ObjectRole.VARIANTS
    # No reference in derived_from at all -- it was never linked, or its
    # object was deleted out from under this VCF.
    vcf.derived_from = [bam.id]
    vcf.facts = {"vcf_stats_summary": {"variants": 6641}}
    await vcf.save()

    return vcf


@pytest_asyncio.fixture(loop_scope="module")
async def vcf_with_mismatched_gff():
    """A GFF3 exists in the project, but for a different assembly than the
    one this VCF was called against -- must not be paired with it."""
    project = await project_service.create_project(name="mismatched-gff")

    bam = await make_object(project, "sample.bam")

    reference = await make_object(project, "tbrucei.fna")
    reference.role = ObjectRole.REFERENCE
    reference.facts = {"ncbi_assembly_accession": GCF_TBRUCEI}
    await reference.save()

    # A GFF3 for yeast, sitting in the same project as a T. brucei VCF.
    annotation = await make_object(project, "yeast.gff3")
    annotation.role = ObjectRole.ANNOTATION
    annotation.facts = {"ncbi_assembly_accession": GCF_YEAST}
    await annotation.save()

    vcf = await make_object(project, "sample.vcf.gz")
    vcf.role = ObjectRole.VARIANTS
    vcf.derived_from = [bam.id, reference.id]
    vcf.facts = {"vcf_stats_summary": {"variants": 6641}}
    await vcf.save()

    return vcf


class TestResolveAnnotationInputs:
    async def test_resolves_when_reference_and_annotation_are_present(
        self, annotatable_vcf
    ):
        got = await pipeline_service.resolve_annotation_inputs(annotatable_vcf)
        assert got.ok
        assert got.reference is not None
        assert got.annotation is not None
        assert got.reason is None

    # The real T. brucei case: reference and GFF3 both present, nothing called.
    async def test_unavailable_when_the_vcf_has_no_variants(self, empty_vcf):
        got = await pipeline_service.resolve_annotation_inputs(empty_vcf)
        assert not got.ok
        assert "no called variants" in got.reason.lower()

    # The real yeast case.
    async def test_unavailable_when_no_annotation_accompanies_the_reference(
        self, vcf_without_gff
    ):
        got = await pipeline_service.resolve_annotation_inputs(vcf_without_gff)
        assert not got.ok
        assert "annotation" in got.reason.lower()
        # Names the action, not just the absence.
        assert "ncbi" in got.reason.lower()

    async def test_unavailable_when_the_reference_is_not_in_the_project(
        self, vcf_without_reference
    ):
        got = await pipeline_service.resolve_annotation_inputs(vcf_without_reference)
        assert not got.ok
        assert "reference" in got.reason.lower()

    # A GFF3 for a different assembly must not be paired with this reference:
    # csq would run and annotate nothing, which reads as success.
    async def test_ignores_an_annotation_for_a_different_assembly(
        self, vcf_with_mismatched_gff
    ):
        got = await pipeline_service.resolve_annotation_inputs(vcf_with_mismatched_gff)
        assert not got.ok
        assert "annotation" in got.reason.lower()
