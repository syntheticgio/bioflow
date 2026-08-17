"""The protein record collection's shape and its indexes.

`ALL_MODELS` is what `init_beanie` registers, so a model missing from it has
no collection and no indexes -- the failure is silent at write time, which is
why registration is asserted here rather than assumed.
"""

import pytest
from app.models import ALL_MODELS, ProteinRecord
from app.metadata.protein_headers import RefKind

pytestmark = pytest.mark.usefixtures("beanie_models")


def test_registered_in_all_models():
    """A model absent from ALL_MODELS never gets its indexes created."""
    assert ProteinRecord in ALL_MODELS


def test_collection_name():
    assert ProteinRecord.Settings.name == "protein_records"


def test_declares_object_ordinal_and_identifier_indexes():
    """Two indexes, because paging and search are different queries.

    `(object_id, ordinal)` orders the list and enforces uniqueness;
    `(object_id, identifier)` serves identifier search without a collection
    scan at the 150,000-record cap.
    """
    names = {ix.document["name"] for ix in ProteinRecord.Settings.indexes}
    assert "uniq_object_ordinal" in names
    assert "object_identifier" in names

    uniq = next(
        ix for ix in ProteinRecord.Settings.indexes
        if ix.document["name"] == "uniq_object_ordinal"
    )
    assert uniq.document.get("unique") is True


def test_ref_fields_are_optional():
    """A record whose header named no identifier is still a record (R12).

    This is every record of a de-novo annotated proteome, so it must be
    constructible without a reference rather than being a validation error.
    """
    record = ProteinRecord(
        object_id="507f1f77bcf86cd799439011",
        ordinal=0,
        identifier="KLLIPMDF_00023",
        description="hypothetical protein",
        length=143,
        byte_offset=0,
    )
    assert record.ref_kind is None
    assert record.ref_accession is None


def test_carries_a_parsed_reference_when_there_is_one():
    record = ProteinRecord(
        object_id="507f1f77bcf86cd799439011",
        ordinal=1,
        identifier="NP_009342.1",
        description="Cdc19p [Saccharomyces cerevisiae S288C]",
        length=500,
        byte_offset=4096,
        ref_kind=RefKind.REFSEQ,
        ref_accession="NP_009342",
    )
    assert record.ref_kind is RefKind.REFSEQ
    assert record.ref_accession == "NP_009342"
