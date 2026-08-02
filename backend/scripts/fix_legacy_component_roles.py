"""Re-role NCBI assembly components that were ingested as `reference`.

A one-off data repair, not a feature. The code that produced these rows is
already correct: `ncbi_assembly_components.COMPONENTS` maps `protein` to
PROTEIN and `cds` to TRANSCRIPT, and `results._role_for_component` re-derives
from that table. These rows predate it.

Why it still matters. `protein.faa` and `cds_from_genomic.fna` are FASTA, so a
row carrying `role=reference` reaches the aligner's reference picker and the
suggestion rules as though it were a genome -- the exact hazard CLAUDE.md
records. It also nearly broke genome-size inference for assembly: a protein
FASTA's `total_bases` is 2.9 Mb against a 12.1 Mb yeast genome.

**Matched on filename, deliberately, and only for this repair.** The right
marker is `metadata.sequence_type`, which the component table sets today --
but it is `None` on every affected row here, which is what makes them legacy
in the first place. Filename matching would be the wrong basis for production
code and is the only basis available for repairing rows written before the
better one existed. That is also why this is a script rather than something
that runs at startup: it is aimed at a known population that does not grow.

Dry run by default. `--apply` writes.

Piped in rather than run from a path inside the container: the api container
mounts only `backend/app` and `backend/tests`, and `backend/scripts` is baked
into the image at build time -- so a script added since the last build is not
in there to execute.

    docker exec -i biopipe-api-1 python - \\
        < backend/scripts/fix_legacy_component_roles.py
    docker exec -i biopipe-api-1 python - --apply \\
        < backend/scripts/fix_legacy_component_roles.py
"""

import argparse
import asyncio
import re
import sys

sys.path.insert(0, "/srv")

from app.db.client import connect_to_mongo  # noqa: E402
from app.models import DataObject, ObjectRole  # noqa: E402

# NCBI's own suffixes for the non-genomic components of an assembly. Anchored
# to the end so a project's own file called `my_protein.faa.backup` is not
# swept up.
_PATTERNS: tuple[tuple[re.Pattern, ObjectRole], ...] = (
    (re.compile(r"_protein\.faa(\.gz)?$", re.I), ObjectRole.PROTEIN),
    (re.compile(r"_cds_from_genomic\.fna(\.gz)?$", re.I), ObjectRole.TRANSCRIPT),
    # rna.fna and rna_from_genomic.fna are transcript sequence too. The
    # component table has no `rna` entry -- it is not offered for download
    # today -- so these came from an older path, which is precisely why they
    # need naming here rather than being derivable from the table.
    (re.compile(r"_rna(_from_genomic)?\.fna(\.gz)?$", re.I), ObjectRole.TRANSCRIPT),
)


def intended_role(name: str) -> ObjectRole | None:
    for pattern, role in _PATTERNS:
        if pattern.search(name):
            return role
    return None


async def main(apply: bool) -> int:
    await connect_to_mongo()

    candidates = await DataObject.find(DataObject.role == ObjectRole.REFERENCE).to_list()
    planned, skipped = [], []

    for obj in candidates:
        role = intended_role(obj.name)
        if role is None:
            continue
        # Never overrule a role the user chose. `results.should_assign_reference
        # _role` makes the same promise for ingest, and a repair script that
        # broke it would be worse than the rows it is fixing: someone who
        # deliberately marked a CDS FASTA as a reference had a reason.
        if "role" in (obj.user_touched or []):
            skipped.append(obj)
            continue
        planned.append((obj, role))

    print(f"{len(candidates)} objects roled 'reference'")
    print(f"{len(planned)} to re-role, {len(skipped)} skipped as user-set\n")

    for obj, role in planned:
        print(f"  {obj.name[:56]:58} reference -> {role.value}")
    for obj in skipped:
        print(f"  {obj.name[:56]:58} SKIPPED (role set by hand)")

    if not apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    for obj, role in planned:
        obj.role = role
        await obj.save()
    print(f"\nUpdated {len(planned)} objects.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
