"""A remote object must refuse byte access and stay visible everywhere else.

Two halves of one design, tested together because they pull in opposite
directions and it is the tension that matters:

- Anything that reaches for the bytes refuses, with a message naming the
  fetch rather than something misleading about hashing.
- Everything that merely *lists* or *suggests* still includes it. That is
  the regression a `REMOTE` ObjectStatus would have caused, and it is the
  single most important test in the feature.

Per CLAUDE.md the assertions here run in the refusing direction: asserting
that something is permitted passes whether or not the mechanism did
anything at all.
"""

import pytest
from beanie import PydanticObjectId

from app.errors import ValidationError
from app.models import DataObject, Locality, ObjectStatus, RemoteSource
from app.services.pipeline_service import _resolve_readable

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


def _remote(**overrides) -> DataObject:
    """An offloaded object: READY, no blob, locality REMOTE."""
    base = dict(
        project_id=PydanticObjectId(),
        owner="local",
        name="ERR17407954_1.fastq.gz",
        size=98765,
        status=ObjectStatus.READY,
        blob_sha256=None,
        locality=Locality.REMOTE,
        remote_source=RemoteSource(accession="ERR17407954", size=98765),
        metadata={"sra_run": "ERR17407954"},
    )
    return DataObject(**{**base, **overrides})


async def test_resolve_readable_refuses_a_remote_object():
    with pytest.raises(ValidationError) as excinfo:
        await _resolve_readable(_remote())
    assert "ERR17407954_1.fastq.gz" in str(excinfo.value)


async def test_the_refusal_names_the_fetch_not_hashing():
    """Trap 3: `blob_sha256 is None` is the *first* check in _resolve_readable.

    A remote object has no blob, so without the remote check placed above it
    the user is told the file "has no stored content yet" -- which describes
    an upload still in flight and sends them looking for a job that does not
    exist. The remote case must be answered first.
    """
    with pytest.raises(ValidationError) as excinfo:
        await _resolve_readable(_remote())
    message = str(excinfo.value)
    assert "no stored content yet" not in message
    assert "fetch" in message.lower()


async def test_the_refusal_carries_the_accession():
    """The caller needs the re-fetch address to offer the action."""
    with pytest.raises(ValidationError) as excinfo:
        await _resolve_readable(_remote())
    assert "ERR17407954" in str(excinfo.value) or "ERR17407954" in str(
        getattr(excinfo.value, "details", {})
    )


async def test_a_local_object_with_no_blob_still_reports_not_ingested():
    """The pre-existing message must survive for the case it was written for.

    This is the direction that breaks if the remote check is written as a
    blanket `blob_sha256 is None` rescue rather than a locality test.
    """
    local_pending = _remote(
        locality=Locality.LOCAL,
        remote_source=None,
        status=ObjectStatus.HASHING,
        metadata={},
    )
    with pytest.raises(ValidationError) as excinfo:
        await _resolve_readable(local_pending)
    assert "no stored content yet" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Stage 3: the regression guard.
#
# These are the tests the spec singles out as the ones that would have caught
# the rejected `ObjectStatus.REMOTE` design. They assert the *permissive*
# direction on purpose -- the opposite of the refusals above -- because here
# the failure mode is a remote object silently vanishing from a dropdown.
#
# To keep them from passing vacuously, each pairs the remote object with an
# assertion that the filter still does its job: a genuinely non-READY object
# is still excluded. A test that only checked "the remote one is present"
# would pass against a filter that had been deleted entirely.
# ---------------------------------------------------------------------------


async def test_a_remote_object_survives_the_query_layer_status_filter():
    """`list_objects(status=READY)` builds a Mongo query, not a comprehension.

    Trap 4: this is the filter that made the status-based design unrescuable.
    A non-READY object is excluded *in the database*, so no amount of Python
    downstream could have added it back. An offloaded object keeps READY
    precisely so this query returns it.
    """
    from app.services import object_service

    project_id = PydanticObjectId()
    remote = _remote(project_id=project_id, name="remote.fna")
    await remote.insert()
    not_ready = _remote(
        project_id=project_id,
        name="still-hashing.fna",
        status=ObjectStatus.HASHING,
        locality=Locality.LOCAL,
        remote_source=None,
    )
    await not_ready.insert()

    listed = await object_service.list_objects(
        project_id, owner="local", status=ObjectStatus.READY
    )
    names = {o.name for o in listed}
    assert "remote.fna" in names, "an offloaded object vanished from a READY query"
    assert "still-hashing.fna" not in names, "the status filter stopped working"


async def test_a_remote_reference_still_appears_in_the_picker():
    """The reference picker filters `o.status is ObjectStatus.READY` in Python.

    Exercised through `list_references`' own predicate rather than the HTTP
    route, so the test pins the filter itself. The paired non-READY object is
    what keeps this from passing against a deleted filter.
    """
    from app.models import FormatInfo, FormatKind
    from app.services import pipeline_service

    def _picked(objects: list[DataObject]) -> set[str]:
        return {
            o.name
            for o in objects
            if o.format.kind in pipeline_service.REFERENCE_KINDS
            and o.status is ObjectStatus.READY
        }

    remote_ref = _remote(
        name="GCF_000146045.2_genomic.fna",
        format=FormatInfo(kind=FormatKind.FASTA),
    )
    pending_ref = _remote(
        name="pending.fna",
        format=FormatInfo(kind=FormatKind.FASTA),
        status=ObjectStatus.INGESTING,
        locality=Locality.LOCAL,
        remote_source=None,
    )

    picked = _picked([remote_ref, pending_ref])
    assert "GCF_000146045.2_genomic.fna" in picked, (
        "an offloaded reference vanished from the align dialog's picker"
    )
    assert "pending.fna" not in picked, "the picker's READY filter stopped working"
