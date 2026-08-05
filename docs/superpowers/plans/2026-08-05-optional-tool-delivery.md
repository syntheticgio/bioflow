# Optional Tool Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A tool can be absent from the backend image and installed later from a Settings screen, without a terminal. DeepVariant stops claiming to be available when its image was never pulled; a user who launches variant calling against an uninstalled caller is told the download size and, on confirm, gets both jobs queued with the real one waiting on the pull.

**Architecture:** `ToolMeta` gains `delivery`/`image`/`download_bytes`, making it the single manifest of what is optional — served by the existing `GET /pipelines/tools`, which is also what unblocks the launcher (#40) without hardcoding a list there. Optional tools are pinned OCI images run as sibling containers, generalizing the existing DeepVariant path. Install and uninstall are queue jobs (`install_tool` / `uninstall_tool`) so they get progress, cancellation, logs and retry for free, and so a launch can `depends_on` an install. Probe results for image-delivered tools are invalidated across processes over Redis pub/sub, because `api` and two `worker` replicas each hold their own `lru_cache`.

**Tech Stack:** FastAPI + Beanie + Pydantic v2; the existing Redis-backed job queue; React + TanStack Query frontend. Tests are pytest (`asyncio_mode = "auto"`).

**Design spec:** `docs/superpowers/specs/2026-08-05-optional-tool-delivery-design.md`

**Issues:** [#26](https://github.com/syntheticgio/bioflow/issues/26) (this plan), epic [#5](https://github.com/syntheticgio/bioflow/issues/5), unblocks [#40](https://github.com/syntheticgio/bioflow/issues/40).

---

## Background for the engineer

**Read `app/pipelines/tools.py` and `docs/superpowers/specs/2026-07-31-deepvariant-sidecar-design.md` first.** DeepVariant is the working precedent for everything here; this plan generalizes it rather than inventing a mechanism.

### Three findings from the existing code that this plan depends on

**1. The DeepVariant probe reports availability it never checked.** `tools.deepvariant()` (`tools.py:382`) returns `available=True` whenever the Docker daemon answers — it never asks whether the image was pulled. So `suggestion_service` offers the card, `tools.require()` passes in `launch_variant_calling`, the job is accepted, and it dies later at `_require_image` (`queue/variant_handlers.py:333`) telling the user to run `docker pull` themselves. Task 2 fixes this, and every user-visible piece of the plan depends on the probe telling the truth first.

**2. The Docker socket is mounted only in `docker-compose.override.yml`, never in the base file.** Grep it: the two `- /var/run/docker.sock:/var/run/docker.sock` lines are both in the override. The launcher ships `docker-compose.yml` alone (`launcher/src-tauri/src/setup/install.rs:52`, `BUNDLED_COMPOSE_RESOURCE` in `commands.rs:30`) and never the override. **So DeepVariant does not work for a launcher-installed user today, and neither would any optional tool.** This is a prerequisite, not a detail — it is Task 0, and without it the entire epic ships a feature that only works in the developer's own checkout.

**3. `Job.depends_on` already exists and works.** `models/job.py:145` with a `by_depends_on` index, `JobState.BLOCKED`, and release logic in `queue/queue.py` (`_unfinished_dependencies`, `_failed_dependencies`, and the fan-out at line 278 when a job finishes). `queue.enqueue()` takes `depends_on: list[PydanticObjectId]`. Nothing new is needed for the chained-launch flow — Task 7 just calls it. `_require_image`'s own docstring anticipates this: *"When `pull_image` exists as its own job this becomes a dependency instead of a message."*

### The trap that will cost a debugging session

Probe results live in per-process `@lru_cache`. **`api` and two `worker` replicas are three separate processes.** An install performed by a worker does not clear the API's cache, so the pull completes, the API keeps serving the pre-install probe, and the screen still reads "not installed." That looks exactly like a broken button, and it will not reproduce in a single-process test. Task 3 is the invalidation channel; do not defer it behind the UI work.

Note also that `tool_cache.NOT_FINGERPRINTABLE` already excludes `deepvariant` from Redis persistence, and the reason generalizes: an image-delivered tool's `Tool.path` is the *docker client's* path, so fingerprint-keyed caching would key availability to the wrong binary's identity. Every `ON_DEMAND_IMAGE` tool joins that set; do not try to make them fingerprintable.

### Rules from CLAUDE.md that bite here specifically

**Registering a tool is only half the change.** `suggestion_service.py` is a hand-maintained mapping. A tool no rule can pick will never be suggested however cleanly it installs. Task 6 is not optional polish.

**`TOOL_META` is rendered by `/help/software` and guarded by `test_every_tool_is_documented`.** Task 1 extends both.

**Test the direction that fails.** The image ships most tools installed, so asserting a card is *available* passes whether or not the patch worked. Assert the card flips to `NEEDS_INSTALL`/unavailable when the probe is patched off.

**Run tests from this worktree with `./backend/run-worktree-tests.sh tests/ -q`**, never `docker compose exec api pytest` — that silently tests main's code. Exercise the UI with `./ops/worktree-up.sh` (5273), never bare `docker compose`.

**`worker` does not hot-reload.** `docker compose restart worker` — or the worktree stack's equivalent — after any change to a handler or anything it imports, before re-testing a job.

---

## File Structure

**Backend — create:**
- `app/queue/tool_handlers.py` — `install_tool` and `uninstall_tool` handlers
- `app/services/tool_install_service.py` — install/uninstall orchestration, in-flight dedup, the "is anything running that uses this tool" guard
- `tests/queue/test_tool_handlers.py`
- `tests/services/test_tool_install_service.py`
- `tests/api/test_tool_install_api.py`

**Backend — modify:**
- `app/pipelines/tools.py` — `Delivery` enum, three `ToolMeta` fields, image-presence probe, `InstallState`
- `app/pipelines/tool_cache.py` — generalize `NOT_FINGERPRINTABLE`, add the invalidation subscriber
- `app/api/v1/pipelines.py` — install/uninstall endpoints
- `app/services/suggestion_service.py` — `CardStatus.NEEDS_INSTALL`
- `app/services/pipeline_service.py` — `launch_variant_calling` chains an install
- `app/queue/variant_handlers.py` — `_require_image` becomes a guard, not a message
- `app/queue/handlers.py` — import `tool_handlers` for registration side effects
- `docker-compose.yml` — the socket mount (Task 0)
- `tests/pipelines/test_tools.py`, `tests/services/test_suggestion_service.py`

**Frontend — create:**
- `src/components/SettingsTools.tsx` — the tool list with state and buttons
- `src/components/SettingsNav.tsx` — the section rail Settings does not have yet

**Frontend — modify:**
- `src/App.tsx` — `/settings/tools` route
- `src/components/SettingsView.tsx` — wrap in the new nav
- `src/api/client.ts`, `src/api/types.ts` — install/uninstall calls, tool state types
- the variant launch dialog — the download-size confirmation
- `src/components/ActionsTab` (wherever cards render) — the `needs_install` card

---

## Task 0 — Move the Docker socket into the base compose file

Without this, optional tools work only in a source checkout, and DeepVariant is already broken for launcher users.

- [x] Move `- /var/run/docker.sock:/var/run/docker.sock` from `docker-compose.override.yml` (both `api` and `worker`) into `docker-compose.yml`.
- [x] Move `BIOINFO_HOME_HOST` into the base file's `x-backend-env` anchor for the same reason — a sibling container's mounts are resolved from the host, and the override is where that value currently lives.
- [x] Carry the existing comments across rather than dropping them: the override's note that this is *"a real privilege increase — a container that can reach the daemon can start any container — accepted because this app is single-user and local"* is the justification, and it now applies to every install, not just DeepVariant. Say so.
- [x] Note in `launcher/README.md` that the shipped stack grants the daemon socket and why.
- [x] Verify: `./ops/worktree-up.sh`, then confirm the socket resolves using **only** `docker-compose.yml` plus the worktree port file — i.e. prove the socket no longer depends on the override.

  Done via `docker compose -p biopipe-base-only -f docker-compose.yml config`
  (base file alone, no override) — the resolved config still carries the
  socket mount on both `api` and `worker`. Also verified against the live
  worktree stack: `docker exec <worktree>-api-1 docker version --format
  '{{.Server.Version}}'` returned `27.5.1`, i.e. the daemon is reachable
  through the mount at runtime, not just present in the resolved YAML.

- [x] Ran the full backend suite against this change: 2728 passed, 0 failed
  (`./backend/run-worktree-tests.sh` resolved a stale fallback image missing
  an unrelated dependency, `cryptography`, added by an earlier feature; ran
  manually against the worktree's actual built image instead — the compose
  change itself introduced no failures).

**Watch for:** Windows and macOS paths. Docker Desktop presents the socket at `/var/run/docker.sock` inside the VM on both, so the literal path is right, but this was only verified on Linux here — confirm on the launcher's other target platforms before calling this fully done.

---

## Task 1 — `ToolMeta` carries the delivery manifest

- [x] Add to `tools.py`:
  ```python
  class Delivery(StrEnum):
      BUNDLED = "bundled"            # in the image; probe by PATH
      ON_DEMAND_IMAGE = "on_demand"  # pulled; probe by image presence
  ```
- [x] Add three fields to `ToolMeta`, all defaulted so existing entries stay constructible: `delivery: Delivery = Delivery.BUNDLED`, `image: str | None = None`, `download_bytes: int | None = None`.
- [x] Set `delivery=Delivery.ON_DEMAND_IMAGE` on the `deepvariant` entry, with `image` and `download_bytes`. Used the measured figure already on record in the sidecar spec: **2.99 GB compressed pull**, not the 8.83 GB on-disk figure also quoted there — `download_bytes` is documented as transfer size, and that's the number worth showing before a download starts.
- [x] `image` resolves per architecture by reading `settings.deepvariant_image` directly at `TOOL_META` construction time (which already runs `default_deepvariant_image()`'s x86-64/arm64 dispatch) rather than adding a second one.
- [x] Extended `test_every_tool_is_documented`'s file with two new tests rather than folding into it: `test_on_demand_tools_declare_image_and_size` (the required check) and its inverse, `test_bundled_tools_have_no_image_or_size`, which catches a tool whose `delivery` reverted to `BUNDLED` but left a stale `image`/`download_bytes` behind — a failure mode the plan hadn't named. Kept separate from the original four-field check so a `BUNDLED` tool's failure message never claims it needs an image it will never have.

**Correction to this task's own note:** `Delivery` does **not** serialize as its string value for free through `asdict(meta)` — `asdict` recurses into dataclass fields but leaves plain enum members as enum instances, which is exactly the `pipelines` problem the note below already flagged for tuples of `PipelineType`. `tool_with_meta` needed an explicit `"delivery": meta_dict["delivery"].value` line, the same treatment `pipelines` gets. Caught by `test_delivery_reaches_the_api_payload_as_its_string_value`, which asserts `isinstance(payload["delivery"], str)` rather than only checking the value compares equal (a raw `Delivery.ON_DEMAND_IMAGE` would still `== "on_demand"` and pass a weaker assertion).

Also updated `tool_with_meta`'s fallback dict (for a tool with no `TOOL_META` entry at all) to include the three new keys defaulting to `BUNDLED`/`None`/`None`, and added `frontend/src/api/types.ts`'s `PipelineTool` interface with the same three fields — not in this task's original file list, but the endpoint's response shape changed and the type should say so. `tsc -b --noEmit` passes.

---

## Task 2 — An honest probe for image-delivered tools

- [x] Added `InstallState` to `tools.py`: `INSTALLED`, `NOT_INSTALLED` (a real, expected state — **not** an error), `UNKNOWN` (no docker client, or daemon unreachable — the only genuine failure).
- [x] Carried it on `Tool` as `install_state: InstallState | None = None`. `Tool.available` stays a boolean, but its logic changed: when `install_state is not None`, availability now requires `install_state is INSTALLED` as well as the existing path/error checks. Every BUNDLED tool leaves `install_state` at `None`, so their `available` computation is byte-for-byte what it was before — `require()`, the launch dialog, and the aligner registry needed no changes.
- [x] Rewrote `tools.deepvariant()` down to a one-line call into a new shared helper, `_probe_on_demand_image(name, image)`, which runs `docker image inspect <image>` after confirming the daemon is reachable, and sets `install_state` accordingly. Reports the image tag as `version` only when `INSTALLED`; a not-installed tool has no image to read a tag from, so `version` stays `None` rather than guessing at the configured name.
- [x] The not-installed `error` reads "deepvariant is not installed. It runs as a separate container image (...) rather than being bundled here, and is downloaded on first use." — an offer, not a fault. Guarded by a test asserting `"not found"` does **not** appear, since that was the old missing-binary wording this must not echo.
- [x] Generalized as planned: `_probe_on_demand_image` takes `(name, image)`, so Task 8's Clair3 move becomes `_probe_on_demand_image("clair3", <image>)` rather than a second copy of the daemon/inspect logic.

**Tests:** `test_unavailable_when_the_image_was_never_pulled` patches `docker image inspect` to return non-zero and asserts `available is False`, `install_state is NOT_INSTALLED`, and the offer-not-fault wording. `test_unavailable_when_the_daemon_is_unreachable` (kept from before this task, assertions extended) covers `UNKNOWN`. `test_available_and_versioned_when_the_image_is_present` is the positive case, since the daemon and inspect calls needed a fake dispatching on argv (`_fake_run`) once there were two `subprocess.run` calls to distinguish rather than one.

**Also verified live, not only in mocks** — this is the class of bug that looks fixed against a mock and isn't: with the worktree stack up and DeepVariant's image genuinely never pulled, `docker exec <api> python -c "from app.pipelines import tools; print(tools.deepvariant())"` and a `curl` against the running `/api/v1/pipelines/tools` endpoint both show `available: false`, `install_state: "not_installed"`, paired with Task 1's `delivery: "on_demand"` / `image` / `download_bytes` in the same payload. Before this task, the same live check reported `available: true`.

Extended `frontend/src/api/types.ts`'s `PipelineTool` with `install_state`; `tsc -b --noEmit` passes. Full backend suite: 2738 passed, 0 failed.

---

## Task 3 — Cross-process cache invalidation

- [x] Generalized `tool_cache.NOT_FINGERPRINTABLE` from `{"deepvariant"}` to a set comprehension over `TOOL_META` selecting `delivery is Delivery.ON_DEMAND_IMAGE`. The DeepVariant-specific reasoning stays in the comment as the worked example; a new sentence states the general rule and the reason it is derived rather than hand-listed: a tool moving to `ON_DEMAND_IMAGE` later (Clair3, task 8) is excluded the moment its `ToolMeta.delivery` changes, with nothing left to forget to edit here.
- [x] Added `INVALIDATE_CHANNEL = "bp:tools:invalidate"` to `tool_cache.py`, alongside the existing `CACHE_KEY` rather than in `queue/keys.py` — this is not queue infrastructure, and `CACHE_KEY` already lives locally in this module for the same reason.
- [x] Added `publish_invalidation(client, tool_name)`. Not yet called from anywhere — task 4 (install/uninstall jobs) doesn't exist yet, so there is no real caller until then. Verified by publishing manually from a separate throwaway process against the live stack (below), which is what the "install or uninstall" call site will do once it exists.
- [x] Added `listen_for_invalidations(client)`: a `while True` subscriber loop, retried on any Redis error rather than exiting (a subscriber that gives up on the first hiccup silently stops watching for the rest of the process's life — worse than the 5s backoff before retrying). Wired into **both** processes: `app/main.py`'s `lifespan` starts it the same fire-and-forget way `_warm_tools` already is (held in a local, cancelled in `finally`), and `Worker._tool_invalidation_loop` joins the existing named-task list in `Worker.start`, so `_drain`'s existing cancel-and-await loop tears it down with no separate shutdown path to write.
- [x] Went with `tools.reset_cache()` (clears all twenty-six probes) rather than a per-tool clear, per this task's own steer: there is no name → probe-function registry to look up a single tool's `.cache_clear()` by string, and building one to shave a rare, cheap operation (each probe is lazily re-run, one at a time, on next use) down to one tool was judged not worth a second place a probe function's name could drift from its `TOOL_META` key.
- [x] Both `publish_invalidation` and `listen_for_invalidations` catch broadly and log a warning, never raise or propagate `CancelledError` as anything but itself — matching every other function in this file.

**Verified manually against the live stack, exactly as this task asked, not only in tests.** With the worktree stack up, published an invalidation from a *separate* throwaway `python -c` process (standing in for the install job task 4 will add) and confirmed via `docker logs` that all three running processes — `api`, `worker-1`, and `worker-2` — logged `tool_cache_invalidated` for the same message, simultaneously, with none of them restarted. Full backend suite: 2774 passed, 0 failed. Found and fixed one bug in my own test while verifying: patching `tool_cache.asyncio.sleep` patches the shared `asyncio` module object itself, so a naive replacement calling `asyncio.sleep(0)` from inside the patch recurses into itself — fixed by capturing the real `asyncio.sleep` before patching.

---

## Task 4 — Install and uninstall as jobs

- [x] `app/queue/tool_handlers.py` with `install_tool` (`HandlerMode.SUBPROCESS`, `JobClass.USER_INTERACTIVE`), payload `{"tool": name}`. Shells to `docker pull`, parsing its progress lines into `ctx.progress()` via a `_PullProgress` class (mirrors `variant_runner.VariantProgress`'s shape) that tracks `<layer-id>: <status>` lines and counts layers that reached `Pull complete`/`Already exists` against layers seen — confirmed against a real `docker pull nginx:latest` piped through a non-TTY stdout, not assumed: piped output collapses to one discrete line per layer-state-change with no byte counts, so a monotonic layer-fraction is what the output actually supports.
  - `USER_INTERACTIVE`, not `COMPUTE`, as planned.
  - `max_attempts=3` for install (transient pull failures), `max_attempts=2` for uninstall (`docker image rm` fails deterministically — image in use, image already gone — so retrying does not change the outcome).
- [x] `uninstall_tool` alongside it, shelling to `docker image rm`.
- [x] Imported `tool_handlers` from `app/queue/handlers.py`.
- [x] `app/services/tool_install_service.py` — all three rules enforced, plus the eligibility checks (`ValidationError` for a `BUNDLED` tool, `NotFoundError` for an unknown one) that gate both `install` and `uninstall` before a job is ever created.
- [x] `POST /pipelines/tools/{name}/install` and `DELETE /pipelines/tools/{name}/install`, both returning `JobOut`.
- [x] `_invalidate()` calls `tool_cache.publish_invalidation` at the end of both handlers on success only (not on failure — a failed pull changed nothing worth invalidating), reached from the SUBPROCESS thread via `db.client.run_from_thread`, the same seam `summary_handlers._resolve_sync` uses for the identical thread-has-no-event-loop problem. Corrected that seam's own docstring while using it: nothing about `run_from_thread` is Mongo-specific despite the name and its original Mongo-only docstring — it reaches whatever the process's one real event loop is, and `connect_to_redis()` runs on that same loop in both `app/main.py` and `worker_main.py`.

**A real bug found while writing tests, not by inspection.** `_active_install_query` (the fallback lookup used when `enqueue` reports a dedup collision) had no `owner` filter. `enqueue`'s stored dedup key *is* owner-scoped (`f"{owner}:{dedup_key}"`), so two different profiles installing the same tool correctly get two independent jobs -- but the unfiltered fallback lookup would then hand the *second* profile's caller back the *first* profile's job id, collapsing two independent requests into one shared job record. Caught by `test_a_second_install_returns_the_first_jobs_id` failing with two different owners' job ids compared equal, in a run where an *earlier* test's leftover job was the one actually matched. Fixed by adding `owner` to the query; the bug and the fix are documented in the query's own docstring so it isn't rediscovered as a mystery later. A second, unrelated test-isolation bug surfaced alongside it: `TestUninstallRefusesWhileRunning` inserted `Job` documents directly into the shared module-scoped test database and never retired them, so a RUNNING job with `payload.caller: "deepvariant"` outlived its own test and poisoned every later `uninstall()` call in the module — fixed by resetting each such job to `SUCCEEDED` in a `finally` block after use.

**Verified live against the running worktree stack, not only against tests or mocks.** `curl -X POST /pipelines/tools/deepvariant/install` created a real job; `docker logs` on the worker showed it claimed, `state: running`, and `tool_install_started` logged with the correct image name; polling `/jobs/{id}` a few seconds later showed `state: "running"`, `phase: "pulling"`, `message: "pulling (20/96 layers)"` — a genuine fraction computed from real `docker pull` output, the same shape Task 5's UI will render. Cancelled the job afterward (via the existing `/jobs/{id}/cancel`, which worked without any code written for this task — `run_subprocess`'s cancellation plumbing is transparent to any SUBPROCESS handler) rather than let a ~3 GB download run to completion in a throwaway stack; `state` moved to `cancelled` immediately.

Full backend suite: 2860 passed, 0 failed (2774 baseline + 86 net new, including the API/service/handler suites above). Frontend `tsc -b --noEmit` clean (no frontend changes this task, checked anyway since the stack was up).

---

## Task 5 — Settings › Tools

Settings is a single page today (`SettingsView.tsx` renders `Settings · AI`, `App.tsx` routes `/settings` and `/settings/ai` to the same component), so this introduces the section nav a second page implies.

- [x] `SettingsNav.tsx` — a section rail (AI / Tools) driven by `useLocation()` rather than component state, since the two pages have no other shared state and routing already *is* the thing that decides which is active. `/settings` still lands on the AI page unchanged.
- [x] Route `/settings/tools` → `SettingsTools.tsx`. `App.tsx`'s `singleColumn` prefix check needed no edit, confirmed.
- [x] `SettingsTools.tsx` renders one row per tool from `GET /pipelines/tools`, sorted alphabetically, **including bundled ones** — all five states implemented (Included / Installed / Not installed / Installing with live progress and Cancel / Failed with Retry).
- [x] Confirm before uninstall via the plain `confirm()` browser dialog, matching the existing pattern in `ProviderForm.tsx`/`ProjectExplorer.tsx` rather than introducing a modal component for this one page. Names the reclaimed size when `download_bytes` is known.
- [x] Polling, not a bare invalidation: `installJobs`/`uninstallJobs` queries use the same conditional-`refetchInterval` shape `JobList.tsx` already established for the Activity tab (poll only while something is actually in flight, `false` otherwise) — chosen over a fixed interval because most of the time there is nothing installing and polling would be pure waste, and chosen over relying only on SSE because this page has no per-tool event channel to subscribe to.
- [x] No second tool list: `SettingsTools.tsx` and `HelpSoftware.tsx` both read `api.pipelineTools()` fresh; `/help/software` untouched, no buttons added there.

**Two API client methods added** (`installTool`/`uninstallTool` in `frontend/src/api/client.ts`) that were not in this task's original file list but are the obvious client-side pair to the task-4 endpoints — `cancelJob`/`retryJob` already existed and were reused as-is for the Installing/Failed states rather than duplicated.

**Verified in the browser via `./ops/worktree-up.sh`, the full round trip, not just a static read.** Loaded `/settings/tools`: every bundled tool showed "Included — `<version>`" with no button, and DeepVariant showed "Not installed — 2.8 GB" (2.99 GB decimal, correctly rendered in `formatBytes`' binary GiB) with an Install button. Clicked Install: the row immediately became "pulling (0/96 layers)" with a Cancel button and the footer's running-job count incremented, with **zero code written for that job-count update** -- it is the existing global job-count indicator picking up the same job. Waited and re-read the page: progress advanced live to "pulling (20/96 layers)" with no manual refresh, confirming the poll loop. Clicked Cancel: the job stopped and the row reverted cleanly to "Not installed — 2.8 GB / Install". Navigated back to `/settings/ai` and confirmed the section nav and the existing AI page still render correctly with the new wrapper. Zero console errors across the entire sequence.

`tsc -b --noEmit`: clean. Backend suite (unaffected by this frontend-only task, checked anyway since the stack was rebuilt): 2921 passed, 0 failed.

---

## Task 6 — `NEEDS_INSTALL` in the Actions tab

- [x] Added `NEEDS_INSTALL = "needs_install"` to `CardStatus`.
- [x] Added `requires_install: dict | None` to `SuggestionCard`, included in `as_dict()`.
- [x] The docstring's `launch`/`status` agreement rule is updated to state `NEEDS_INSTALL` as the deliberate exception: it keeps `launch` exactly like `AVAILABLE` does.
- [x] Updated the variant card builder's DeepVariant-fallback branch: `dv_tool.install_state is NOT_INSTALLED` now returns a `NEEDS_INSTALL` card with a real `caller=deepvariant` launch payload and `requires_install`, rather than falling into the plain `UNAVAILABLE` refusal. `UNKNOWN` (daemon unreachable) deliberately still falls through to `UNAVAILABLE` — pressing Install would just fail again for the same reason, so it is a fault, not an offer, and a test (`test_an_unknown_deepvariant_state_does_not_offer_install`) pins that the two states diverge.
- [x] Frontend: `PipelineSuggestions.tsx` treats `needs_install` as runnable, the same as `available` — same enabled button, different label ("Install and launch") and an extra line showing the download size when `requires_install.download_bytes` is present. `PipelineSuggestion`'s TypeScript type gained the third status and the `requires_install` field to match.

**Correction to this task's own note:** the "patch `spec_for`, not the probe function" caveat is about `aligner_registry`'s frozen dataclass specs, which capture a tool *function object* at import time — it does not apply here. `installed_callers` (the existing test fixture) already patches `app.services.suggestion_service.tools.deepvariant` as a plain module-attribute lookup, which the file's own `test_the_caller_patch_actually_takes_effect` pins as reaching the call site. Extended it with a `deepvariant_install_state` parameter (`_FakeTool` gained an `install_state` attribute) rather than inventing a second fixture.

**A deliberate scope boundary, confirmed with the user rather than assumed:** clicking a `NEEDS_INSTALL` card's button today posts the real launch payload straight to `/pipelines/variants`, which still bare-calls `tools.require(tools.deepvariant())` and will refuse a not-yet-installed tool with an error until task 7 builds the confirm-then-chain flow. Asked explicitly whether the button should (a) post directly and surface that error until task 7 lands, (b) redirect to Settings › Tools instead, or (c) render disabled in the meantime — chose (a): it matches "`NEEDS_INSTALL` keeps its launch payload, same as `AVAILABLE`" literally, and option (c) is exactly the UNAVAILABLE-shaped treatment this task exists to avoid. Nothing here needs to be undone once task 7 lands; the same click just starts succeeding.

Full backend suite: 2932 passed, 0 failed (5 new tests in `TestVariantsCard`, plus the pre-existing `test_every_card_is_a_plain_dict_with_the_full_key_set` updated for the new field — exactly the kind of test that should catch an added field, and did). `tsc -b --noEmit` clean.

---

## Task 7 — Confirm-then-chain launch

- [x] Replaced the bare `tools.require(tools.deepvariant())` with a new helper, `_require_or_offer_install`, called only for the `DEEPVARIANT` caller branch (Clair3 and bcftools are still bundled today, so they keep the plain `require()` — this generalizes automatically once task 8 moves Clair3 to `ON_DEMAND_IMAGE`, since the same helper reads `install_state` off whatever `Tool` it is given).
- [x] Not installed, without consent: raises `ValidationError` with `details={"tool": ..., "needs": "install_tool", "download_bytes": ...}`, matching the `.bai`/`.fai` refusals' `needs` vocabulary exactly, and a message naming the size in GB.
- [x] With consent (`install_optional: bool`, threaded through `VariantRequest` → `launch_variant_calling`): enqueues via `tool_install_service.install` (task 4's dedup-aware install, so a second consenting request does not start a second pull) and chains `call_variants` behind it with `depends_on=[install_job_id]`.
- [x] Frontend, two call sites: `VariantDialog.tsx` (the manual launch dialog) catches the `needs: "install_tool"` refusal, shows a banner with the size, and re-posts with `install_optional: true` on a second click labeled "Install and call". `PipelineSuggestions.tsx`'s `NEEDS_INSTALL` card (task 6) needed no client-side change at all -- the flag is set server-side in `build_variants_card`'s launch body, since the card already states the size in `requires_install` before it can be clicked, so the click *is* the consent, and the frontend component stays "ignorant of the three launch request shapes" per its own existing comment.
- [x] `_require_image` rewritten as the guard the task describes: its docstring now explains that reaching it with the image still absent is a bug in the install-then-launch chain, not an expected first-run state, and the raised message no longer tells anyone to open a terminal.

**Verified live against the real chain, not only mocks.** With DeepVariant genuinely not installed in the running worktree stack: called `_require_or_offer_install` directly against the real probe and real Mongo -- without consent it raised with `message: "...about 3.0 GB."` and `details: {'tool': 'deepvariant', 'needs': 'install_tool', 'download_bytes': 2990000000}`; with consent it returned a real `install_tool` job id, confirmed via `Job.get()` to have `state: queued`. Separately confirmed the `depends_on` mechanism itself: enqueued a fake dependent job behind a real install job and confirmed it landed `BLOCKED` (`job_blocked` logged), which is `queue.py`'s own existing mechanism -- not rebuilt, only relied on, matching this task's own "watch for" note. `_failed_dependencies` was not separately re-verified beyond reading it; it is unmodified code covered by its own existing tests.

Full backend suite: 2937 passed, 0 failed, including 5 new tests directly exercising `_require_or_offer_install`'s four branches (available / not-installed-refused / not-installed-consented / unknown-still-refuses) and a fifth guarding the BUNDLED-tool-genuinely-missing case. Caught and fixed a bug in the test helper itself while writing these: an early version of `_fake_dv_tool` set `path` from `install_state` truthiness independent of the `available` flag it was named for, which made `available=False, install_state=None` silently construct a `Tool` that reported `available=True` -- exactly the kind of fake-object bug that would have made the BUNDLED-tool test pass for the wrong reason. `tsc -b --noEmit` clean.

---

## Task 8 — Move Clair3 out of the image — **researched, not done: blocked on a real arm64 gap**

**This is not optional garnish.** An abstraction designed against a single example usually fits exactly that example. Clair3 has different mounts, a different output layout (it writes a fixed `merge_output.vcf.gz` that `_rename_output` already renames), and baked-in models. It is what proves the seam generalizes — and if it does not, the cost of finding out is lowest here.

- [x] Picked a pinned Clair3 image and verified it hands-on rather than trusting the plan's own "bioconda/biocontainers publishes one; verify arm64 availability" line at face value. Result: **neither exists for arm64.**
  - `hkubal/clair3:v2.0.2` (the tool's own maintainers, HKU-BAL) — `docker manifest inspect` + `docker pull --platform linux/arm64` confirms **linux/amd64 only**, no manifest list. 1.50 GB compressed pull (Docker Hub API `full_size`), 4.05 GB on disk. Carries all model directories at `/opt/models/{ont,hifi,...}` — the `--watch for` risk is cleared for this image.
  - `quay.io/biocontainers/clair3:2.0.2--py311hbc58adc_0` — also **amd64 only** (`docker image inspect` reports `Architecture: amd64`), despite the Dockerfile's own comment believing bioconda is the arm64-capable distribution. That claim is true of the *conda package* (installable via micromamba, which is exactly how this image's own Dockerfile installs it today) but not of *any Docker image built from it* — biocontainers' CI does not multi-arch this recipe. 2.09 GB on disk, also carries models.
- [ ] ~~Remove the Clair3 layer from `backend/Dockerfile`~~ — not done. See below.
- [ ] ~~Add a Clair3 runner path~~ — not done, though the shape is now known: `run_clair3.sh`'s CLI is unchanged between the bioconda binary and `hkubal/clair3`'s entrypoint (confirmed via `docker run hkubal/clair3:v2.0.2 run_clair3.sh --help`, which lists `--bam_fn`/`--ref_fn`/`--threads`/`--platform`/`--model_path`/`--output`/`--include_all_ctgs` identically), so `build_clair3_command` would need only the same host-path/mount translation `build_deepvariant_command` already does — a `docker run` wrapper, not a rewrite of the argument list.
- [ ] ~~Flip its `ToolMeta` to `ON_DEMAND_IMAGE`~~ — not done.
- [ ] ~~Measure the image size change~~ — not applicable; no migration happened. The image-size figures above are recorded for whoever picks this back up.

**Why this stops here rather than proceeding anyway.** Moving Clair3 to `ON_DEMAND_IMAGE` on every architecture would remove long-read variant calling from arm64 (Apple Silicon, Graviton) entirely — there is no image to pull, where today there is a working bundled binary. That is a real regression, not a rounding error: Clair3 is the *preferred* long-read caller (DeepVariant is only ever a fallback, per `build_variants_card`), so an arm64 user would lose their default caller outright, not fall back to a slower path.

Raised explicitly rather than decided silently. Two other options were on the table and rejected:
- **Migrate anyway, amd64-only, arm64 keeps the bundled binary** (`ToolMeta.delivery` branching by `is_arm64()`) — real code, real complexity (a delivery model that differs per architecture, which nothing else in `TOOL_META` does today), for a benefit limited to shrinking the amd64 image alone.
- **Substitute a different second candidate** (checked FastQC, the spec's own other flagged candidate: also amd64-only on Docker Hub, `biocontainers/fastqc:v0.11.9_cv8`, ~340 MB, and marginal even if it did have arm64 per the spec's own "optional, marginal" verdict) — this turned out to be a *systemic* gap, not one specific to Clair3: widely-used bioinformatics tool images are routinely built amd64-only across the ecosystem. Forcing a new, larger tool (hifiasm, Kraken2) into `TOOL_META` from scratch just to have a second `ON_DEMAND_IMAGE` citizen is a materially bigger task than "migrate an existing one," answering a different question than task 8 was scoped to ask.

**Chosen instead: task 8 stops here, and DeepVariant stands as the proof.** Tasks 1–7 were built generically from the start — `Delivery`, `_probe_on_demand_image(name, image)`, `tool_install_service.install`/`.uninstall`, and `_require_or_offer_install` all take a tool name and a `Tool`, none of them hardcode "deepvariant" in their control flow — rather than being retrofitted from DeepVariant-specific code after the fact. A second migration would *validate* that genericity, not *add* it, and is not worth trading a working arm64 caller to prove.

**Revisit when:** an arm64-capable Clair3 image appears upstream (bioconda's own arm64 conda package means a maintainer-published multi-arch image is plausible, just not present today), or when a genuinely new large tool is added to the app and can be built `ON_DEMAND_IMAGE` from day one rather than migrated.

**Watch for:** the models, if this is revisited. They are baked into the image today *"so a run does not depend on the network"* — both candidate images checked above do carry them, so that risk was cleared, but re-check for whatever image is chosen if the version pin moves.

---

## Task 9 — Launcher prefetch (#40)

Nearly free once Task 1 lands, since the manifest is already served.

- [ ] Launcher queries `GET /pipelines/tools` after the stack is healthy and offers a checkbox per optional tool, with sizes, during first-run setup.
- [ ] **The list must not be hardcoded in the launcher** — that is #40's first acceptance criterion and the reason it was cut from #28.
- [ ] Declining leaves on-demand behavior unchanged.
- [ ] Note the ordering inversion #40 flags: this runs *after* the stack is up, unlike the rest of first-run setup.

Close #40 with a note on what shipped, per the TODO close-out rules.

---

## Closing out

- [ ] Move the `## Post-install tool downloads` entry from `docs/TODO.md` to `docs/TODO-done.md` **in full**, heading marked ` — FIXED`, with a note saying what shipped, when, and where the code lives — but only once the epic's user-visible slices (Tasks 0–7) are merged. If Clair3 (8) or the launcher (9) are still open, the entry **stays in `docs/TODO.md`**: an entry that is only partially resolved does not move, because moving it buries the still-open part.
- [ ] Say what the implementation did differently from this plan. Every entry closed so far departed from its own plan somewhere, and that delta is the most valuable sentence in the entry.
- [ ] Close #26 against the spec, and update epic #5's acceptance criteria.
- [ ] Record the measured image-size change if Task 8 shipped.
