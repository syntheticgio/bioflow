"""Indexing a protein FASTA's records.

The scan is tested against real files on disk rather than mocked readers: the
thing most likely to be wrong is byte-offset arithmetic, and a mock that
returns lines cannot get that wrong.
"""

import pytest
from beanie import PydanticObjectId

from app.metadata.protein_headers import RefKind
from app.models import Compression, ProteinRecord
from app.services import protein_record_index as index_mod

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

FASTA = (
    ">sp|P00924|ENO1_YEAST Enolase 1 OS=Saccharomyces cerevisiae\n"
    "MAVSKVYARS\nVYDSRGNPTV\n"
    ">NP_009342.1 Cdc19p [Saccharomyces cerevisiae S288C]\n"
    "MSRLERLTSL\n"
    ">KLLIPMDF_00023 hypothetical protein\n"
    "MKKLLA\n"
)


@pytest.fixture
def fasta_file(tmp_path):
    path = tmp_path / "proteins.faa"
    path.write_text(FASTA)
    return path


@pytest.fixture
def crlf_fasta_file(tmp_path):
    """Same content as FASTA, but with CRLF line endings throughout.

    Written in binary mode with `\\r\\n` explicit, so nothing on the writing
    side normalizes it back to LF before the test can exercise the reader.
    """
    path = tmp_path / "proteins_crlf.faa"
    crlf = FASTA.replace("\n", "\r\n")
    path.write_bytes(crlf.encode("utf-8"))
    return path


def test_scan_reads_identifier_description_and_length(fasta_file):
    records = list(index_mod.scan_records(fasta_file, Compression.NONE))

    assert [r.identifier for r in records] == [
        "sp|P00924|ENO1_YEAST",
        "NP_009342.1",
        "KLLIPMDF_00023",
    ]
    # The description is everything after the first token (R3) -- the part a
    # person picks a protein by, and the part the facts document drops.
    assert records[1].description == "Cdc19p [Saccharomyces cerevisiae S288C]"
    # Length is residues, not bytes: newlines inside the sequence do not count.
    assert records[0].length == 20
    assert records[2].length == 6


def test_scan_records_byte_offsets_that_point_at_the_header(fasta_file):
    """The offset must land on the '>' so a later reader can seek to it."""
    raw = fasta_file.read_bytes()
    for record in index_mod.scan_records(fasta_file, Compression.NONE):
        assert raw[record.byte_offset : record.byte_offset + 1] == b">"


def test_scan_records_byte_offsets_that_point_at_the_header_crlf(crlf_fasta_file):
    """CRLF regression (finding 1): universal-newline translation in text mode
    silently drops the `\\r` from each line before `len(line.encode(...))`
    ever sees it, undercounting every offset after the first CRLF line. Raw
    bytes are the only thing that can't be fooled by that translation, so the
    offsets are checked against `read_bytes()`, not a text-mode re-read.
    """
    raw = crlf_fasta_file.read_bytes()
    records = list(index_mod.scan_records(crlf_fasta_file, Compression.NONE))
    assert len(records) == 3
    for record in records:
        assert raw[record.byte_offset : record.byte_offset + 1] == b">"


def test_scan_attaches_the_parsed_reference(fasta_file):
    records = list(index_mod.scan_records(fasta_file, Compression.NONE))

    assert records[0].ref_kind is RefKind.UNIPROT
    assert records[0].ref_accession == "P00924"
    assert records[1].ref_kind is RefKind.REFSEQ
    assert records[1].ref_accession == "NP_009342"
    # Annotation-tool output names nothing, which is an ordinary outcome.
    assert records[2].ref_kind is None


def test_scan_stops_at_the_cap(fasta_file, monkeypatch):
    """Above the cap the scan stops rather than reading on (R5)."""
    monkeypatch.setattr(index_mod, "MAX_INDEXED_RECORDS", 2)
    records = list(index_mod.scan_records(fasta_file, Compression.NONE))
    assert len(records) == 2


def test_scan_honours_an_explicit_limit(fasta_file):
    records = list(index_mod.scan_records(fasta_file, Compression.NONE, limit=1))
    assert len(records) == 1


async def test_index_writes_one_document_per_record(fasta_file):
    object_id = "507f1f77bcf86cd799439011"
    result = await index_mod.index_protein_records(
        object_id=object_id, path=fasta_file, compression=Compression.NONE
    )

    assert result.indexed == 3
    assert result.truncated is False
    stored = await ProteinRecord.find(
        ProteinRecord.object_id == PydanticObjectId(object_id)
    ).to_list()
    assert len(stored) == 3
    assert {r.ordinal for r in stored} == {0, 1, 2}


async def test_reindexing_replaces_rather_than_appends(fasta_file):
    """R7. The handler promises idempotency; appending would break it.

    Delete-then-insert rather than upsert-per-record: a re-ingest of a file
    that has *changed* must not leave orphaned records from the old one.
    """
    object_id = "507f1f77bcf86cd799439011"
    for _ in range(2):
        await index_mod.index_protein_records(
            object_id=object_id, path=fasta_file, compression=Compression.NONE
        )

    stored = await ProteinRecord.find(
        ProteinRecord.object_id == PydanticObjectId(object_id)
    ).to_list()
    assert len(stored) == 3


async def test_index_reports_truncation_above_the_cap(fasta_file, monkeypatch):
    monkeypatch.setattr(index_mod, "MAX_INDEXED_RECORDS", 2)
    result = await index_mod.index_protein_records(
        object_id="507f1f77bcf86cd799439011",
        path=fasta_file,
        compression=Compression.NONE,
    )
    assert result.indexed == 2
    assert result.truncated is True


async def test_a_file_exactly_at_the_cap_is_not_truncated(fasta_file, monkeypatch):
    """The boundary. A file of exactly N records is complete, and flagging it
    as truncated would warn the user that a complete list is partial.

    This is why the scan runs one past the cap: stopping at exactly the cap
    cannot distinguish the two cases.
    """
    monkeypatch.setattr(index_mod, "MAX_INDEXED_RECORDS", 3)
    result = await index_mod.index_protein_records(
        object_id="507f1f77bcf86cd799439011",
        path=fasta_file,
        compression=Compression.NONE,
    )
    assert result.indexed == 3
    assert result.truncated is False


# --- read_record_sequence (issue #534) ---


def test_read_first_record_sequence(fasta_file):
    records = list(index_mod.scan_records(fasta_file, Compression.NONE))
    seq = index_mod.read_record_sequence(
        fasta_file, Compression.NONE, byte_offset=records[0].byte_offset
    )
    assert seq == "MAVSKVYARSVYDSRGNPTV"


def test_read_middle_record_sequence(fasta_file):
    records = list(index_mod.scan_records(fasta_file, Compression.NONE))
    seq = index_mod.read_record_sequence(
        fasta_file, Compression.NONE, byte_offset=records[1].byte_offset
    )
    assert seq == "MSRLERLTSL"


def test_read_last_record_sequence(fasta_file):
    records = list(index_mod.scan_records(fasta_file, Compression.NONE))
    seq = index_mod.read_record_sequence(
        fasta_file, Compression.NONE, byte_offset=records[2].byte_offset
    )
    assert seq == "MKKLLA"


def test_read_record_sequence_matches_scanned_length(fasta_file):
    """The reader strips line endings the same way the scan does, so the
    sequence length must match the `length` field from scan_records."""
    for record in index_mod.scan_records(fasta_file, Compression.NONE):
        seq = index_mod.read_record_sequence(
            fasta_file, Compression.NONE, byte_offset=record.byte_offset
        )
        assert len(seq) == record.length


def test_read_record_sequence_crlf(crlf_fasta_file):
    records = list(index_mod.scan_records(crlf_fasta_file, Compression.NONE))
    for record in records:
        seq = index_mod.read_record_sequence(
            crlf_fasta_file, Compression.NONE, byte_offset=record.byte_offset
        )
        assert "\r" not in seq


def test_read_record_sequence_from_gzip(tmp_path):
    """A gzipped file is decompressed transparently; offsets are counted in
    the decompressed stream, same as scan_records."""
    import gzip

    path = tmp_path / "proteins.faa.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(FASTA.encode("utf-8"))

    records = list(index_mod.scan_records(path, Compression.GZIP))
    assert [r.identifier for r in records] == [
        "sp|P00924|ENO1_YEAST",
        "NP_009342.1",
        "KLLIPMDF_00023",
    ]

    seq = index_mod.read_record_sequence(
        path, Compression.GZIP, byte_offset=records[2].byte_offset
    )
    assert seq == "MKKLLA"


def test_read_record_sequence_past_eof_returns_empty(fasta_file):
    """An offset past the last record reads nothing without raising."""
    seq = index_mod.read_record_sequence(
        fasta_file, Compression.NONE, byte_offset=999999
    )
    assert seq == ""
