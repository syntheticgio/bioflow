# Report Artifacts in Project Export Archives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pack report and analysis-artifact files belonging to exported objects into project archives, with per-file thresholding and typed manifest rows.

**Architecture:** Extend `backend/app/services/export_service.py` with a report-artifact collector and an `ExportArtifact` representation. Keep database blobs and report artifacts distinct in `ExportBundle`, then normalize both into a common artifact list for deterministic manifest rendering, threshold decisions, and tar writing. Report files are stored under `reports/<category>/<object_id>/<relative_path>` and the archive format advances to version 2.

**Tech Stack:** Python 3, `pathlib`, `tarfile`, SHA-256 hashing, pytest/pytest-asyncio, Beanie models, existing BioFlow storage settings and export service.

## Global Constraints

- Include only files below the configured per-file threshold; list excluded files in the manifest.
- Scope discovery to `qc_reports_dir`, `bam_stats_dir`, `vcf_stats_dir`, and `annotation_stats_dir`.
- Include only object directories whose IDs belong to the owner-scoped export bundle; ignore orphan directories.
- Ignore symlinks and reject regular files whose resolved paths escape the expected object directory.
- Preserve archive redaction guarantees; never add absolute source paths or machine identity to exported metadata.
- Bump `BIOFLOW_EXPORT_VERSION` from `1` to `2` because the manifest schema and archive layout change.
- Do not create `Blob` records for report files or build an importer.
- Run backend tests with `./backend/run-worktree-tests.sh`, never against the main stack's API container.

---

## File map

- Modify `backend/app/services/export_service.py`: report roots, artifact dataclasses, discovery, manifest rendering, archive writing, version/count metadata, and README copy.
- Modify `backend/tests/services/test_export_service.py`: discovery, manifest, archive, threshold, safety, and redaction coverage.
- Do not modify the approved design spec unless implementation reveals a genuine design decision that needs recording.

## Task 1: Define and test report artifact discovery

**Files:**
- Modify: `backend/app/services/export_service.py` near `ExportBundle` and `collect()`.
- Test: `backend/tests/services/test_export_service.py` with new filesystem discovery tests.

**Interfaces:**
- Add `ExportArtifact` with `artifact_type`, `artifact_id`, optional `object_id`, `category`, `source_path`, `archive_path`, `size`, `sha256`, `status`, and optional blob `state`.
- Add `report_artifacts: list[ExportArtifact]` to `ExportBundle`.
- Add synchronous `collect_report_artifacts(objects: list[DataObject]) -> list[ExportArtifact]`; call it through `asyncio.to_thread` from async export code.

- [ ] **Step 1: Write failing discovery tests.** Patch all four settings roots to temporary directories. Create an exported object and files at `qc_reports/<id>/fastp.html`, a nested QC path, and one file under each other root. Assert one artifact per regular file, stable categories (`qc`, `bam_stats`, `vcf_stats`, `annotation_stats`), object-relative paths, archive paths under `reports/<category>/<object_id>/`, file sizes, and SHA-256 digests.
- [ ] **Step 2: Run `./backend/run-worktree-tests.sh tests/services/test_export_service.py -q`; verify the new tests fail because the artifact type/helper does not exist.**
- [ ] **Step 3: Implement a module-level tuple describing the four report roots and add the dataclass. Keep report artifacts separate from `ExportBundle.blobs`; do not create fake `Blob` documents.**
- [ ] **Step 4: Implement safe recursive discovery.** For each exported object ID and root, skip missing directories; recurse with `Path.rglob`. Ignore symlinks and non-files. Resolve each regular file and require it to remain below the resolved object directory. Hash in 64 KiB chunks, derive the POSIX relative path, and sort by `(category, object_id, relative_path)`.
- [ ] **Step 5: Add tests for missing roots, orphan object IDs, symlink targets, and escaped paths.** Symlinks return no artifact; a resolved file outside the object directory is rejected. Run the focused suite and require all discovery/safety tests to pass.
- [ ] **Step 6: Commit:** `git add backend/app/services/export_service.py backend/tests/services/test_export_service.py && git commit -m "feat(export): discover object report artifacts"`.

## Task 2: Normalize artifacts and create the version-2 manifest

**Files:**
- Modify: `backend/app/services/export_service.py` at `BIOFLOW_EXPORT_VERSION`, `_MANIFEST_HEADER`, `build_manifest()`, and `export_project()`.
- Test: `backend/tests/services/test_export_service.py` manifest tests.

**Interfaces:**
- Add `artifact_rows(bundle: ExportBundle) -> list[ExportArtifact]`.
- Change `build_manifest(bundle, threshold_bytes)` to return `(str, list[ExportArtifact])`.
- Preserve blob inclusion semantics: blob bytes are included only when `size <= threshold_bytes` and `rel_path` is present.

- [ ] **Step 1: Write failing mixed-manifest tests.** Build a bundle with small/large blobs and small/large report artifacts. Assert one row per artifact, deterministic ordering, object/category/archive metadata for reports, and `included`/`excluded` status at the threshold boundary.
- [ ] **Step 2: Run the focused service suite and verify the blob-only header/signature assertions fail.**
- [ ] **Step 3: Set `BIOFLOW_EXPORT_VERSION = 2` and normalize blobs into `ExportArtifact` values with `artifact_type="blob"`, blob ID, existing `rel_path`, size, digest, and state. Keep report values unchanged.**
- [ ] **Step 4: Replace the manifest header with:**

```python
(
    "artifact_type", "artifact_id", "object_id", "category",
    "source_path", "archive_path", "size", "sha256", "status",
)
```

Render only safe relative source descriptors, never absolute local paths. Sort
by `(artifact_type, category, object_id or "", archive_path, artifact_id)`.

- [ ] **Step 5: Update `manifest.json` counts.** Retain `blob_count` and `blob_threshold_bytes`; add `artifact_count`, `report_artifact_count`, `included_artifact_count`, and `included_report_artifact_count`. Run the focused suite and require all manifest tests to pass.
- [ ] **Step 6: Commit:** `git add backend/app/services/export_service.py backend/tests/services/test_export_service.py && git commit -m "feat(export): add typed report artifact manifest rows"`.

## Task 3: Pack included report files

**Files:**
- Modify: `backend/app/services/export_service.py` in `collect()`, `_write_archive()`, and `export_project()`.
- Test: `backend/tests/services/test_export_service.py` archive integration tests.

**Interfaces:**
- Change `_write_archive(..., included)` to accept the normalized included `ExportArtifact` list.
- `export_project()` must discover reports off the event loop, build one manifest, and pass the same included list to the writer.

- [ ] **Step 1: Write failing archive tests.** Create small and large files under one report directory, export with a small threshold, and assert the small file appears at `reports/qc/<id>/...`, the large file does not, and its manifest row is `excluded`. Add mixed blob/report assertions and require `manifest.json` version 2 with separate report counts.
- [ ] **Step 2: Run the focused suite and verify report tar members are absent or the new writer signature is unwired.**
- [ ] **Step 3: After database collection, call `await asyncio.to_thread(collect_report_artifacts, objects)` and store the result in `ExportBundle.report_artifacts`.**
- [ ] **Step 4: Update `_write_archive()`.** For blob artifacts, resolve the source through `blob_path(blob_id)`. For report artifacts, use the validated `source_path` and `archive_path`. Check `is_file()` before adding; if a discovered file is gone/unreadable, retain its manifest row with `unavailable` or `error` status and continue without a tar member. Render the final manifest after final packability statuses are known.
- [ ] **Step 5: Run `./backend/run-worktree-tests.sh tests/services/test_export_service.py -q`; require all archive, threshold, and version-envelope tests to pass.**
- [ ] **Step 6: Commit:** `git add backend/app/services/export_service.py backend/tests/services/test_export_service.py && git commit -m "feat(export): pack included report artifacts"`.

## Task 4: Update archive documentation and redaction verification

**Files:**
- Modify: `backend/app/services/export_service.py` `_README` and any report copy that still describes omission.
- Test: `backend/tests/services/test_export_service.py` archive documentation and redaction tests.

- [ ] **Step 1: Add failing assertions that README/report mention `reports/<category>/<object_id>/` and no longer say report directories are “not included” or a “known, deliberate gap.”**
- [ ] **Step 2: Replace the `_README` deferral paragraph with the new layout, per-file threshold, and manifest behavior. Keep the archive’s non-importable warning.**
- [ ] **Step 3: Extend the archive-wide redaction test so an included report file is scanned along with every other archive member. Treat report bytes as opaque user-generated artifacts: do not rewrite their contents, and use a benign fixture report. Assert that forbidden values still do not appear in serialized metadata or archive-generated text, while the report file's own bytes are preserved exactly.**
- [ ] **Step 4: Run `./backend/run-worktree-tests.sh tests/services/test_export_service.py -q`; require the full export service suite to pass.**
- [ ] **Step 5: Commit:** `git add backend/app/services/export_service.py backend/tests/services/test_export_service.py && git commit -m "docs(export): describe packed report artifacts"`.

## Task 5: Final verification

**Files:** Verify `backend/app/services/export_service.py`, `backend/tests/services/test_export_service.py`, and the approved design spec.

- [ ] **Step 1: Run `git diff --check`; expected result is no output.**
- [ ] **Step 2: Run `./backend/run-worktree-tests.sh tests/services/test_export_service.py -q`; record the exact passed count.**
- [ ] **Step 3: Run `./backend/run-worktree-tests.sh tests/services/test_object_deletion.py tests/services/test_drift_service.py -q`; require no report-directory cleanup or drift regressions.**
- [ ] **Step 4: Inspect the final diff against the spec. Confirm coverage for per-file rows, per-file thresholding, object matching, orphan exclusion, symlink/containment safety, version 2, report paths, explicit unavailable/error status, documentation, and redaction tests.**
- [ ] **Step 5: If a final correction is necessary, stage `backend/app/services/export_service.py` and `backend/tests/services/test_export_service.py` and commit it separately with `git commit -m "test(export): verify report artifact archive behavior"`; otherwise leave the task commits unchanged.**
