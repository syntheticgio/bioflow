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

- [ ] Add to `tools.py`:
  ```python
  class Delivery(StrEnum):
      BUNDLED = "bundled"            # in the image; probe by PATH
      ON_DEMAND_IMAGE = "on_demand"  # pulled; probe by image presence
  ```
- [ ] Add three fields to `ToolMeta`, all defaulted so existing entries stay constructible: `delivery: Delivery = Delivery.BUNDLED`, `image: str | None = None`, `download_bytes: int | None = None`.
- [ ] Set `delivery=Delivery.ON_DEMAND_IMAGE` on the `deepvariant` entry, with `image` and `download_bytes` (~3 GB compressed; state which you measured, pulled or on-disk, in a comment — the button promises this number).
- [ ] `image` must resolve per architecture the way `config.default_deepvariant_image()` already does. Do not add a second arch-dispatch mechanism; either reference the existing setting or factor the pattern so both use one.
- [ ] Extend `test_every_tool_is_documented` in `tests/pipelines/test_tools.py`: a tool declaring `ON_DEMAND_IMAGE` must supply non-empty `image` and a positive `download_bytes`. A new optional tool that forgets either now fails the suite rather than shipping a button that cannot state its cost.

**No second edit needed in `tool_with_meta`** — it builds the payload with `asdict(meta)` precisely so a new `ToolMeta` field reaches the API automatically. Check that `Delivery` serializes as its string value; it is a `StrEnum`, so it will, but `pipelines` needed explicit handling and this is the same class of problem.

---

## Task 2 — An honest probe for image-delivered tools

- [ ] Add an `InstallState` to `tools.py` distinguishing three cases: `INSTALLED`, `NOT_INSTALLED` (a real, expected state — **not** an error), and `UNKNOWN` (no docker client, or daemon unreachable — the only genuine failure).
- [ ] Carry it on `Tool` as an optional field. **`Tool.available` stays a boolean** and stays false for a not-installed optional tool, so every existing caller — `require()`, the launch dialog, the aligner registry — keeps working unchanged.
- [ ] Rewrite `tools.deepvariant()` to run `docker image inspect <image>` after confirming the daemon is reachable, and set `install_state` accordingly. Keep reporting the image tag as `version` when installed.
- [ ] Give a not-installed tool an `error` string that reads as an offer, not a fault — it is what the UNAVAILABLE path renders today, and it should not say "not found."
- [ ] Generalize the probe rather than hardcoding DeepVariant: a helper taking a tool name and image reference, so Task 8's Clair3 move is a table entry rather than a second special case.

**Tests:** patch the `docker image inspect` seam to return non-zero and assert `available is False` and `install_state is NOT_INSTALLED`; patch the daemon probe to fail and assert `UNKNOWN`. That is the direction that fails when the seam breaks — asserting the installed direction passes whether or not the patch worked.

---

## Task 3 — Cross-process cache invalidation

- [ ] Generalize `tool_cache.NOT_FINGERPRINTABLE` from the hardcoded `{"deepvariant"}` to "every tool whose `ToolMeta.delivery` is `ON_DEMAND_IMAGE`", derived from `TOOL_META`. Update the comment, which currently explains the DeepVariant-specific reasoning, to state the general rule; leave the DeepVariant example in place as the worked case.
- [ ] Add a Redis pub/sub channel (`bp:tools:invalidate`) carrying a tool name.
- [ ] Publish on it at the end of a successful install or uninstall.
- [ ] Subscribe in both `api` and `worker` startup; on receipt, clear that tool's `lru_cache` entry. `tools.reset_cache()` exists but clears all twenty-six — fine as a first implementation, and cheaper to reason about than a per-tool registry. Prefer it unless per-tool clearing is trivial.
- [ ] Degrade like the rest of `tool_cache.py`: **every Redis failure here is a warning, never an exception.** A missed invalidation means a stale badge until restart; a raised exception means a failed install.

**Verify this one manually, not only in tests.** With the worktree stack up, install from the UI and confirm the *API's* view flips without a restart — a single-process test cannot show you this.

---

## Task 4 — Install and uninstall as jobs

- [ ] `app/queue/tool_handlers.py` with `install_tool` (`HandlerMode.SUBPROCESS`, `JobClass.USER_INTERACTIVE`), payload `{"tool": name}`. Shell to `docker pull`, parse its progress lines into `ctx.progress()`.
  - `USER_INTERACTIVE` deliberately, **not** `COMPUTE`: the user pressed a button and is watching. `COMPUTE` is documented as never promoted, so a download would sit behind a multi-hour alignment.
  - `max_attempts`: 2 or 3. A pull failure is often transient (network), unlike a missing binary — but do not spend five attempts on an auth or manifest error.
- [ ] `uninstall_tool` alongside it, shelling to `docker image rm`.
- [ ] Import `tool_handlers` from `app/queue/handlers.py` for the registration side effect, the way `pipeline_handlers` is imported.
- [ ] `app/services/tool_install_service.py`:
  - **One install per tool at a time.** Find an in-flight install for that tool and return it rather than starting a second. `enqueue`'s `dedup_key` does this — note it folds `owner` in, which is right here.
  - Refuse an install for a tool whose `delivery` is `BUNDLED`, and refuse an uninstall of anything not `ON_DEMAND_IMAGE` and currently installed. **This is the symmetry rule from the spec** — uninstall is offered exactly when install was.
  - Refuse an uninstall while a running job uses that tool.
- [ ] `POST /pipelines/tools/{name}/install` and `DELETE /pipelines/tools/{name}/install`, returning the job the way the other launch endpoints do (`JobOut`).
- [ ] Publish the invalidation from Task 3 on success.

**Watch for:** `docker pull` progress output is layer-interleaved and not a clean percentage. Getting a monotonic overall percentage out of it is fiddly; a phase string plus a coarse percentage is enough, and a job that reports "pulling" with no number beats a bar that jumps backwards. Don't over-invest here.

---

## Task 5 — Settings › Tools

Settings is a single page today (`SettingsView.tsx` renders `Settings · AI`, `App.tsx` routes `/settings` and `/settings/ai` to the same component), so this introduces the section nav a second page implies.

- [ ] `SettingsNav.tsx` — a rail with AI and Tools. Keep `/settings` landing where it does today so no existing link breaks.
- [ ] Route `/settings/tools` → `SettingsTools.tsx`. `App.tsx`'s `singleColumn` check already covers `/settings` by prefix, so nothing is needed there.
- [ ] `SettingsTools.tsx` renders one row per tool from `GET /pipelines/tools` — **including bundled ones**:
  - **Included** — bundled, with version, no button. Listing these is what makes the page the answer to "why is this card greyed out," and is the reason tools with no action attached still appear.
  - **Installed** — with the image tag as version, and Uninstall.
  - **Not installed** — with the download size and Install.
  - **Installing** — the job's progress, and cancel.
  - **Failed** — the job's error, and retry.
- [ ] Confirm before uninstall, naming the space reclaimed.
- [ ] Poll or invalidate the tools query while an install job is in flight, so the row advances without a manual refresh.
- [ ] **Do not add a second list of tools.** Both this page and `HelpSoftware.tsx` read the same endpoint built from `TOOL_META`. `/help/software` stays documentation and gains no buttons.

**Verify in the browser at localhost:5273** via `./ops/worktree-up.sh` — there is no headless component testing in this repo and none is expected.

---

## Task 6 — `NEEDS_INSTALL` in the Actions tab

- [ ] Add `NEEDS_INSTALL = "needs_install"` to `CardStatus` in `suggestion_service.py`.
- [ ] Add a `requires_install: dict | None` field to `SuggestionCard` (`{"tool": name, "download_bytes": n}`) and include it in `as_dict()`.
- [ ] A `NEEDS_INSTALL` card **keeps its launch payload** — it is not blocked, it is one click from working. The docstring's rule that *"`launch` and `status` must agree"* needs updating to cover the third status explicitly.
- [ ] Update the variant card builder (`suggestion_service.py:~538`): when the chemistry's caller is an uninstalled optional tool, emit `NEEDS_INSTALL` rather than `UNAVAILABLE`. Note the existing DeepVariant-as-long-read-fallback branch just above — an *installable* DeepVariant should not silently replace an uninstalled Clair3 with a 3 GB download; prefer offering the install of the tool the chemistry actually indicates.
- [ ] Frontend: render `needs_install` as an offer with the size, not a refusal. Rendering it as `UNAVAILABLE` is the worst outcome — the card reads as a permanent dead end and the user never learns the tool exists.

**Test:** patch `spec_for`/the probe so the tool reads not-installed, and assert the card is `NEEDS_INSTALL` **with** a launch payload and a `requires_install` block. Per CLAUDE.md, patch `spec_for` rather than the tool function where a frozen registry spec captured the function object at import time.

---

## Task 7 — Confirm-then-chain launch

- [ ] In `launch_variant_calling` (`pipeline_service.py:1676`), replace the bare `tools.require(tools.deepvariant())` for optional callers with a check that distinguishes not-installed from broken.
- [ ] Not installed, without consent: raise a `ValidationError` naming the tool and the download size, in the shape the dialog can render — the existing `details={"needs": ...}` pattern used for a missing `.bai`/`.fai` is the precedent to follow.
- [ ] With consent (an explicit request flag): enqueue the install job first, then the variant job with `depends_on=[install_job.id]`.
- [ ] Frontend: the launch dialog reads the refusal and offers "DeepVariant is not installed. This will download about 3 GB first." → re-posts with the consent flag.
- [ ] `_require_image` in `variant_handlers.py` **survives as a guard, not a message.** With the dependency satisfied the image is present; if it somehow is not, the job should still fail cleanly rather than emit a raw Docker error. Rewrite its text — it should no longer tell the user to open a terminal, since that is now a bug rather than an instruction.

**Watch for:** a failed install must fail the dependent job rather than leaving it blocked forever. `queue.py` already handles this (`_failed_dependencies` at line 285) — verify it, don't rebuild it.

---

## Task 8 — Move Clair3 out of the image

**This is not optional garnish.** An abstraction designed against a single example usually fits exactly that example. Clair3 has different mounts, a different output layout (it writes a fixed `merge_output.vcf.gz` that `_rename_output` already renames), and baked-in models. It is what proves the seam generalizes — and if it does not, the cost of finding out is lowest here.

- [ ] Pick a pinned Clair3 image (bioconda/biocontainers publishes one; verify arm64 availability, since the Dockerfile comment notes bioconda is the only arm64-capable distribution and that is why it is installed the way it is).
- [ ] Remove the Clair3 layer from `backend/Dockerfile`, keeping the comment's reasoning about why it was its own layer as the record of what changed.
- [ ] Add a Clair3 runner path that builds a `docker run` command, mirroring `variant_runner.build_deepvariant_command`.
- [ ] Flip its `ToolMeta` to `ON_DEMAND_IMAGE` with image and size; update `usage` to say it is downloaded on first use, as DeepVariant's entry already does.
- [ ] Measure the image size change and record it — the entry claims ~600 MB with models, and a real number is what makes this checkable later.

**Watch for:** the models. They are baked into the image today *"so a run does not depend on the network"* — confirm the chosen image carries them, or this trades one download for two.

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
