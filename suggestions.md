# BioFlow Codebase Improvement Suggestions

Analysis date: 2026-08-15

Based on a thorough review of the codebase, here are recommendations organized by category. Each suggestion is tagged with estimated effort and impact.

---

## 🚨 Edge Cases & Error Handling

### 1. `complete.py` — Broad exception catching swallows critical signals

**File:** `backend/app/services/ai/complete.py`

Both `complete()` and `complete_sync()` use `except Exception` (aliased via `# noqa: BLE001`) to enforce the "never raise into a job" invariant. However, this also catches `KeyboardInterrupt` and `SystemExit`, which should always propagate.

**Fix:** Re-raise critical exceptions explicitly:

```python
except Exception as e:
    if isinstance(e, (KeyboardInterrupt, SystemExit)):
        raise
    log.warning("ai_call_crashed", provider=provider.name, error=str(e))
    result = Failure(FailureReason.BAD_RESPONSE, str(e)[:500])
```

**Effort:** Low | **Impact:** Medium — prevents silent swallow of shutdown signals

---

### 2. `objects.py` — `_SyncStreamBridge` lacks async iterator error handling

**File:** `backend/app/api/v1/objects.py`

If the async iterator raises an exception (network error, file read error), `future.result()` re-raises it in `__iter__`, but the bridge has no way to cleanly propagate a `StopAsyncIteration` through sync iteration. The sync iterator crashes mid-stream without signaling end-of-data.

**Fix:** Add try/except around `__anext__` in `_next()`:

```python
async def _next(self):
    try:
        return await self._it.__anext__()
    except StopAsyncIteration:
        return None
    except Exception:
        # Log and re-raise so the sync side sees it
        raise
```

**Effort:** Low | **Impact:** Medium — prevents silent data loss during streaming uploads

---

### 3. `upload_service.py` — Optional SHA256 verification on chunks

**File:** `backend/app/services/upload_service.py`

`write_chunk` accepts an optional `expected_sha256` parameter. If the client doesn't send `X-Chunk-SHA256`, chunks are written without integrity checking. A corrupted chunk during upload goes undetected until final assembly.

**Fix:** Either make SHA256 verification mandatory for all chunks, or log a warning when it's absent. Consider rejecting chunks without hash on the server side.

**Effort:** Low | **Impact:** Medium — ensures upload integrity

---

### 4. `compress.py` — Silent fallback from bgzip to stdlib gzip masks failures

**File:** `backend/app/storage/compress.py`

If `bgzip` fails (not found, wrong version, corrupt input), `_compress_with_bgzip` raises an exception caught by `compress_and_hash`, which silently falls back to stdlib gzip. A broken bgzip installation is invisible — the file compresses, but without BGZF blocks that tools like tabix need.

**Fix:** Log a warning on fallback so the user knows bgzip is broken:

```python
log.warning("bgzip_failed_falling_back_to_gzip", error=str(e))
```

**Effort:** Low | **Impact:** Medium — prevents silently producing non-BGZF files

---

### 5. `storage/paths.py` — TOCTOU race in `resolve_registerable`

**File:** `backend/app/storage/paths.py`

Between `candidate.resolve()` and the actual file operation, a symlink could be changed to point outside allowed roots. Low risk for a single-user local tool, but worth acknowledging.

**Fix:** Add a comment documenting the limitation, or add a post-resolution check:

```python
real = candidate.resolve()
# TOCTOU note: a symlink swapped between resolve() and use could escape roots
```

**Effort:** Trivial | **Impact:** Low — documentation improvement

---

### 6. `queue/queue.py` — `enqueue` has complexity 16 and spans 128 lines

**File:** `backend/app/queue/queue.py`

The function handles dedup, dependencies, resource overrides, delays, workflow advancement, and Redis push. Several paths have overlapping error conditions (e.g., a job with both `depends_on` and `dedup_key`).

**Fix:** Extract into smaller functions:
- `_validate_enqueue_params()` — validate input consistency
- `_handle_dedup()` — check for existing identical jobs
- `_handle_dependencies()` — resolve dependency chains

**Effort:** Medium | **Impact:** Medium — improves testability and readability

---

### 7. `worker.py` — `compute_free_resources` can return negative values

**File:** `backend/app/queue/worker.py`

If reserved resources exceed total (e.g., stale tracking), the function returns negative `available_cpu` or `available_mem`, which could cause the worker to oversubscribe.

**Fix:** Clamp to zero:

```python
return {
    "available_cpu": max(0, cpu_budget - reserved_cpu),
    "available_mem_mb": max(0, mem_mb - reserved_mem),
    ...
}
```

**Effort:** Trivial | **Impact:** Medium — prevents oversubscription and confusing UI states

---

### 8. `executor.py` — Complex inline `pump` closure in `_run_streaming`

**File:** `backend/app/queue/executor.py`

The inline `pump()` function handles 5 different I/O streams (stdout, stderr, cancel checks, progress parsing, heartbeat) with complexity 30. Hard to test and maintain.

**Fix:** Extract into a small class or separate functions, each with a single responsibility.

**Effort:** Medium | **Impact:** Medium — improves testability of subprocess I/O

---

## 🔧 Code Streamlining

### 9. `operations.py` — `merge_fastq` loads ALL project objects to verify a few

**File:** `backend/app/api/v1/operations.py`

```python
objects = await object_service.list_objects(project_id, owner=owner)
obj_map = {str(o.id): o for o in objects}
```

For a project with thousands of files, this loads everything just to check a handful of IDs.

**Fix:** Use targeted queries — either fetch by IDs directly with `find({"_id": {"$in": ids}})`, or use `object_service.get_object()` per ID with error handling.

**Effort:** Low | **Impact:** Medium — prevents unnecessary I/O on large projects

---

### 10. `suggestion_service.py` — `suggestions_for` is 228 lines with complexity 51

**File:** `backend/app/services/suggestion_service.py`

The single biggest simplification opportunity. It's a massive if-else chain checking object properties. The AGENTS.md itself warns about testing gaps here.

**Fix:** Use a registry pattern:

```python
card_builders = [
    (lambda obj: obj.format.kind == "fastq", build_preprocess_card),
    (lambda obj: ..., build_align_card),
    # ...
]

for check, builder in card_builders:
    if check(obj):
        card = builder(obj, ...)
        if card:
            results.append(card.as_dict())
```

Break each card builder into its own module/file and add unit tests for each in isolation.

**Effort:** High | **Impact:** High — makes the most complex function in the backend testable

---

### 11. `operations.py` — Module-level helpers used once

**File:** `backend/app/api/v1/operations.py`

`_read_file_chunks` and `_count_by` are module-level functions used by a single endpoint. They add to the module's public surface unnecessarily.

**Fix:** Make them local functions inside the endpoint, or inline them.

**Effort:** Trivial | **Impact:** Low — reduces namespace pollution

---

### 12. `client.ts` — 1444-line API client with ~70 repetitive methods

**File:** `frontend/src/api/client.ts`

Each method is the same pattern: `request<T>(path, { method, body })`. ~500 lines of repetitive wrappers.

**Fix:** Use a generic builder pattern:

```typescript
const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
```

Then callers write `api.post<Project>("/projects", { name: "foo" })`.

**Effort:** Medium | **Impact:** Medium — eliminates hundreds of lines of boilerplate

---

### 13. `types.ts` — 2541 lines in a single file

**File:** `frontend/src/api/types.ts`

The largest file in the frontend. Every type imported from a single barrel.

**Fix:** Split by domain: `types/project.ts`, `types/object.ts`, `types/run.ts`, `types/alignment.ts`, `types/variant.ts`, `types/ai.ts`, etc. Each module re-exports from a barrel `index.ts`.

**Effort:** Medium | **Impact:** Medium — improves discoverability and module boundaries

---

### 14. `App.tsx` — Route definitions inline in the shell component

**File:** `frontend/src/App.tsx`

The `Shell()` function contains inline route definitions and component mappings that add ~200 lines to the app shell.

**Fix:** Extract route definitions to a separate `routes.tsx` file, keeping `App.tsx` focused on layout.

**Effort:** Low | **Impact:** Low — improves separation of concerns

---

### 15. `nodes.py` — `_provision_node` is 170 lines with complexity 35

**File:** `backend/app/api/v1/nodes.py`

Handles SSH key generation, remote connection, command execution, verification, and error handling across multiple failure modes.

**Fix:** Extract:
- `_setup_ssh_connection()` — connect and authenticate
- `_execute_remote_commands()` — run commands with retry logic
- `_verify_node_operational()` — post-provision health check

**Effort:** Medium | **Impact:** Medium — makes node provisioning testable

---

## 🔒 Security Improvements

### 16. `complete.py` — API keys can leak into logs via error messages

**File:** `backend/app/services/ai/complete.py`

When a provider call crashes, the error message is logged with `str(e)[:500]`. If the error response from the provider API contains the API key (e.g., in a URL or request dump), it gets logged. The `redaction.py` module exists but is only applied to stored error bodies, not to logs.

**Fix:** Apply `redaction.scrub()` to error strings before logging:

```python
log.warning("ai_call_crashed", provider=provider.name,
            error=redaction.scrub(str(e)[:500], provider.api_key))
```

Also apply to `complete_sync()`.

**Effort:** Low | **Impact:** High — prevents credential leakage into logs

---

### 17. `node_ssh.py` — Host key verification disabled

**File:** `backend/app/services/node_ssh.py`

`known_hosts=None` disables SSH host key verification entirely during node provisioning. This allows MITM attacks on the SSH connection.

**Fix:** At minimum, log a prominent warning that host keys aren't verified. Better: implement TOFU (trust on first use) by storing the host key after first connection. Best: let users pre-configure known host keys via settings.

```python
# Current (vulnerable):
conn = await asyncssh.connect(host, port=port, username=username, known_hosts=None, ...)

# Better:
conn = await asyncssh.connect(host, port=port, username=username,
                               known_hosts=settings.ssh_known_hosts_path, ...)
```

**Effort:** Medium | **Impact:** High — prevents MITM during node provisioning

---

### 18. `compress.py` — Subprocess call without shell escaping

**File:** `backend/app/storage/compress.py`

`_compress_with_bgzip` uses `subprocess.run` with the command as a string. If `bgzip_path` contains spaces or special characters, this could lead to command injection.

**Fix:** Pass the command as a list:

```python
result = subprocess.run(
    [bgzip_path, "-c", str(source)],
    capture_output=True, input=raw_data,
    check=False, timeout=COMPRESS_TIMEOUT
)
```

**Effort:** Low | **Impact:** Medium — prevents command injection via tool paths

---

### 19. `upload_service.py` — Symlink attack in `_write_chunk_atomic`

**File:** `backend/app/services/upload_service.py`

```python
fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
```

If an attacker can create a symlink in the staging directory with the same name as a chunk, this follows the symlink and writes to an arbitrary file.

**Fix:** Add `O_NOFOLLOW` flag:

```python
fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
```

**Effort:** Trivial | **Impact:** Medium — prevents arbitrary file write via symlink

---

### 20. `params_sanitizer.py` — Simplistic path marker detection

**File:** `backend/app/services/params_sanitizer.py`

`PATH_MARKERS = ("/", "\\", "~")` is a substring check. A value like `etcpasswd` (no leading slash) or `subdir` passes through. Also, the allowlist approach is good but undocumented as a security boundary.

**Fix:** Use `os.path.isabs()` or check for path separator characters more thoroughly. Document the allowlist as a security boundary in the module docstring.

**Effort:** Low | **Impact:** Low — defense-in-depth improvement

---

### 21. `paths.py` — `resolve_report_file` doesn't handle Windows path separators

**File:** `backend/app/storage/paths.py`

`PurePosixPath(report_path).parts` treats backslashes as literal characters, not path separators. On Windows, `..\..\etc\passwd` would not be caught by the `".."` check.

**Fix:** Use `PureWindowsPath` for cross-platform safety, or strip/convert backslashes first.

**Effort:** Low | **Impact:** Low — only matters if the app runs on Windows

---

### 22. No rate limiting on API endpoints

The API has no rate limiting, making it vulnerable to accidental or intentional DoS. For a local tool this is less critical, but adding a simple per-IP rate limiter (token-bucket per second) would be cheap insurance.

**Fix:** Add `slowapi` or a custom middleware with a simple in-memory token bucket. Start with conservative limits on mutation endpoints.

**Effort:** Low | **Impact:** Low-Medium — defense-in-depth for local deployments

---

### 23. Profile authentication is explicitly not authentication

**File:** `backend/app/api/deps.py`

The documentation says "any client can send any profile's id and get that profile's data." This is a known design trade-off, but if the app is ever exposed beyond localhost, this becomes a real vulnerability.

**Fix:** Document this limitation in `SECURITY.md` or a security guide. If the app is ever deployed beyond localhost, add proper authentication.

**Effort:** Low (documentation) | **Impact:** Medium — awareness of a known limitation

---

## 🧪 Testing & Maintainability

### 24. `suggestion_service.py` has no dedicated tests

**File:** `backend/app/services/suggestion_service.py`

The 2200-line file with the most complex function in the backend has zero tests in `backend/tests/services/test_suggestion_service.py`. The AGENTS.md itself warns about this ("Check a rule against the real database, not only its unit tests").

**Fix:** Add unit tests for each card builder in isolation, using mock objects with controlled inputs. Test the "unavailable" paths specifically — the AGENTS.md warns these are often wrong.

**Effort:** High | **Impact:** High — catches regressions in the most complex code

---

### 25. `executor.py` — Subprocess I/O handling is untested

**File:** `backend/app/queue/executor.py`

The streaming logic, cancellation handling, and progress parsing in `_run_streaming` have no dedicated tests. This is one of the most error-prone parts of the system.

**Fix:** Add unit tests with mock subprocesses that simulate stdout/stderr output, cancellation requests, and timeout scenarios.

**Effort:** Medium | **Impact:** Medium — catches subprocess handling bugs

---

### 26. `types.ts` — No runtime validation of API responses

**File:** `frontend/src/api/types.ts`

The frontend trusts API response shapes. If the backend changes a field type or removes a required field, the frontend silently breaks with cryptic errors.

**Fix:** Consider using Zod schemas for runtime validation of critical API responses, or at minimum add TypeScript strict mode checks with branded types.

**Effort:** Medium | **Impact:** Medium — catches API contract violations early

---

### 27. Missing type hints on `_SyncStreamBridge`

**File:** `backend/app/api/v1/objects.py`

```python
def __iter__(self):
    import asyncio
```

Missing return type annotation. Should be `-> Iterator[bytes]`.

**Fix:** Add return type annotation.

**Effort:** Trivial | **Impact:** Low — type safety

---

## 📦 Other Observations

### 28. `docker-compose.override.yml` uses relative bind-mount paths

The compose override uses `./backend/app` and `./frontend/src` relative paths, which resolve against the Compose invocation directory. The AGENTS.md documents this extensively as a source of confusion.

**Fix:** Consider making the override paths absolute or using env vars. At minimum, keep the existing documentation and the block-compose-in-worktree hook.

**Effort:** Low | **Impact:** Low — prevents developer confusion

---

### 29. `tool_cache.py` — Redis pub/sub invalidation can lose messages

**File:** `backend/app/pipelines/tool_cache.py`

If the Redis connection drops between `publish_invalidation` and `listen_for_invalidations`, the cache stays stale until the next process restart.

**Fix:** Add a periodic full refresh as a fallback (e.g., re-warm the cache every 5 minutes regardless of invalidation events).

**Effort:** Low | **Impact:** Low — prevents stale tool cache

---

### 30. `timing_service.py` — Silent estimate drift from failed runs

**File:** `backend/app/services/timing_service.py`

The AGENTS.md explicitly warns: "A few OOM-killed runs in a fit drag estimates downward." The `_modelled()` accessor filters failures correctly, but there's no alerting when estimates drift.

**Fix:** Add a health check or metric that compares estimated vs actual resource usage and logs a warning when estimates are consistently wrong by >20%.

**Effort:** Medium | **Impact:** Low-Medium — prevents silent prediction degradation

---

## Quick Wins (Low Effort, High Impact)

| # | What | File(s) | Why |
|---|---|---|---|
| 1 | Add `O_NOFOLLOW` to chunk write | `upload_service.py` | Prevents symlink attack on staging dir |
| 2 | Apply `redaction.scrub()` to error logs | `complete.py` | Prevents API key leakage into logs |
| 3 | Clamp resource values to zero | `worker.py` | Prevents negative available resources |
| 4 | Use list-based subprocess calls | `compress.py` | Prevents command injection via tool paths |
| 5 | Extract route definitions from `App.tsx` | `App.tsx` | Reduces file from 312 to ~100 lines |
| 6 | Add `isinstance` checks for critical exceptions | `complete.py` | Ensures `KeyboardInterrupt`/`SystemExit` propagate |
| 7 | Log warning on bgzip fallback | `compress.py` | Prevents silently producing non-BGZF files |
| 8 | Make SHA256 verification mandatory on chunks | `upload_service.py` | Ensures upload integrity |
| 9 | Add return type to `_SyncStreamBridge.__iter__` | `objects.py` | Type safety |
| 10 | Document profile auth limitation in SECURITY.md | `deps.py` | Awareness of known limitation |

---

## Summary

The codebase is well-structured with excellent inline documentation and thoughtful design decisions. The main areas to address are:

1. **Complexity hotspots** — `suggestion_service.py` (complexity 51), `enqueue()` (complexity 16), `_provision_node()` (complexity 35), and `_run_streaming`'s `pump()` (complexity 30) are the hardest to test and most likely to hide bugs.

2. **Concrete security gaps** — SSH host key verification disabled, API keys potentially logged, symlink attacks possible on chunk uploads, and subprocess calls without shell escaping.

3. **Edge cases** — Broad exception handling swallowing critical signals, async-sync bridge error propagation, and silent fallback from bgzip to gzip.

4. **Frontend simplification** — The API client and types file are overly repetitive and could be significantly streamlined with patterns and domain splitting.

The quick wins are all low-risk and address real (if unlikely) failure modes.
