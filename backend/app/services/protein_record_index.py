"""Recording every record of a protein FASTA so the file can be browsed.

Lives here rather than in `storage/parsers.py` on purpose. That module's
docstring commits to being pure -- "All functions are synchronous and
cancellable; callers run them off the loop" -- and every function in it returns
a facts dict and writes nothing. A database write inside it would break a
contract the whole ingest path depends on, so the write happens in the caller
instead.

The scan is a generator. The cap bounds rows, but a list comprehension over a
120,000-record proteome would still hold every record in memory at once before
the cap could apply.

The scan reads its own decompressed byte stream rather than going through
`parsers._open_text`, on purpose: that helper opens in text mode, which
applies universal-newline translation (`\r\n` -> `\n`, silently dropping a
byte per CRLF line) and `errors="replace"` (an invalid byte becomes U+FFFD,
which is 3 bytes in UTF-8 -- 2 bytes more than whatever it replaced). Either
transformation happens before a caller's code ever sees the line, so
`len(line.encode("utf-8"))` computed from the text-mode string does not
reflect the file's real byte layout, and `byte_offset` -- stored specifically
so a later reader can seek directly to one record -- would be silently wrong.
Reading raw bytes here and decoding each line ourselves (also with
`errors="replace"`, for the same reason `_open_text` uses it: a bad file must
not break ingestion) keeps the offsets exact while keeping the same
tolerance for malformed input.
"""

import gzip
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from beanie import PydanticObjectId

from app.logging import get_logger
from app.metadata.protein_headers import RefKind, parse_header
from app.models import Compression, ProteinRecord

log = get_logger(__name__)

# Above the largest realistic input -- human RefSeq is roughly 120,000 records
# -- so an ordinary proteome never trips it, while a pathological file stays
# bounded. Module level so a test can patch it down rather than writing a
# 150,000-record fixture.
MAX_INDEXED_RECORDS = 150_000

# How many documents go to Mongo in one round trip. A record is a small
# document and 150,000 single inserts would dominate the ingest.
_BATCH = 1_000


@dataclass(frozen=True)
class ScannedRecord:
    ordinal: int
    identifier: str
    description: str
    length: int
    byte_offset: int
    # Split into kind/accession rather than kept as one `ProteinRef` -- this is
    # the same shape `ProteinRecord` stores, and a caller building a document
    # from this record needs both fields separately regardless.
    ref_kind: RefKind | None
    ref_accession: str | None


@dataclass(frozen=True)
class IndexResult:
    indexed: int
    truncated: bool


def _open_binary(path: Path, compression: Compression):
    """The decompressed byte stream, undecoded.

    Mirrors `parsers._open_text`'s compression dispatch but stops short of
    text mode, so nothing here is subject to universal-newline translation or
    replacement-character re-encoding -- see the module docstring for why
    that distinction is the whole point.
    """
    if compression in (Compression.GZIP, Compression.BGZF):
        return gzip.open(path, "rb")
    return open(path, "rb")


def scan_records(
    path: Path, compression: Compression, *, limit: int | None = None
) -> Iterator[ScannedRecord]:
    """Every record in the file, up to `limit` (default `MAX_INDEXED_RECORDS`).

    The caller passes `limit=MAX_INDEXED_RECORDS + 1` when it needs to
    distinguish "exactly at the cap" from "over it": a file with exactly
    150,000 records is complete, and reporting it as truncated would put a
    "this list is incomplete" warning on a list that is not.

    Byte offsets are counted in the *decompressed* stream, which is what a
    later reader seeking into the same stream needs. For an uncompressed file
    they are also file offsets; for a gzipped one they are not, and a caller
    that wants random access into a `.gz` has to decompress to reach them.
    That limitation is acceptable here because nothing in this ticket seeks --
    the offsets are recorded for the follow-ups.

    Lines are read and counted as raw bytes, split on `b"\\n"` exactly as they
    sit in the decompressed stream -- a `\\r` before it (CRLF) stays part of
    the line's byte length, and each line is decoded independently with
    `errors="replace"` only for the text fields, never for the offset. That
    keeps a CRLF file's or a non-UTF-8 file's offsets exact even though the
    identifier/description text can still contain U+FFFD, same as before.
    """
    cap = MAX_INDEXED_RECORDS if limit is None else limit
    ordinal = 0
    offset = 0
    pending: dict | None = None

    def finish(record: dict) -> ScannedRecord:
        ref = record["ref"]
        return ScannedRecord(
            ordinal=record["ordinal"],
            identifier=record["identifier"],
            description=record["description"],
            length=record["length"],
            byte_offset=record["offset"],
            ref_kind=ref.kind if ref else None,
            ref_accession=ref.accession if ref else None,
        )

    with _open_binary(path, compression) as fh:
        for raw_line in fh:
            line_bytes = len(raw_line)
            line = raw_line.decode("utf-8", errors="replace")
            if line.startswith(">"):
                if pending is not None:
                    yield finish(pending)
                    ordinal += 1
                    if ordinal >= cap:
                        return
                text = line[1:].strip()
                parts = text.split(maxsplit=1)
                pending = {
                    "ordinal": ordinal,
                    "identifier": parts[0] if parts else "",
                    "description": parts[1] if len(parts) > 1 else "",
                    "length": 0,
                    "offset": offset,
                    "ref": parse_header(text),
                }
            elif pending is not None:
                # Residues, not bytes: the newline is not part of the sequence.
                pending["length"] += len(line.strip())
            offset += line_bytes

    if pending is not None:
        yield finish(pending)


async def index_protein_records(
    *, object_id: PydanticObjectId | str, path: Path, compression: Compression
) -> IndexResult:
    """Replace this object's records with a fresh scan of the file.

    Delete-then-insert rather than upsert-per-record. `ingest_headers` promises
    idempotency, and an append would break it -- but more than that, a
    re-ingest of a *changed* file must not leave records from the previous
    version behind, which an upsert keyed on ordinal would do whenever the new
    file has fewer records than the old one.

    `object_id` is coerced to `PydanticObjectId` up front: the field is typed
    that way on `ProteinRecord`, and a Beanie query built from a plain string
    (`ProteinRecord.object_id == "507f..."`) compares against the string form
    rather than the stored `ObjectId`, so it silently matches nothing -- the
    delete deletes zero rows and the query in Task 4 would find zero records,
    with no error either time.
    """
    object_id = PydanticObjectId(object_id)
    await ProteinRecord.find(ProteinRecord.object_id == object_id).delete()

    indexed = 0
    truncated = False
    batch: list[ProteinRecord] = []
    # One past the cap, so that reaching record 150,001 proves the file has
    # more than the cap holds. Scanning to exactly the cap cannot tell a file
    # of exactly 150,000 records -- which is complete -- from a larger one.
    for scanned in scan_records(path, compression, limit=MAX_INDEXED_RECORDS + 1):
        if scanned.ordinal >= MAX_INDEXED_RECORDS:
            truncated = True
            break
        batch.append(
            ProteinRecord(
                object_id=object_id,
                ordinal=scanned.ordinal,
                identifier=scanned.identifier,
                description=scanned.description,
                length=scanned.length,
                byte_offset=scanned.byte_offset,
                ref_kind=scanned.ref_kind,
                ref_accession=scanned.ref_accession,
            )
        )
        if len(batch) >= _BATCH:
            await ProteinRecord.insert_many(batch)
            indexed += len(batch)
            batch = []

    if batch:
        await ProteinRecord.insert_many(batch)
        indexed += len(batch)

    return IndexResult(indexed=indexed, truncated=truncated)
