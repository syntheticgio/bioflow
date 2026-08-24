"""Backfill `sequence_roles` for references enriched before roles existed.

A one-off data repair, not a feature. The chromosome strip draws only an
assembly's actual chromosomes when `facts.sequence_roles` says which sequences
those are, and falls back to length ranking when it does not. Enrichment writes
the fact from now on, but that only runs on ingest or on-demand re-ingest --
references already in the database keep the length-ranked strip, and, worse,
the `sequence_labels` written beside it may be the broken pre-#836 map that
labelled one chromosome's scaffolds and nothing else.

Re-fetches each candidate's sequence report by its stored assembly accession
and re-derives both facts together, exactly as `enrich_from_assembly` does now,
so a backfilled reference cannot disagree with a fresh ingest of the same file.

`sequence_labels` is overwritten rather than preserved. That is the point: the
existing map is the bug. Every other fact is left alone.

A reference whose lookup fails keeps what it has -- a failed network call must
never turn a working strip into a broken one.

Dry run by default. `--apply` writes.

Piped in rather than run from a path inside the container: the api container
mounts only `backend/app` and `backend/tests`, and `backend/scripts` is baked
into the image at build time -- so a script added since the last build is not
in there to execute.

Call the interpreter by its full path. A bare `python` in this container is
the medaka venv (`/opt/medaka/env/bin/python`), which has none of the app's
dependencies and fails on `import beanie`.

    docker exec -i biopipe-api-1 /usr/local/bin/python3.12 - \\
        < backend/scripts/backfill_sequence_roles.py
    docker exec -i biopipe-api-1 /usr/local/bin/python3.12 - --apply \\
        < backend/scripts/backfill_sequence_roles.py
"""

import argparse
import asyncio
import sys

sys.path.insert(0, "/srv")

from app.db.client import connect_to_mongo  # noqa: E402
from app.metadata import enrich, ncbi_assembly  # noqa: E402
from app.models import DataObject, SequenceType  # noqa: E402


def _accession(obj: DataObject) -> str | None:
    """The assembly this reference came from, however it was recorded.

    Enrichment writes the fact; the metadata key is what a user editing the
    record by hand fills in. Either resolves to one assembly.
    """
    candidate = obj.facts.get("ncbi_assembly_accession") or obj.metadata.get(
        "assembly_accession"
    )
    if not isinstance(candidate, str):
        return None
    return candidate if ncbi_assembly.is_valid_accession(candidate) else None


def _is_genomic(obj: DataObject) -> bool:
    """Whether this file holds the assembly's chromosomes at all.

    Every file NCBI ships for an assembly carries the same accession, so
    `cds_from_genomic.fna` and `protein.faa` are candidates by accession alone
    -- and roles written onto them would claim their coding records are
    chromosomes. The user's own answer wins over the filename convention, as
    everywhere else.
    """
    declared = obj.metadata.get("sequence_type")
    if declared:
        return declared == SequenceType.GENOMIC
    detected = enrich.detect_sequence_type(
        filename=obj.name,
        existing_metadata=obj.metadata,
        format_kind=obj.format.kind if obj.format else None,
    )
    # None means the filename says nothing either way; the sequence facts on a
    # genomic file are what the strip reads, so let it through and let the
    # role lookup decide.
    return detected in (None, SequenceType.GENOMIC)


async def main(apply: bool) -> int:
    await connect_to_mongo()

    all_objects = await DataObject.find().to_list()
    # Only references that can actually draw a strip: one with no sequence
    # lengths has nothing to label, and re-fetching for it would spend an NCBI
    # request to change nothing on screen.
    drawable = [
        obj
        for obj in all_objects
        if obj.facts.get("sequence_lengths") and not obj.facts.get("sequence_roles")
    ]
    candidates = [obj for obj in drawable if _accession(obj) and _is_genomic(obj)]

    planned, skipped = [], []

    for obj in drawable:
        if _accession(obj) is None:
            skipped.append((obj, "no assembly accession -- nothing to look up"))
        elif not _is_genomic(obj):
            skipped.append((obj, "not a genomic file -- holds no chromosomes"))

    for obj in candidates:
        accession = _accession(obj)
        roles = ncbi_assembly.lookup_sequence_roles(accession)
        if not roles:
            skipped.append((obj, f"no sequence report for {accession}"))
            continue
        # Distinct records, not keys: one record is stored under both its
        # RefSeq and its GenBank accession, which would double every count.
        core = len({id(e) for e in roles.values() if e["core"]})
        total = len({id(e) for e in roles.values()})
        planned.append((obj, roles, core, total))

    print(f"{len(candidates)} references with an assembly accession and no sequence_roles")
    print(f"{len(planned)} to backfill, {len(skipped)} skipped\n")

    for obj, _roles, core, total in planned:
        print(f"  {obj.name[:56]:58} {core} chromosomes of {total} sequences")
    for obj, reason in skipped:
        print(f"  {obj.name[:56]:58} SKIPPED ({reason})")

    if not apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    for obj, roles, _core, _total in planned:
        obj.facts = {
            **obj.facts,
            "sequence_roles": roles,
            # Re-derived from the same response, replacing the pre-#836 map.
            "sequence_labels": {
                accession: entry["label"] for accession, entry in roles.items()
            },
        }
        await obj.save()
    print(f"\nUpdated {len(planned)} references.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
