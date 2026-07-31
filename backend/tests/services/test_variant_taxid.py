"""Resolving the organism taxid behind a VCF.

The taxid scopes the UniProt gene lookup that the structure viewer is built
on. Getting it wrong is not a degraded answer but a wrong one: gene symbols
collide across organisms far more often than within one, so an unscoped query
can return a completely unrelated protein. Every branch here therefore
distinguishes "no taxid" from "some taxid", never falling back to a guess.

`tax_id` lives in `metadata`, not `facts` -- checked against all seven
reference objects on this machine, where it is populated and `facts.tax_id` is
absent everywhere.
"""

import pytest
import pytest_asyncio

from app.models import ObjectRole
from app.services import pipeline_service, project_service
from tests.services.helpers import make_object

# `beanie_models` is module-scoped and holds a Motor connection bound to that
# scope's loop, so the tests have to run on the same one -- see
# test_annotation_inputs.py for the same note.
pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

YEAST_TAXID = 559292
TBRUCEI_TAXID = 185431


async def _vcf_with_reference(project_name, *, metadata, role=ObjectRole.REFERENCE):
    """A VCF derived from a BAM and a reference carrying `metadata`."""
    project = await project_service.create_project(name=project_name)
    bam = await make_object(project, "sample.bam")

    reference = await make_object(project, "genome.fna")
    reference.role = role
    reference.metadata = metadata
    await reference.save()

    vcf = await make_object(project, "sample.vcf.gz")
    vcf.role = ObjectRole.VARIANTS
    vcf.derived_from = [bam.id, reference.id]
    await vcf.save()
    return vcf


@pytest_asyncio.fixture(loop_scope="module")
async def vcf_with_taxid():
    return await _vcf_with_reference(
        "taxid-present", metadata={"tax_id": YEAST_TAXID, "organism": "S. cerevisiae"}
    )


@pytest_asyncio.fixture(loop_scope="module")
async def vcf_without_taxid():
    """A local assembly: named, but never enriched from NCBI."""
    return await _vcf_with_reference(
        "taxid-absent", metadata={"organism": "Saccharomyces cerevisiae"}
    )


@pytest_asyncio.fixture(loop_scope="module")
async def vcf_without_reference():
    """An uploaded VCF, carrying no record of what it was called against."""
    project = await project_service.create_project(name="no-reference")
    vcf = await make_object(project, "uploaded.vcf.gz")
    vcf.role = ObjectRole.VARIANTS
    await vcf.save()
    return vcf


async def test_resolves_taxid_from_the_reference(vcf_with_taxid):
    assert await pipeline_service.taxid_for_vcf(vcf_with_taxid) == YEAST_TAXID


async def test_no_taxid_when_the_reference_has_none(vcf_without_taxid):
    """An organism *name* is not a substitute.

    Resolving a name to a taxid is a second remote lookup with its own failure
    modes, and "Saccharomyces cerevisiae" alone does not distinguish the S288C
    strain the callset used. None is the honest answer.
    """
    assert await pipeline_service.taxid_for_vcf(vcf_without_taxid) is None


async def test_no_taxid_when_there_is_no_reference(vcf_without_reference):
    assert await pipeline_service.taxid_for_vcf(vcf_without_reference) is None


async def test_ignores_non_reference_parents():
    """The BAM parent must not be mistaken for the reference.

    A VCF's `derived_from` holds both, and an annotated VCF additionally holds
    its unannotated parent and the GFF3 -- four parents, one of which is the
    reference.
    """
    project = await project_service.create_project(name="mixed-parents")

    bam = await make_object(project, "sample.bam")
    bam.metadata = {"tax_id": 9606}  # a wrong answer sitting in reach
    await bam.save()

    reference = await make_object(project, "genome.fna")
    reference.role = ObjectRole.REFERENCE
    reference.metadata = {"tax_id": TBRUCEI_TAXID}
    await reference.save()

    vcf = await make_object(project, "sample.vcf.gz")
    vcf.role = ObjectRole.VARIANTS
    # Reference deliberately last: taking the first parent would pass by luck.
    vcf.derived_from = [bam.id, reference.id]
    await vcf.save()

    assert await pipeline_service.taxid_for_vcf(vcf) == TBRUCEI_TAXID


async def test_annotated_vcf_reaches_its_reference():
    """An annotated VCF derives from the *unannotated* VCF, not the BAM.

    This is the shape `bcftools csq` output actually has on this machine, and
    the reference is still a direct parent -- so the walk needs no recursion.
    """
    project = await project_service.create_project(name="annotated-vcf")

    parent_vcf = await make_object(project, "sample.vcf.gz")
    parent_vcf.role = ObjectRole.VARIANTS
    await parent_vcf.save()

    reference = await make_object(project, "genome.fna")
    reference.role = ObjectRole.REFERENCE
    reference.metadata = {"tax_id": YEAST_TAXID}
    await reference.save()

    annotation = await make_object(project, "genomic.gff")
    annotation.role = ObjectRole.ANNOTATION
    await annotation.save()

    csq = await make_object(project, "sample.csq.vcf.gz")
    csq.role = ObjectRole.VARIANTS
    csq.derived_from = [parent_vcf.id, reference.id, annotation.id]
    await csq.save()

    assert await pipeline_service.taxid_for_vcf(csq) == YEAST_TAXID


async def test_non_integer_taxid_is_rejected():
    """Metadata is hand-editable, so the field can hold anything.

    A string that happens to parse is still not something to pass into a
    remote query unchecked, and a non-numeric one must not raise.
    """
    project = await project_service.create_project(name="bad-taxid")
    reference = await make_object(project, "genome.fna")
    reference.role = ObjectRole.REFERENCE
    reference.metadata = {"tax_id": "not a number"}
    await reference.save()

    vcf = await make_object(project, "sample.vcf.gz")
    vcf.role = ObjectRole.VARIANTS
    vcf.derived_from = [reference.id]
    await vcf.save()

    assert await pipeline_service.taxid_for_vcf(vcf) is None


async def test_numeric_string_taxid_is_accepted():
    """The NCBI path stores an int, but a hand-entered value arrives as text
    and means the same thing."""
    project = await project_service.create_project(name="string-taxid")
    reference = await make_object(project, "genome.fna")
    reference.role = ObjectRole.REFERENCE
    reference.metadata = {"tax_id": str(YEAST_TAXID)}
    await reference.save()

    vcf = await make_object(project, "sample.vcf.gz")
    vcf.role = ObjectRole.VARIANTS
    vcf.derived_from = [reference.id]
    await vcf.save()

    assert await pipeline_service.taxid_for_vcf(vcf) == YEAST_TAXID
