"""Export one project and its descendants to a shareable archive.

The archive documents an analysis for a collaborator to read, check, or
cite. It is not a backup (ops/backup.sh is), and it is not currently
importable -- but it carries a version stamp and preserves ObjectIds so
that an importer stays possible later.

`Share`/`share_service.py` is deliberately not reused: it is
profile-to-profile on one machine and moves no bytes by design, both sides
pointing at the same refcounted blob. Crossing machines is precisely what
it cannot do.

See docs/superpowers/specs/2026-08-17-project-export-archive-design.md.
"""

# Bumped when the archive layout changes in a way a reader must notice.
# Preserved ObjectIds plus this stamp are what a future importer needs.
BIOFLOW_EXPORT_VERSION = 1

# Blobs at or below this size have their bytes packed into the archive;
# larger ones are listed in the manifest as excluded. A collaborator wants
# the derived results, not hundreds of gigabytes of FASTQ.
DEFAULT_BLOB_THRESHOLD_BYTES = 100 * 1024 * 1024
