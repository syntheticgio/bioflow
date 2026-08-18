# Pack report artifacts into project export archives

**Issue:** [#544](https://github.com/syntheticgio/bioflow/issues/544)
**Date:** 2026-08-18
**Status:** design approved, ready for implementation plan

## Problem

The per-project export archive includes database records, ordinary blob-store
files, and sidecar objects, but it omits the report and analysis-artifact
directories stored outside the blob store. The affected roots are
`qc_reports_dir`, `bam_stats_dir`, `vcf_stats_dir`, and
`annotation_stats_dir`. Each root contains directories keyed by `DataObject`
ID, so the archive can currently contain an object without the report that
makes its result useful to a recipient.

Issue #544 is the implementation follow-up to the project export design's
known gap. The export remains read-only documentation; no importer is added.

## Goals and non-goals

The exporter must:

1. Include report-directory files belonging to exported objects.
2. List every discovered report file in `data-manifest.tsv`, whether its
   bytes are included or excluded by threshold.
3. Apply the existing byte threshold independently to each report file.
4. Preserve enough object, category, and relative-path metadata for a
   recipient to understand each artifact.
5. Keep owner scoping and archive redaction guarantees intact.

The exporter will not include orphan report directories, add a new report
directory root, create blob records for report files, or build an import path.

## Decisions

### One manifest row per file

Each regular file below an exported object's report directory gets one
manifest row. A directory-level row would hide which files were included and
would make partial threshold inclusion ambiguous.

Report files retain their path relative to the object directory. They are
stored in the archive under:

```text
reports/<category>/<object_id>/<relative_path>
```

The four categories are stable labels corresponding to the configured roots:
`qc`, `bam_stats`, `vcf_stats`, and `annotation_stats`.

### Per-file thresholding

The existing threshold applies independently to every report file, just as it
does to blob-store files. A directory may therefore contain both included and
excluded files. This allows small HTML, TSV, JSON, or image outputs to remain
useful even when a sibling SQLite database or large artifact exceeds the
threshold.

### Object matching and orphan handling

The collector walks only the four configured report roots and only considers
`<root>/<object_id>/` directories where the object ID belongs to the
owner-scoped export bundle. Missing directories are normal and produce no
rows. Directories for objects outside the bundle are ignored, including
objects belonging to another owner.

The collector recursively discovers regular files and ignores symlinks. Every
resolved regular-file path must remain within the expected object directory;
files that fail that containment check are rejected. This prevents archive
traversal and prevents a report symlink from escaping the report root.

### A common export-artifact abstraction

Report files must not be represented as fake `Blob` records: they are not
content-addressed, are not refcounted, and do not belong in serialized blob
metadata. Instead, introduce a small `ExportArtifact` abstraction containing:

- artifact type (`blob` or `report`);
- stable artifact ID;
- optional source object ID;
- report category when applicable;
- source path and archive path;
- byte size and SHA-256 digest;
- inclusion/status decision.

`ExportBundle` keeps database blobs and report artifacts as distinct fields,
while manifest rendering and archive packing operate over a normalized list of
`ExportArtifact` values. This makes threshold and sorting behavior shared
without erasing the storage distinction between blobs and reports.

### Version 2 manifest

The archive layout and manifest schema change materially, so
`BIOFLOW_EXPORT_VERSION` increases from 1 to 2. The manifest becomes a typed
artifact manifest with columns equivalent to:

```text
artifact_type  artifact_id  object_id  category  source_path  archive_path  size  sha256  status
```

Blob rows retain their existing identity, state, and digest information. Report
rows use the report file's digest as their artifact identity and include the
object/category fields needed to map them back to the source object. All rows
use `included`, `excluded`, or an explicit `unavailable`/`error` status rather
than silently disappearing.

## Data flow

1. `collect()` gathers the owner-scoped project descendants and their
   `DataObject`s as it does today.
2. A report-artifact collector receives the exported object IDs and walks each
   configured report root.
3. For each regular report file, the collector validates containment, records
   its category/object/relative path, computes size and SHA-256, and creates an
   `ExportArtifact`.
4. The manifest builder combines blob and report artifacts, sorts them
   deterministically, and applies the per-file threshold.
5. `_write_archive()` packs included blobs under `blobs/` and included report
   files under `reports/<category>/<object_id>/`.
6. `manifest.json` records separate blob/report counts and total artifact
   counts, along with the version-2 format marker and threshold.
7. The README and report remove the deferral note and describe the `reports/`
   archive layout.

## Error handling

Missing report directories are expected because report generation is optional.
They produce no manifest rows.

If a discovered report file disappears or becomes unreadable between
collection and archive writing, its manifest row remains with an explicit
unavailable/error status and the export continues. The exporter must never
silently omit a file that it reported as discovered.

Archive paths are generated from validated category, object ID, and
object-relative paths; user-controlled absolute paths are never used as tar
member names. Report contents must continue to be covered by the archive-wide
redaction verification, and report metadata must not introduce absolute
filesystem paths or machine identity.

## Testing

Add focused tests for:

- discovery under all four configured roots;
- nested report files and deterministic ordering;
- missing roots and missing object directories;
- orphan object IDs being excluded;
- symlinks and containment/traversal rejection;
- per-file inclusion and exclusion within one directory;
- report digest and size recording;
- the version-2 manifest header and mixed blob/report rows;
- archive members under `reports/<category>/<object_id>/`;
- excluded report files appearing in the manifest but not the tarball;
- explicit unavailable/error handling when a file cannot be packed;
- README/report removal of the deferral text;
- redaction scanning across included report bytes.

Existing blob export tests should remain valid after adapting them to the
version-2 manifest shape, with additional mixed-artifact coverage at the
archive boundary.

## Review checklist

- [ ] Every discovered report file has exactly one manifest row.
- [ ] Every row identifies whether bytes are included, excluded, or
      unavailable.
- [ ] Report bytes are thresholded per file.
- [ ] Only report directories for exported, owner-scoped objects are read.
- [ ] Symlink and path-containment checks prevent archive escape.
- [ ] Archive format version is 2 and documentation no longer calls report
      packing a deferred gap.
