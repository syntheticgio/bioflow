"""Backfill `molecule_type` and `library_source` for files enriched before
these fields existed.

A one-off data repair, not a feature. `SraMetadata.to_metadata()` now maps
`library_source` onto `molecule_type`/`library_source`, but this only runs on
ingest or on-demand re-ingest -- there is no periodic SRA re-verification job
in this codebase. Files ingested before this change have an SRA accession and
metadata but neither new field.

Re-fetches each candidate's SRA record by its stored accession and re-derives
just the two new keys, so the backfilled values cannot disagree with what a
fresh ingest of the same file would produce today. Never overwrites a value
already present -- defensive even though the source data guarantees no
candidate has one, since this reuses fields also touched by the ordinary
ingest path and a second run of this same script must be a no-op.

Dry run by default. `--apply` writes.

Piped in rather than run from a path inside the container: the api container
mounts only `backend/app` and `backend/tests`, and `backend/scripts` is baked
into the image at build time -- so a script added since the last build is not
in there to execute.

    docker exec -i biopipe-api-1 python - \\
        < backend/scripts/backfill_molecule_type.py
    docker exec -i biopipe-api-1 python - --apply \\
        < backend/scripts/backfill_molecule_type.py
"""

import argparse
import asyncio
import sys

sys.path.insert(0, "/srv")

from app.db.client import connect_to_mongo  # noqa: E402
from app.metadata import sra  # noqa: E402
from app.models import DataObject  # noqa: E402


def _accession(obj: DataObject) -> str | None:
    return (
        obj.metadata.get("sra_run")
        or obj.metadata.get("sra_experiment")
        or obj.metadata.get("sra_sample")
        or obj.metadata.get("sra_study")
    )


async def main(apply: bool) -> int:
    await connect_to_mongo()

    candidates = [
        obj
        for obj in await DataObject.find().to_list()
        if _accession(obj) and not obj.metadata.get("molecule_type")
    ]

    planned, skipped = [], []
    for obj in candidates:
        accession = _accession(obj)
        try:
            meta = sra.lookup(accession)
        except Exception as exc:  # unexpected failure -- report, don't crash the batch
            skipped.append((obj, str(exc)))
            continue
        if meta is None:
            skipped.append((obj, "SRA lookup returned no record"))
            continue
        derived = meta.to_metadata()
        updates = {
            k: v
            for k, v in derived.items()
            if k in ("molecule_type", "library_source") and not obj.metadata.get(k)
        }
        if updates:
            planned.append((obj, updates))
        else:
            skipped.append((obj, "SRA record has no library_source"))

    print(f"{len(candidates)} objects with an SRA accession and no molecule_type")
    print(f"{len(planned)} to backfill, {len(skipped)} skipped\n")

    for obj, updates in planned:
        print(f"  {obj.name[:56]:58} {updates}")
    for obj, reason in skipped:
        print(f"  {obj.name[:56]:58} SKIPPED ({reason})")

    if not apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    for obj, updates in planned:
        obj.metadata = {**obj.metadata, **updates}
        await obj.save()
    print(f"\nUpdated {len(planned)} objects.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
