# Task 1 implementation report

## Summary

Implemented the report-artifact discovery slice for issue #544, Task 1.

### Code

- Added `ExportArtifact` to `backend/app/services/export_service.py`.
- Added `report_artifacts` to `ExportBundle`.
- Added the module-level `REPORT_ARTIFACT_ROOTS` tuple for the four report roots.
- Added synchronous `collect_report_artifacts(objects)` with:
  - recursive discovery under each object-specific report directory,
  - regular-file filtering,
  - symlink skipping,
  - object-directory escape rejection,
  - 64 KiB chunked SHA-256 hashing,
  - POSIX relative source paths and archive paths,
  - sorting by `(category, object_id, relative_path)`.
- Wired `collect()` to call `collect_report_artifacts()` through `asyncio.to_thread`.

### Tests

Added focused discovery/safety tests in `backend/tests/services/test_export_service.py` for:

- regular-file discovery across all four report roots,
- missing roots,
- orphan object IDs,
- symlink targets,
- escaped paths that resolve outside the object directory.

## Verification

Ran:

```bash
./backend/run-worktree-tests.sh tests/services/test_export_service.py -q
```

Result: `33 passed`

## Concerns

None at the moment. The worktree stack was started for verification and should be torn down after the task is finished.
