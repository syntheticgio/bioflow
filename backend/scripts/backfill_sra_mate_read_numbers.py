"""Set `read_number` on paired-end objects the SRA download path linked without it.

A one-off data repair, not a feature. `queue/results._apply_sra_download` set
`mate_object_id` on both sides of a paired run but never set `read_number` --
`_link_mate` (the generic ingest path) always sets both together, but the SRA
path bypasses it and had its own gap. That gap is fixed at the source now;
this repairs the rows it already wrote. Every pair the SRA path ever linked
has this gap, so it is not bounded to a known population the way
`fix_legacy_component_roles.py`'s is -- but the read numbers are recoverable
without guessing, so this stays a script rather than a migration that runs at
startup.

Derives read_number from `pairing.split_mate(name)`, the same function that
determined the link in the first place, so the backfilled value cannot
disagree with which half of the pair the object actually is.

Dry run by default. `--apply` writes.

Piped in rather than run from a path inside the container: the api container
mounts only `backend/app` and `backend/tests`, and `backend/scripts` is baked
into the image at build time -- so a script added since the last build is not
in there to execute.

    docker exec -i biopipe-api-1 python - \\
        < backend/scripts/backfill_sra_mate_read_numbers.py
    docker exec -i biopipe-api-1 python - --apply \\
        < backend/scripts/backfill_sra_mate_read_numbers.py
"""

import argparse
import asyncio
import sys

sys.path.insert(0, "/srv")

from app.db.client import connect_to_mongo  # noqa: E402
from app.models import DataObject  # noqa: E402
from app.pipelines import pairing  # noqa: E402

_READ_NUMBER = {"R1": 1, "R2": 2}


async def main(apply: bool) -> int:
    await connect_to_mongo()

    candidates = await DataObject.find(
        DataObject.mate_object_id != None,  # noqa: E711
        DataObject.read_number == None,  # noqa: E711
    ).to_list()

    planned, skipped = [], []
    for obj in candidates:
        split = pairing.split_mate(obj.name)
        if split is None:
            skipped.append(obj)
            continue
        planned.append((obj, _READ_NUMBER[split[1]]))

    print(f"{len(candidates)} linked objects with no read_number")
    print(f"{len(planned)} to backfill, {len(skipped)} skipped as unparseable\n")

    for obj, read_number in planned:
        print(f"  {obj.name[:56]:58} read_number -> {read_number}")
    for obj in skipped:
        print(f"  {obj.name[:56]:58} SKIPPED (name does not match a mate token)")

    if not apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    for obj, read_number in planned:
        obj.read_number = read_number
        await obj.save()
    print(f"\nUpdated {len(planned)} objects.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
