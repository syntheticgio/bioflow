"""One-off repair for blobs wrongly marked MISSING while their bytes are present.

See docs/TODO.md entry on the 2026-08-05 verifier anomaly: 45 blobs ended up
with state=missing and miss_count=1, a combination verify_files' own two-strike
logic cannot produce (state only flips to MISSING on a *second* miss, which
would leave miss_count>=2). Because verify_files excludes MISSING blobs from
its rotation (`Blob.state != BlobState.MISSING`), these can never self-heal.

This mirrors the verifier's own "present" branch: stat the file, and if it's
there, heal the blob back to PRESENT and any dependent objects back to READY.
Run inside the api container:

    docker exec -e PYTHONPATH=/srv biopipe-api-1 python3 scripts/repair_missing_blobs.py
    docker exec -e PYTHONPATH=/srv biopipe-api-1 python3 scripts/repair_missing_blobs.py --dry-run
"""

import argparse
import asyncio
from datetime import UTC, datetime

from app.db.client import connect_to_mongo, get_db
from app.models import BlobState, BlobStorage, ObjectStatus
from app.storage.paths import blob_path


async def main(dry_run: bool) -> None:
    await connect_to_mongo()
    db = get_db()

    now = datetime.now(UTC)
    cursor = db.blobs.find({"state": BlobState.MISSING.value})
    blobs = await cursor.to_list(length=None)
    print(f"found {len(blobs)} blobs in state=missing")

    healed = still_missing = skipped_external = 0

    for blob in blobs:
        digest = blob["_id"]

        if blob.get("storage") == BlobStorage.EXTERNAL.value:
            path_str = blob.get("external_path")
            if not path_str:
                skipped_external += 1
                continue
            from pathlib import Path

            path = Path(path_str)
        else:
            path = blob_path(digest)

        try:
            stat = path.stat()
        except OSError:
            still_missing += 1
            print(f"  still missing: {digest} ({path})")
            continue

        if blob.get("size") is not None and stat.st_size != blob["size"]:
            print(
                f"  size mismatch, not healing: {digest} "
                f"(recorded {blob['size']}, actual {stat.st_size})"
            )
            still_missing += 1
            continue

        healed += 1
        print(f"  healing: {digest} ({path}, {stat.st_size} bytes)")

        if dry_run:
            continue

        await db.blobs.update_one(
            {"_id": digest},
            {
                "$set": {
                    "state": BlobState.PRESENT.value,
                    "miss_count": 0,
                    "last_verified_at": now,
                    "updated_at": now,
                }
            },
        )
        await db.objects.update_many(
            {"blob_sha256": digest, "status": ObjectStatus.MISSING.value},
            {"$set": {"status": ObjectStatus.READY.value, "updated_at": now}},
        )

    print(
        f"done: healed={healed} still_missing={still_missing} "
        f"skipped_external_no_path={skipped_external} dry_run={dry_run}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
