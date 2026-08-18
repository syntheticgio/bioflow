# Declared-Memory Refusal Escape Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every launcher declaring more than `MIN_DECLARED_MEM_MB` refuses at launch with a working "Launch anyway", instead of enqueuing a job the governor can never claim.

**Architecture:** Three layers, in dependency order. First the refusal payload gains a `refusal` discriminator and the frontend card learns to render a declared refusal (no estimate, no replan) — this repairs #478's unreachable escape and is a precondition for everything after it. Then fourteen launchers and their request models gain `resource_override` and a `refuse_if_over_budget` call, mirroring `launch_assembly`. Finally an exhaustiveness test derives the heavy-launcher set by inspection so a future launcher cannot silently reopen the gap.

**Tech Stack:** Python 3 / FastAPI / Pydantic / Beanie (backend), pytest, React + TypeScript + TanStack Query (frontend).

**Spec:** `docs/superpowers/specs/2026-08-18-declared-memory-refusal-escape-design.md`

## Global Constraints

- The threshold is `pipeline_service.MIN_DECLARED_MEM_MB` (currently 2048). Read the constant; never hardcode 2048. The refusal applies strictly above it.
- Run backend tests from this worktree with `./backend/run-worktree-tests.sh`, never `docker compose exec api python -m pytest` — the latter silently tests main's code.
- Commit subjects are Conventional Commits: lowercase after the colon, imperative, no trailing period, ~65 chars. Enable the hook once per checkout: `git config core.hooksPath ops/hooks`.
- Keep commits separable — one behaviour change per commit. `main` merges with `--rebase`, so the subjects survive into `CHANGELOG.md`.
- **Naming deviation from the spec, deliberate:** the spec calls the details discriminator `kind`. This plan uses **`refusal`** instead. `kind` is already taken three times in the same scopes — `ReplanResult.kind` (`"proposal" | "infeasible" | "no_knobs"`), `PipelineSuggestion.kind`, and field `kind` — and `PipelineSuggestions.tsx` would end up with `card.kind` and a refusal `kind` side by side. Values are unchanged: `"estimate" | "declared"`.

---

## File Structure

**Backend**
- `backend/app/services/pipeline_service.py` — the 14 launchers, `refuse_if_over_budget`, both estimate-path raise sites (lines 1994, 4436).
- `backend/app/api/v1/pipelines.py` — 14 request models and their routes.
- `backend/tests/services/test_declared_budget_refusal.py` — extend; existing pure-helper tests stay.
- `backend/tests/services/test_heavy_launcher_overrides.py` — new; the exhaustiveness test.

**Frontend**
- `frontend/src/api/types/pipeline.ts` — `ResourceRefusalDetails` gains `refusal`; `estimate_mb`, `estimate_source`, `replan` become optional.
- `frontend/src/components/ResourceRefusalCard.tsx` — `estimateMb`/`detail`/`replan` optional; render without the estimate line when absent.
- `frontend/src/components/PipelineSuggestions.tsx` — per-card refusal state and the shared card.
- `frontend/src/components/AssembleDialog.tsx` — switch the guard from `estimate_mb` to `refusal`.

---

### Task 1: Tag both refusal payloads with a `refusal` discriminator

Repairs the root defect: the declared refusal is indistinguishable from an arbitrary validation error, so no frontend can route it to a card.

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (`refuse_if_over_budget` ~line 1339; estimate raises at ~1994 and ~4436)
- Test: `backend/tests/services/test_declared_budget_refusal.py`

**Interfaces:**
- Consumes: nothing.
- Produces: every resource refusal carries `details["refusal"]`, either `"declared"` or `"estimate"`. Tasks 2 and 4 depend on this key existing.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_declared_budget_refusal.py`:

```python
def test_declared_refusal_is_tagged_for_the_frontend():
    """R4: the card is routed by this key, not by sniffing estimate_mb.

    #478 shipped without it, so AssembleDialog's `"estimate_mb" in details`
    guard was false for every declared refusal and the escape hatch the
    message promises never rendered.
    """
    with pytest.raises(ValidationError) as excinfo:
        pipeline_service.refuse_if_over_budget(
            declared_mb=16384, budget_mb=5600, resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"
    assert excinfo.value.details["declared_mb"] == 16384
    assert excinfo.value.details["budget_mb"] == 5600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./backend/run-worktree-tests.sh tests/services/test_declared_budget_refusal.py::test_declared_refusal_is_tagged_for_the_frontend -v`
Expected: FAIL with `KeyError: 'refusal'`

- [ ] **Step 3: Add the key to the declared path**

In `refuse_if_over_budget`, change the raise's `details` to:

```python
        details={
            "refusal": "declared",
            "declared_mb": declared_mb,
            "budget_mb": budget_mb,
        },
```

- [ ] **Step 4: Add the key to both estimate paths**

At `pipeline_service.py` line ~1994 and line ~4436, both `details={` blocks open with `"estimate_mb": estimate,`. Add one line immediately above that key in each:

```python
                    "refusal": "estimate",
```

- [ ] **Step 5: Run the whole refusal file**

Run: `./backend/run-worktree-tests.sh tests/services/test_declared_budget_refusal.py -v`
Expected: PASS, all tests including the pre-existing ones.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pipeline_service.py backend/tests/services/test_declared_budget_refusal.py
git commit -m "fix(api): tag resource refusals so the frontend can route them to a card"
```

---

### Task 2: Render a declared refusal in the card instead of a toast

The card currently requires an estimate and a replan, neither of which a declared refusal has. Without this, Task 3's refusals all degrade to toasts.

**Files:**
- Modify: `frontend/src/api/types/pipeline.ts:163-169`
- Modify: `frontend/src/components/ResourceRefusalCard.tsx`
- Modify: `frontend/src/components/AssembleDialog.tsx:126-133, 255-290`

**Interfaces:**
- Consumes: `details.refusal` from Task 1.
- Produces: `ResourceRefusalCard` accepts `estimateMb?: number`, `detail?: string`, `replan: ReplanResult | null`, and renders correctly with all three absent. Task 4 renders this same component.

- [ ] **Step 1: Widen the details type**

In `frontend/src/api/types/pipeline.ts`, replace the `ResourceRefusalDetails` interface:

```typescript
export interface ResourceRefusalDetails {
  /** Which refusal this is. "declared" carries no estimate and no replan:
   *  nothing about the run changes a fixed reservation, which is why the
   *  card must render without them. */
  refusal: "estimate" | "declared";
  budget_mb: number;
  /** Present only when refusal === "estimate". */
  estimate_mb?: number;
  /** Present only when refusal === "declared". */
  declared_mb?: number;
  estimate_source?: "measured" | "heuristic" | "declared" | "unknown";
  detail?: string;
  replan?: ReplanResult;
}
```

- [ ] **Step 2: Make the card's estimate props optional**

In `ResourceRefusalCard.tsx`, change the props interface entries for `estimateMb` and `detail` to optional, leaving every other prop as-is:

```typescript
  /** Absent for a declared refusal, which has no estimate to report. */
  estimateMb?: number;
  /** The prose phrase from memory_estimate.resolve() -- "from 23 previous
   *  runs on this machine" or "from published tool coefficients". Absent
   *  for a declared refusal. */
  detail?: string;
```

- [ ] **Step 3: Guard the estimate line**

In `ResourceRefusalCard.tsx`, replace the `Estimated {estimateMb.toLocaleString()} MB ...` block with a guarded version. This is the line that would throw on `undefined.toLocaleString()`:

```tsx
      {estimateMb !== undefined && (
        <div style={{ marginTop: 4, opacity: 0.85 }}>
          Estimated {estimateMb.toLocaleString()} MB {detail}, against a{" "}
          {budgetMb.toLocaleString()} MB budget.
        </div>
      )}
```

- [ ] **Step 4: Switch AssembleDialog's guard**

In `AssembleDialog.tsx`, in the `launch` mutation's `onError`, replace the `estimate_mb` sniff:

```typescript
    onError: (e: Error) => {
      if (e instanceof ApiRequestError && "refusal" in e.details) {
        setRefusal(e.details as unknown as ResourceRefusalDetails);
        return;
      }
      notify.error(e.message);
    },
```

- [ ] **Step 5: Make AssembleDialog's card render both refusals**

In `AssembleDialog.tsx`, replace the `<ResourceRefusalCard ... />` invocation's `estimateMb`, `explanation`, and `replan` props:

```tsx
            <ResourceRefusalCard
              estimateMb={refusal.estimate_mb}
              budgetMb={refusal.budget_mb}
              detail={refusal.detail}
              explanation={
                refusal.refusal === "declared"
                  ? `This assembly reserves ${(refusal.declared_mb ?? 0).toLocaleString()} MB, ` +
                    `more than the ${refusal.budget_mb.toLocaleString()} MB budget. ` +
                    `Nothing about the run changes that number.`
                  : `This assembly needs about ${(refusal.estimate_mb ?? 0).toLocaleString()} MB, ` +
                    `more than the ${refusal.budget_mb.toLocaleString()} MB available.`
              }
              replan={refusal.replan ?? null}
```

Leave the four handler props and `launchAnywayPending` exactly as they are.

- [ ] **Step 6: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors. If `AlignDialog.tsx` errors on its `replan` prop, it passes a `ReplanResult | null` already and needs no change — read the error before editing.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/types/pipeline.ts frontend/src/components/ResourceRefusalCard.tsx frontend/src/components/AssembleDialog.tsx
git commit -m "fix(ui): show the refusal card for a declared over-budget launch, not a toast"
```

---

### Task 3: Give the fourteen heavy launchers an override and a refusal

The mechanical core. Fourteen launchers, each the same four edits.

**Files:**
- Modify: `backend/app/services/pipeline_service.py` (14 launchers)
- Modify: `backend/app/api/v1/pipelines.py` (14 request models and routes)
- Test: `backend/tests/services/test_declared_budget_refusal.py`

**Interfaces:**
- Consumes: `refuse_if_over_budget(*, declared_mb: int, budget_mb: int, resource_override: bool) -> None` and `current_admission_budget_mb() -> int`, both existing.
- Produces: each of the 14 launchers accepts a keyword-only `resource_override: bool = False`; each request model exposes `resource_override: bool = False`. Task 5's exhaustiveness test asserts exactly this.

The fourteen, with the line of their `mem_mb` declaration:

| `mem_mb` | Launcher | Line | Request model |
|---|---|---|---|
| 16384 | `launch_annotate_genome` | 4995 | `AnnotateGenomeRequest` |
| 16384 | `launch_polish` | 5299 | `PolishRequest` |
| 16384 | `launch_continuity_qc` | 6400 | `AssemblyContinuityRequest` |
| 12288 | `launch_qv_qc` | 6139 | `AssemblyQvRequest` |
| 8192 | `launch_variant_calling` | 3270 | `VariantRequest` |
| 8192 | `launch_completeness` | 4682 | `CompletenessRequest` |
| 8192 | `launch_meryl_analysis` | 4893 | `MerylAnalysisRequest` |
| 8192 | `launch_consensus` | 5129 | `ConsensusRequest` |
| 8192 | `launch_scaffold` | 5443 | `ScaffoldRequest` |
| 8192 | `launch_misassembly_qc` | 5570 | `MisassemblyQcRequest` |
| 8192 | `launch_synteny` | 5695 | `SyntenyRequest` |
| 8192 | `launch_assembly_error_qc` | 5832 | `AssemblyErrorRequest` |
| 4096 | `launch_quantify` | 3875 | `QuantifyRequest` |
| 4096 | `launch_differential_expression` | 4113 | `DifferentialExpressionRequest` |

Line numbers drift as you edit. After the first launcher, locate the rest by name: `grep -n "async def launch_polish" backend/app/services/pipeline_service.py`.

- [ ] **Step 1: Write the failing test for the first launcher**

Append to `backend/tests/services/test_declared_budget_refusal.py`:

```python
@pytest.mark.asyncio
async def test_annotate_genome_refuses_over_budget(monkeypatch):
    """R1: the issue's headline case -- 16384 MB against a smaller budget."""
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    with pytest.raises(ValidationError) as excinfo:
        await pipeline_service.launch_annotate_genome(
            object_id=PydanticObjectId(), owner="t", resource_override=False
        )
    assert excinfo.value.details["refusal"] == "declared"
```

Add the helper and imports at the top of the file:

```python
from beanie import PydanticObjectId


def _budget_of(mb: int):
    """A stand-in for current_admission_budget_mb, which reads the database."""

    async def _budget() -> int:
        return mb

    return _budget
```

- [ ] **Step 2: Run it to confirm it fails for the right reason**

Run: `./backend/run-worktree-tests.sh tests/services/test_declared_budget_refusal.py::test_annotate_genome_refuses_over_budget -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'resource_override'`.

If it instead fails earlier — on object lookup, because the launcher validates its input before reaching the budget check — that is the signal to hoist the refusal above the input validation, exactly as `launch_assembly` hoists its own. Note which launchers needed hoisting; Task 4's manual test depends on the refusal being reachable for a real object.

- [ ] **Step 3: Change the first launcher**

In `launch_annotate_genome`, add the parameter:

```python
async def launch_annotate_genome(
    *,
    object_id: PydanticObjectId,
    owner: str,
    resource_override: bool = False,
) -> Job:
```

Immediately before the `job = await queue.enqueue(` call, add the check. `ANNOTATE_GENOME_MEM_MB` replaces the literal so the declaration has one home:

```python
    # Hoisted above the enqueue for the same reason as launch_assembly: a
    # declaration the budget can never satisfy is unclaimable, and claim.lua
    # has no starvation escape (#478, #527).
    refuse_if_over_budget(
        declared_mb=ANNOTATE_GENOME_MEM_MB,
        budget_mb=await current_admission_budget_mb(),
        resource_override=resource_override,
    )
```

Define the constant next to `UNKNOWN_ASSEMBLY_MEM_MB` near line 1313:

```python
# Bakta's declared reservation. Named rather than inlined so the launcher and
# the exhaustiveness test in test_heavy_launcher_overrides.py read the same
# number.
ANNOTATE_GENOME_MEM_MB = 16384
```

and use it in the enqueue:

```python
        resources=JobResources(cpu=8, mem_mb=ANNOTATE_GENOME_MEM_MB, io=IoClass.HEAVY),
```

Then pass the flag through the enqueue by adding one argument to the same call:

```python
        resource_override=resource_override,
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./backend/run-worktree-tests.sh tests/services/test_declared_budget_refusal.py::test_annotate_genome_refuses_over_budget -v`
Expected: PASS

- [ ] **Step 4b: Write the override test (R2)**

Refusing is half the contract; the escape has to actually enqueue. Append:

```python
@pytest.mark.asyncio
async def test_annotate_genome_override_enqueues_with_the_flag(monkeypatch):
    """R2: 'Launch anyway' reaches the job, where claim.lua reads it."""
    monkeypatch.setattr(
        pipeline_service, "current_admission_budget_mb", _budget_of(5600)
    )
    captured = {}

    async def _fake_enqueue(job_type, **kwargs):
        captured.update(kwargs)
        return None  # the launcher raises ConflictError; we only need the call

    monkeypatch.setattr(pipeline_service.queue, "enqueue", _fake_enqueue)
    with pytest.raises(Exception):
        await pipeline_service.launch_annotate_genome(
            object_id=PydanticObjectId(), owner="t", resource_override=True
        )
    assert captured["resource_override"] is True
```

`queue` is imported inside each launcher body, so patch it where the launcher
resolves it. If `monkeypatch.setattr(pipeline_service.queue, ...)` raises
`AttributeError`, patch `app.queue.queue.enqueue` directly instead — read the
launcher's own import line before choosing.

- [ ] **Step 5: Wire the route**

In `backend/app/api/v1/pipelines.py`, add the field to `AnnotateGenomeRequest`:

```python
class AnnotateGenomeRequest(BaseModel):
    object_id: PydanticObjectId
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False
```

and forward it in `launch_annotate_genome_route`:

```python
    job = await pipeline_service.launch_annotate_genome(
        object_id=body.object_id,
        owner=owner,
        resource_override=body.resource_override,
    )
```

- [ ] **Step 6: Commit the first launcher on its own**

```bash
git add backend/app/services/pipeline_service.py backend/app/api/v1/pipelines.py backend/tests/services/test_declared_budget_refusal.py
git commit -m "fix(pipelines): refuse an over-budget genome annotation instead of stranding it"
```

- [ ] **Step 7: Repeat Steps 1-6 for the remaining thirteen**

Same five edits each: named constant, `resource_override` parameter, `refuse_if_over_budget` before the enqueue, `resource_override=resource_override` in the enqueue, request-model field plus route forwarding. Constant names follow the launcher: `POLISH_MEM_MB`, `CONTINUITY_QC_MEM_MB`, `QV_QC_MEM_MB`, `VARIANT_CALLING_MEM_MB`, `COMPLETENESS_MEM_MB`, `MERYL_ANALYSIS_MEM_MB`, `CONSENSUS_MEM_MB`, `SCAFFOLD_MEM_MB`, `MISASSEMBLY_QC_MEM_MB`, `SYNTENY_MEM_MB`, `ASSEMBLY_ERROR_QC_MEM_MB`, `QUANTIFY_MEM_MB`, `DIFFERENTIAL_EXPRESSION_MEM_MB`.

Two launchers declare `cpu=merged.threads` or `cpu=resolved_threads` alongside the flat memory (`launch_variant_calling`, `launch_quantify`) — change only `mem_mb`, leave the cpu expression alone.

Write the matching test per launcher before its edit, in the shape of Step 1. Commit per launcher, or in small related groups (the three assembly-QC launchers together is reasonable); do not squash all fourteen into one commit — a revert of one must not drag the other thirteen.

- [ ] **Step 8: Run the full backend suite**

Run: `./backend/run-worktree-tests.sh tests/ -q`
Expected: PASS. Read the counts, not the exit code.

---

### Task 4: Give Actions cards a refusal card with a working escape

Nine of the fourteen have no dialog. Without this task those nine are refusals with no escape — the exact regression #527 warns against.

**Files:**
- Modify: `frontend/src/components/PipelineSuggestions.tsx:131-146` and its render block

**Interfaces:**
- Consumes: `details.refusal` (Task 1), the optional-prop card (Task 2), `api.launchSuggestion(endpoint, body, targetNode?)`.
- Produces: nothing downstream.

- [ ] **Step 1: Add refusal state, keyed like `launched`**

In `PipelineSuggestions.tsx`, beside the `launched` state:

```typescript
  // Keyed by `kind` for the same reason `launched` is: the grid keys its
  // cards by it, so a refusal renders under the card that caused it rather
  // than at the top of the grid.
  const [refusals, setRefusals] = useState<Record<string, ResourceRefusalDetails>>({});
```

Add `ResourceRefusalDetails` and `ApiRequestError` to the existing imports from `../api/types` and the api module respectively, and `ResourceRefusalCard` from `./ResourceRefusalCard`.

- [ ] **Step 2: Route the refusal instead of toasting it**

Replace the `launch` mutation's `onError`. It needs the card, so it takes the two-argument form:

```typescript
    onError: (e: Error, card) => {
      if (e instanceof ApiRequestError && "refusal" in e.details) {
        setRefusals((m) => ({
          ...m,
          [card.kind]: e.details as unknown as ResourceRefusalDetails,
        }));
        return;
      }
      notify.error(e.message);
    },
```

- [ ] **Step 3: Add the Launch-anyway mutation**

Beside `launch`. It re-posts the card's own body, so this component stays ignorant of the fourteen request shapes:

```typescript
  const launchAnyway = useMutation({
    mutationFn: (card: PipelineSuggestion) =>
      api.launchSuggestion(
        card.launch!.endpoint,
        { ...card.launch!.body, resource_override: true },
        targetNode || undefined,
      ),
    onSuccess: (_job, card) => {
      setRefusals((m) => {
        const next = { ...m };
        delete next[card.kind];
        return next;
      });
      setLaunched((m) => ({ ...m, [card.kind]: { at: Date.now() } }));
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["suggestions", objectId] });
      notify.success("Queued without the memory check");
    },
    onError: (e: Error) => notify.error(e.message),
  });
```

- [ ] **Step 4: Render the card under its own suggestion**

Inside the per-card JSX, after the Launch/Adjust button row:

```tsx
              {refusals[card.kind] && (
                <ResourceRefusalCard
                  estimateMb={refusals[card.kind].estimate_mb}
                  budgetMb={refusals[card.kind].budget_mb}
                  detail={refusals[card.kind].detail}
                  explanation={
                    `This reserves ${(refusals[card.kind].declared_mb ?? 0).toLocaleString()} MB, ` +
                    `more than the ${refusals[card.kind].budget_mb.toLocaleString()} MB budget. ` +
                    `Raise it in Settings, or launch it anyway to run it alone.`
                  }
                  replan={refusals[card.kind].replan ?? null}
                  onCancel={() =>
                    setRefusals((m) => {
                      const next = { ...m };
                      delete next[card.kind];
                      return next;
                    })
                  }
                  // A card with no dialog has no parameters to edit, so this
                  // exit is the same as dismissing. Where the server named a
                  // dialog, send the user there instead -- the same handler
                  // the Adjust button uses.
                  onEdit={() =>
                    card.configure && onConfigure
                      ? onConfigure(card.configure.dialog, card.launch!.body)
                      : setRefusals((m) => {
                          const next = { ...m };
                          delete next[card.kind];
                          return next;
                        })
                  }
                  onLaunchAnyway={() => launchAnyway.mutate(card)}
                  launchAnywayPending={launchAnyway.isPending}
                  onAcceptReplan={() => undefined}
                />
              )}
```

`onAcceptReplan` is a no-op because a declared refusal carries no replan, so the button that calls it never renders (`ResourceRefusalCard` gates it on `proposal`).

- [ ] **Step 5: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/PipelineSuggestions.tsx
git commit -m "feat(ui): offer \"Launch anyway\" on an Actions card refused for memory"
```

---

### Task 5: Pin the invariant so a new heavy launcher cannot regress it

The durable deliverable. Without it this issue recurs the moment someone adds a launcher.

**Files:**
- Create: `backend/tests/services/test_heavy_launcher_overrides.py`

**Interfaces:**
- Consumes: `pipeline_service.MIN_DECLARED_MEM_MB` and the 14 launchers from Task 3.
- Produces: nothing downstream.

- [ ] **Step 1: Write the test**

```python
"""Every heavy launcher must have an escape hatch (#527).

The registry-pair shape CLAUDE.md prescribes: derive the set by inspection
rather than hand-listing it, so a launcher added above the threshold without
`resource_override` fails here instead of silently enqueuing a job the
governor can never claim.

Deriving from source text rather than from the call signature is deliberate:
the declaration is a literal inside a JobResources(...) call, and nothing at
import time exposes it.
"""

import ast
import inspect
import re

from app.services import pipeline_service

# A launcher below this floor cannot be refused by a budget that lets anything
# else run, so the check there would be dead code. Read, never hardcoded.
THRESHOLD_MB = pipeline_service.MIN_DECLARED_MEM_MB


def _declared_mem_mb(func) -> int | None:
    """The flat mem_mb this launcher declares, or None if it computes one."""
    source = inspect.getsource(func)
    literals = [int(m) for m in re.findall(r"mem_mb=(\d+)", source)]
    if literals:
        return max(literals)
    # A named constant, as Task 3 introduces: resolve it off the module.
    names = re.findall(r"mem_mb=([A-Z_][A-Z0-9_]*)", source)
    values = [
        getattr(pipeline_service, n)
        for n in names
        if isinstance(getattr(pipeline_service, n, None), int)
    ]
    return max(values) if values else None


def _launchers():
    for name, obj in vars(pipeline_service).items():
        if name.startswith("launch_") and inspect.isfunction(obj):
            yield name, obj


def _accepts_override(func) -> bool:
    return "resource_override" in inspect.signature(func).parameters


def _calls_the_refusal(func) -> bool:
    tree = ast.parse(inspect.getsource(func).lstrip())
    return any(
        isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "refuse_if_over_budget"
        for node in ast.walk(tree)
    )


def test_every_heavy_launcher_accepts_an_override():
    """R8: the exhaustiveness half of the pair."""
    missing = sorted(
        name
        for name, func in _launchers()
        if (declared := _declared_mem_mb(func)) is not None
        and declared > THRESHOLD_MB
        and not _accepts_override(func)
    )
    assert not missing, (
        f"These launchers declare more than {THRESHOLD_MB} MB with no "
        f"'Launch anyway' escape, so an over-budget run waits forever: {missing}"
    )


def test_every_heavy_launcher_refuses_over_budget():
    """R8: accepting the flag means nothing if nothing checks the budget."""
    missing = sorted(
        name
        for name, func in _launchers()
        if (declared := _declared_mem_mb(func)) is not None
        and declared > THRESHOLD_MB
        and not _calls_the_refusal(func)
    )
    assert not missing, (
        f"These launchers accept an override but never call "
        f"refuse_if_over_budget, so the flag is inert: {missing}"
    )


def test_the_threshold_actually_partitions_the_launchers():
    """The guard against a vacuous pass.

    Both tests above pass trivially if the detector finds nothing. This pins
    that it finds launchers on both sides -- the failure CLAUDE.md describes,
    where a green suite means the seam broke rather than the code is right.
    """
    declared = {
        name: mb
        for name, func in _launchers()
        if (mb := _declared_mem_mb(func)) is not None
    }
    assert any(mb > THRESHOLD_MB for mb in declared.values())
    assert any(mb <= THRESHOLD_MB for mb in declared.values())
    assert len(declared) >= 20, (
        f"Only found {len(declared)} declarations; the detector regexes have "
        f"probably stopped matching how the launchers are written."
    )
```

- [ ] **Step 1b: Add the request-model half (R9)**

A launcher can accept the flag while its route silently drops it, which is
invisible from the service layer. Append to the same file:

```python
def test_every_heavy_launcher_route_exposes_the_override():
    """R9: the flag is useless if the request model cannot carry it.

    Walks the route handlers rather than a hand-listed set of models, so a
    launcher wired to a new model is covered without editing this test.
    """
    from app.api.v1 import pipelines as routes

    heavy = {
        name
        for name, func in _launchers()
        if (mb := _declared_mem_mb(func)) is not None and mb > THRESHOLD_MB
    }
    source = inspect.getsource(routes)
    missing = sorted(
        name
        for name in heavy
        if f"pipeline_service.{name}(" in source
        and "resource_override=body.resource_override"
        not in source.split(f"pipeline_service.{name}(")[1][:400]
    )
    assert not missing, (
        f"These routes call a heavy launcher without forwarding "
        f"resource_override, so the card's button posts a flag the route "
        f"discards: {missing}"
    )
```

- [ ] **Step 2: Run the whole file together**

Run: `./backend/run-worktree-tests.sh tests/services/test_heavy_launcher_overrides.py -v`
Expected: all four PASS.

Run the file, never a single test — this is the partition-completeness trap from CLAUDE.md, where two independently-correct fixes collide and only the whole file catches it.

- [ ] **Step 3: Verify the test actually detects a regression**

Temporarily delete the `resource_override` parameter from `launch_polish`, re-run the file, and confirm `test_every_heavy_launcher_accepts_an_override` fails naming `launch_polish`. Restore it. A detector that cannot fail is the failure mode this test exists to prevent.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/services/test_heavy_launcher_overrides.py
git commit -m "test(pipelines): require an escape hatch on every heavy launcher"
```

---

### Task 6: Verify in the browser, then merge

The defect this plan opens with passed a full green suite. Only the running UI closes it out.

**Files:** none — verification only.

- [ ] **Step 1: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

UI on 5273, API on 8100. The main stack on 5173 keeps serving main.

- [ ] **Step 2: Force refusals**

In Settings on 5273, lower the memory budget below 4096 MB so every one of the fourteen is over budget.

- [ ] **Step 3: Verify the Actions-card path (R5)**

Open a bacterial assembly with an Actions card for genome annotation. Click Launch. Confirm: a refusal card renders under that card — not a toast — naming the reserved MB and the budget, with a "Launch anyway" button. Click it; confirm the job queues and appears in Activity.

- [ ] **Step 4: Verify the dialog path and the #478 regression (R6)**

Open the Assemble dialog on a long-read FASTQ with no genome size, so it declares `UNKNOWN_ASSEMBLY_MEM_MB`. Launch. Confirm the card renders rather than a toast. This is the case that was broken on `main` before this branch.

- [ ] **Step 5: Verify the card degrades cleanly (R7)**

On both refusals above, confirm no "A smaller configuration fits" block, no "Estimated ... MB" line, and no console error. An `undefined.toLocaleString()` throw would show as a blank card.

- [ ] **Step 6: Verify the regression guard (R3)**

Raise the budget back above 16384 MB. Relaunch the annotation. Confirm it queues with no refusal.

- [ ] **Step 7: Bring the stack down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 8: Close out the issue trail**

Check whether `docs/TODO.md` holds an entry for #478's escape hatch or #527. If so, append ` — FIXED` with what shipped and how it differed, and move the whole entry to `docs/TODO-done.md`.

- [ ] **Step 9: Rebase, push, and open the PR**

```bash
git fetch origin main
git rebase origin/main
```

```bash
git diff origin/main...HEAD --stat
```

Confirm the file list is the four source files plus two test files, and that nothing looks reverted.

```bash
git push -u origin HEAD
```

```bash
gh pr create --base main --fill
```

Title: `fix(pipelines): refuse over-budget launches with a working "Launch anyway"`. The description must carry `Closes #527`, and must say that the #478 escape hatch was unreachable and is repaired here — that is the part a reader six months on will not reconstruct from the diff. Label it `type: bug` and `area: backend`.

- [ ] **Step 10: Poll CI, then merge**

```bash
gh pr checks <N>
```

Poll until every check reports pass — not pending, not "the ones I looked at". Watch for `ruff` import-order (`I001`), which the local suite does not invoke. Then:

```bash
gh pr merge <N> --rebase --delete-branch
```

- [ ] **Step 11: Remove the worktree**

Per CLAUDE.md, a merged PR is the signal to tear down the worktree.
