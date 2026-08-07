# Job Failure Explainer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cached, on-demand plain-language explanation of a failed job's `error.code` + `error.message`, shown via an "Explain this error" expander beneath the raw error line in `ActivityView.tsx`.

**Architecture:** A new `TaskSlot.FAILURE_EXPLANATION`, served synchronously at request time and cached in a new `FailureExplanation` collection -- both modeled directly on `OrganismBlurb`/`organism_service.py`, not on the job-queue-based `FILE_SUMMARY` pattern, because this is wanted the instant a user clicks rather than something to have ready in advance.

**Tech Stack:** Python (FastAPI, Beanie/MongoDB), React + TypeScript, TanStack Query.

---

## File Structure

- Modify: `backend/app/models/ai.py` -- add `FAILURE_EXPLANATION` to `TaskSlot`.
- Create: `backend/app/models/failure_explanation.py` -- the `FailureExplanation` document and `normalize_failure()`, mirroring `app/models/organism.py`.
- Modify: `backend/app/models/__init__.py` -- export `FailureExplanation` and `normalize_failure`, mirroring the existing `OrganismBlurb`/`normalize_organism` exports.
- Create: `backend/app/services/failure_explanation_prompt.py` -- `FAILURE_SYSTEM_PROMPT` and `build_failure_prompt()`.
- Create: `backend/app/services/failure_explanation_service.py` -- `get_or_generate()`, mirroring `organism_service.py`.
- Modify: `backend/app/api/v1/pipelines.py` -- add `GET /pipelines/failure-explanation` endpoint, mirroring `get_organism_blurb`.
- Modify: `frontend/src/api/client.ts` -- add `failureExplanation()`.
- Modify: `frontend/src/components/ActivityView.tsx` -- add the "Explain this error" expander beneath the raw error line.
- Create: `backend/tests/services/test_failure_explanation_prompt.py`
- Create: `backend/tests/services/test_failure_explanation_service.py` -- mirrors `test_organism_service.py`.
- Create: `backend/tests/models/test_failure_explanation.py` -- mirrors the `normalize_organism` tests in `test_organism_service.py`, but for `normalize_failure`.

---

## Task 1: Add the FAILURE_EXPLANATION TaskSlot

**Files:**
- Modify: `backend/app/models/ai.py`
- Test: `backend/tests/models/test_ai_task_slot.py`

- [ ] **Step 1: Write the failing test**

If `backend/tests/models/test_ai_task_slot.py` already exists (it may, from
prior work adding `DE_SUMMARY`/`VARIANT_SUMMARY`), add this test to it. If
not, create it with just this test.

```python
from app.models.ai import TaskSlot


def test_failure_explanation_slot_has_a_label():
    assert TaskSlot.FAILURE_EXPLANATION.label == "Job failure explanations"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/models/test_ai_task_slot.py -v`
Expected: FAIL with `AttributeError: FAILURE_EXPLANATION`

- [ ] **Step 3: Add the slot**

In `backend/app/models/ai.py`, add to `TaskSlot` and `_SLOT_LABELS`:

```python
    FAILURE_EXPLANATION = "failure_explanation"
```

```python
    TaskSlot.FAILURE_EXPLANATION: "Job failure explanations",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec api python -m pytest tests/models/test_ai_task_slot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/ai.py backend/tests/models/test_ai_task_slot.py
git commit -m "feat: add FAILURE_EXPLANATION task slot"
```

---

## Task 2: FailureExplanation model and normalize_failure

**Files:**
- Create: `backend/app/models/failure_explanation.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/models/test_failure_explanation.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/models/test_failure_explanation.py
"""The cache key for a job failure explanation.

Unlike normalize_organism's human-readable key, this hashes: error messages
are unbounded in length and character content (embedded paths, quotes,
newlines), which makes them unsuitable as a literal indexed string.
"""

from app.models import normalize_failure


class TestNormalization:
    def test_the_same_code_and_message_produce_the_same_key(self):
        a = normalize_failure("CalledProcessError", "exit status 1")
        b = normalize_failure("CalledProcessError", "exit status 1")
        assert a == b

    def test_a_different_message_with_the_same_code_produces_a_different_key(self):
        a = normalize_failure("CalledProcessError", "exit status 1")
        b = normalize_failure("CalledProcessError", "exit status 2")
        assert a != b

    def test_a_different_code_with_the_same_message_produces_a_different_key(self):
        """The same message text can mean different things depending on which
        code raised it, so code alone must distinguish the key."""
        a = normalize_failure("CalledProcessError", "no such file or directory")
        b = normalize_failure("PermanentError", "no such file or directory")
        assert a != b

    def test_the_key_is_a_fixed_length_hash(self):
        key = normalize_failure("X", "y" * 5000)
        assert len(key) == 32
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec api python -m pytest tests/models/test_failure_explanation.py -v`
Expected: FAIL with `ImportError: cannot import name 'normalize_failure'`

- [ ] **Step 3: Write the model**

```python
# backend/app/models/failure_explanation.py
"""Cached plain-language explanations of job errors.

Its own collection rather than a field on Job, because the same underlying
error recurs across many jobs and many users -- the same tool crash (e.g.
minimap2 exiting 1 on a missing index) produces the same code and message on
every occurrence, and keying the cache on that pair means it is explained
once and every later occurrence is a free indexed read, the same reasoning
OrganismBlurb (app/models/organism.py) uses for species background text.

Nothing here is authoritative. It is a plain-language restatement of a given
error string, regenerable at any time, so the collection can be dropped
without losing anything that matters.
"""

import hashlib
from datetime import datetime

from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument, utcnow


def normalize_failure(code: str, message: str) -> str:
    """The cache key: a hash of code and message together.

    Unlike normalize_organism's human-readable lowercase key, a hash is
    required here -- error messages are unbounded in length and character
    content (embedded paths, quotes, newlines), unsuitable as a literal
    indexed string. Code is hashed together with message, not separately:
    the same message text can mean different things depending on which code
    raised it ("no such file or directory" under a permanent config error
    reads differently than under a transient subprocess failure), so both
    must distinguish the key.
    """
    payload = f"{code}\x00{message}".encode()
    return hashlib.sha256(payload).hexdigest()[:32]


class FailureExplanation(TimestampedDocument):
    """A cached plain-language explanation of one job error."""

    # The cache key. Unique, so two jobs failing with the same error
    # concurrently cannot produce two rows.
    failure_key: str
    # Stored alongside the hash purely for inspectability -- a developer
    # reading this collection can see what a key maps to. Never queried on.
    code: str
    message: str
    text: str
    model: str | None = None
    generated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "failure_explanations"
        indexes = [
            IndexModel([("failure_key", ASCENDING)], unique=True, name="uniq_failure"),
        ]
```

- [ ] **Step 4: Export from app/models/__init__.py**

Read `backend/app/models/__init__.py` around lines 36 and 66 and 102/123
(where `OrganismBlurb`/`normalize_organism` are imported and re-exported)
and add matching lines for `FailureExplanation`/`normalize_failure`:

```python
from app.models.failure_explanation import FailureExplanation, normalize_failure
```

Add `"FailureExplanation"` and `"normalize_failure"` to the module's
`__all__` list in the same positions their `OrganismBlurb`/
`normalize_organism` counterparts occupy.

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/models/test_failure_explanation.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/failure_explanation.py backend/app/models/__init__.py backend/tests/models/test_failure_explanation.py
git commit -m "feat: add FailureExplanation model and normalize_failure"
```

---

## Task 3: Failure explanation prompt

**Files:**
- Create: `backend/app/services/failure_explanation_prompt.py`
- Test: `backend/tests/services/test_failure_explanation_prompt.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/services/test_failure_explanation_prompt.py
"""What goes into the failure explanation prompt.

Same discipline as test_summary_prompt.py: the model is given exactly the
code and message, nothing else, and the system prompt forbids proposing a
fix or a root cause the text does not state.
"""

from app.services.failure_explanation_prompt import (
    FAILURE_SYSTEM_PROMPT,
    build_failure_prompt,
)


class TestPromptContent:
    def test_the_code_and_message_both_reach_the_prompt(self):
        prompt = build_failure_prompt("CalledProcessError", "exit status 1")
        assert "CalledProcessError" in prompt
        assert "exit status 1" in prompt

    def test_the_traceback_is_never_a_parameter(self):
        """build_failure_prompt takes only code and message -- asserted by
        the call succeeding with exactly two arguments and no keyword for a
        traceback."""
        prompt = build_failure_prompt(code="X", message="y")
        assert prompt is not None


class TestSystemPrompt:
    def test_the_system_prompt_forbids_proposing_a_fix(self):
        assert "fix" in FAILURE_SYSTEM_PROMPT.lower()
        assert "never propose" in FAILURE_SYSTEM_PROMPT.lower()

    def test_the_system_prompt_forbids_asserting_certainty(self):
        assert "certainty" in FAILURE_SYSTEM_PROMPT.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec api python -m pytest tests/services/test_failure_explanation_prompt.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the prompt module**

```python
# backend/app/services/failure_explanation_prompt.py
"""Turning a job's raw error code and message into a prompt worth answering.

The input here is the least structured of anything this app hands a model:
`code` is sometimes a clean PermanentError code and sometimes a bare Python
exception class name (CalledProcessError, KeyError, whatever a library
happened to raise), and `message` is str(exception) -- free text with no
guaranteed shape. traceback_tail is deliberately never passed in: it is
mostly file paths and line numbers with no interpretive content for a
scientist, and this module's whole job is picking out what does.
"""

FAILURE_SYSTEM_PROMPT = (
    "You are a bioinformatics core facility analyst explaining a failed "
    "computation to the scientist who ran it. You are given only an error "
    "code and an error message -- nothing else about the job.\n\n"
    "Write 1-3 sentences of plain prose. No headings, no bullet points, no "
    "markdown, no preamble such as 'Here is an explanation'. Start directly "
    "with the substance.\n\n"
    "What to do:\n"
    "1. Restate, in everyday language, what kind of problem this error "
    "text describes.\n"
    "2. If the text supports it, name the general category: a problem "
    "with the input data or files, a configuration problem, a resource "
    "problem (disk space, memory), or an environment problem (a missing "
    "tool, a permissions issue). Only name a category the text actually "
    "indicates -- do not guess one to seem more useful.\n\n"
    "Rules you must follow:\n"
    "- Never propose a specific fix, a command to run, or a setting to "
    "change. You do not have enough information to be right, and a wrong "
    "fix suggestion is worse than none.\n"
    "- Never state a root cause the given text does not support. If the "
    "text does not say what caused the problem, do not invent one.\n"
    "- Never assert certainty about the cause. Prefer 'this usually means' "
    "or 'this suggests' over 'this means' or 'this is because'.\n"
    "- If the code and message are too opaque or generic to say anything "
    "useful about -- a bare exception class name with no real message, for "
    "example -- say so briefly in one sentence rather than inventing an "
    "explanation."
)


def build_failure_prompt(code: str, message: str) -> str:
    """The user turn for a failure explanation.

    Trivially small on purpose, like build_organism_prompt: the error text
    is the entire input, and the system prompt carries all of the shaping.
    """
    return (
        f"Error code: {code}\n"
        f"Error message: {message}\n\n"
        "Explain this error, following every rule in your instructions."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/services/test_failure_explanation_prompt.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/failure_explanation_prompt.py backend/tests/services/test_failure_explanation_prompt.py
git commit -m "feat: add failure explanation prompt"
```

---

## Task 4: Failure explanation service

**Files:**
- Create: `backend/app/services/failure_explanation_service.py`
- Test: `backend/tests/services/test_failure_explanation_service.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/services/test_failure_explanation_service.py
"""The explanation generation path -- mocked at the AI seam, not the
network. Mirrors test_organism_service.py's TestGetOrGenerate exactly.
"""

import pytest

from app.services import failure_explanation_service


async def _async_none(*a, **k):
    return None


async def _provider(*a, **k):
    from app.models.ai import ProviderKind
    from app.services.ai.router import ResolvedProvider

    return ResolvedProvider(
        provider_id="000000000000000000000000",
        name="Test",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1",
        api_key=None,
        model="test-model",
        models_cache=[],
    )


async def _completion(*a, **k):
    from app.services.ai.adapters import Completion

    return Completion("The process could not find one of its input files.", "test-model")


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
class TestGetOrGenerate:
    async def test_no_provider_configured_yields_none(self, monkeypatch, beanie_models):
        monkeypatch.setattr(failure_explanation_service.ai_router, "resolve", _async_none)
        result = await failure_explanation_service.get_or_generate(
            "CalledProcessError", "exit status 1"
        )
        assert result is None

    async def test_a_successful_completion_is_cached_and_returned(
        self, monkeypatch, beanie_models
    ):
        monkeypatch.setattr(failure_explanation_service.ai_router, "resolve", _provider)
        monkeypatch.setattr(failure_explanation_service.ai_complete, "complete", _completion)

        result = await failure_explanation_service.get_or_generate(
            "CalledProcessError", "exit status 1"
        )
        assert result is not None
        assert result.text == "The process could not find one of its input files."
        assert result.model == "test-model"

        cached = await failure_explanation_service.get_cached(
            "CalledProcessError", "exit status 1"
        )
        assert cached is not None
        assert cached.text == "The process could not find one of its input files."

    async def test_a_cache_hit_does_not_call_the_model_again(
        self, monkeypatch, beanie_models
    ):
        # Reuses the row written by the previous test -- same code/message,
        # so this must be a cache hit.
        monkeypatch.setattr(failure_explanation_service.ai_router, "resolve", _provider)

        def must_not_run(*a, **k):
            raise AssertionError("a cache hit must not call the model")

        monkeypatch.setattr(failure_explanation_service.ai_complete, "complete", must_not_run)

        result = await failure_explanation_service.get_or_generate(
            "CalledProcessError", "exit status 1"
        )
        assert result is not None
        assert result.text == "The process could not find one of its input files."

    async def test_a_non_completion_result_yields_none(self, monkeypatch, beanie_models):
        from app.services.ai.adapters import Failure

        async def _failure(*a, **k):
            return Failure("bad_key")

        monkeypatch.setattr(failure_explanation_service.ai_router, "resolve", _provider)
        monkeypatch.setattr(failure_explanation_service.ai_complete, "complete", _failure)

        # A distinct code/message pair from the cache-hit tests above, so
        # this reaches the (patched) completion call rather than short
        # circuiting on an existing row.
        result = await failure_explanation_service.get_or_generate(
            "KeyError", "'reference_path'"
        )
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose exec api python -m pytest tests/services/test_failure_explanation_service.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the service**

```python
# backend/app/services/failure_explanation_service.py
"""A plain-language explanation of a job error, generated once and cached.

Read-through cache: a hit is a single indexed document read, and a miss
calls the model and stores the result. Like the organism blurb and unlike
the file summary, this does not go through the job queue -- the explanation
is wanted the instant a user clicks "Explain this error," it takes one short
generation, and a queued job would mean the panel shows an empty state that
pops in seconds later. A synchronous call the UI can show a loading state
for is the honest presentation of that.

Every failure yields None. The explanation is a plain-language restatement
of an error the user can already see verbatim; a model that is not running,
or one that produces nothing, simply means no restatement appears, exactly
as before this existed.
"""

import importlib

from app.logging import get_logger
from app.models import FailureExplanation, normalize_failure
from app.services import failure_explanation_prompt
from app.services.ai import router as ai_router
from app.services.ai.adapters import Completion

# NOT `from app.services.ai import complete as ai_complete`: see
# organism_service.py's identical comment for why this goes through
# importlib rather than a normal import.
ai_complete = importlib.import_module("app.services.ai.complete")

log = get_logger(__name__)


async def get_cached(code: str, message: str) -> FailureExplanation | None:
    """The stored explanation for an error, if one has been written."""
    return await FailureExplanation.find_one(
        FailureExplanation.failure_key == normalize_failure(code, message)
    )


async def get_or_generate(code: str, message: str) -> FailureExplanation | None:
    """The explanation for an error, generating and caching it on a miss."""
    from app.config import settings

    if not settings.llm_summaries_enabled:
        return None

    key = normalize_failure(code, message)
    cached = await FailureExplanation.find_one(FailureExplanation.failure_key == key)
    if cached is not None:
        return cached

    from app.models.ai import TaskSlot

    provider = await ai_router.resolve(TaskSlot.FAILURE_EXPLANATION)
    if provider is None:
        return None

    result = await ai_complete.complete(
        provider,
        system=failure_explanation_prompt.FAILURE_SYSTEM_PROMPT,
        user=failure_explanation_prompt.build_failure_prompt(code, message),
        # Shorter than a file summary: this is one to three sentences, and
        # the cap is what stops a chatty model from writing an essay.
        max_tokens=200,
    )
    if not isinstance(result, Completion):
        return None

    text, model = result.text, result.model
    log.info("failure_explanation_generated", key=key, model=model, chars=len(text))

    # Upsert rather than insert: two jobs failing with the same error can
    # reach here concurrently, and the unique index would turn the loser's
    # insert into an error over an explanation that is already correct.
    await FailureExplanation.find_one(FailureExplanation.failure_key == key).upsert(
        {
            "$set": {
                "code": code,
                "message": message,
                "text": text,
                "model": model,
                "generated_at": _now(),
                "updated_at": _now(),
            }
        },
        on_insert=FailureExplanation(
            failure_key=key,
            code=code,
            message=message,
            text=text,
            model=model,
        ),
    )

    return await FailureExplanation.find_one(FailureExplanation.failure_key == key)


def _now():
    from app.models.base import utcnow

    return utcnow()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose exec api python -m pytest tests/services/test_failure_explanation_service.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/failure_explanation_service.py backend/tests/services/test_failure_explanation_service.py
git commit -m "feat: add failure explanation service"
```

---

## Task 5: API endpoint

**Files:**
- Modify: `backend/app/api/v1/pipelines.py`
- Test: `backend/tests/api/test_failure_explanation_endpoint.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/api/test_failure_explanation_endpoint.py
"""The failure explanation endpoint mirrors GET /pipelines/organism/{organism}
exactly -- returns null rather than 404 when there is nothing to say, since
no provider and a model producing nothing are both ordinary states for a
decorative field, not errors the client should handle differently.
"""

import pytest


@pytest.mark.asyncio
async def test_returns_null_with_no_provider_configured(client):
    resp = await client.get(
        "/pipelines/failure-explanation",
        params={"code": "CalledProcessError", "message": "exit status 1"},
    )
    assert resp.status_code == 200
    assert resp.json() is None
```

Fixture name (`client`) is a placeholder matching whatever the existing
`backend/tests/api/` suite already provides for an authenticated or
unauthenticated API-client fixture -- check an existing test file covering
`/pipelines/organism/{organism}` and match its fixture usage exactly. Note
this endpoint is not owner-scoped (see Step 2's docstring), so it likely
needs no `owner_headers` fixture at all, unlike the DE/variant summary
endpoints -- confirm this by reading how the existing organism endpoint's
tests (if any exist yet) are written, or by reading the organism endpoint
itself, which takes no `OwnerDep`.

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/api/test_failure_explanation_endpoint.py -v`
Expected: FAIL with a 404 routing error

- [ ] **Step 3: Add the endpoint**

In `backend/app/api/v1/pipelines.py`, immediately after `get_organism_blurb`
(after its closing), add:

```python
class FailureExplanationOut(BaseModel):
    text: str
    model: str | None = None


@router.get("/failure-explanation", response_model=FailureExplanationOut | None)
async def get_failure_explanation(code: str, message: str) -> FailureExplanationOut | None:
    """A plain-language explanation of a job error, from cache or freshly
    generated.

    Mirrors get_organism_blurb exactly: returns null rather than 404 when
    there is nothing to say -- no provider configured and a model that
    produced nothing are both ordinary states for this decorative field.

    Deliberately *not* owner-scoped, same reasoning as get_organism_blurb:
    there is one provider routing for the whole machine, and the
    explanation depends only on the error text, not on who is looking at
    it -- two profiles hitting the identical tool crash should share the
    one generation.

    GET with query params rather than the POST-with-body shape
    /pipelines/summary uses: this is a read (cache lookup, generating only
    on a miss) with no side effect the caller directs, matching
    /pipelines/organism/{organism}'s shape more closely than the job-launch
    endpoints'.
    """
    from app.services import failure_explanation_service

    explanation = await failure_explanation_service.get_or_generate(code, message)
    if explanation is None:
        return None
    return FailureExplanationOut(text=explanation.text, model=explanation.model)
```

`code`/`message` go in the query string rather than the path (unlike the
organism endpoint's `{organism}` path param), since they are not
URL-path-safe identifiers. FastAPI handles the necessary encoding
transparently; no manual `encodeURIComponent`-equivalent is needed in the
endpoint itself, only in the frontend client (Task 6).

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec api python -m pytest tests/api/test_failure_explanation_endpoint.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/tests/api/test_failure_explanation_endpoint.py
git commit -m "feat: add GET /pipelines/failure-explanation endpoint"
```

---

## Task 6: Frontend API client function

**Files:**
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add the client function**

In `frontend/src/api/client.ts`, immediately after the existing
`organismBlurb` function (around line 520-523), add:

```typescript
  /**
   * A plain-language explanation of a job error, from cache or freshly
   * generated. Returns null when there is no provider configured or the
   * model produced nothing -- both ordinary states, not an error for a
   * decorative field.
   *
   * Cached server-side per (code, message) pair, so re-explaining the same
   * underlying error on a different job is an indexed read.
   */
  failureExplanation: (code: string, message: string) =>
    request<{ text: string; model: string | null } | null>(
      `/pipelines/failure-explanation?code=${encodeURIComponent(code)}&message=${encodeURIComponent(message)}`,
    ),
```

Match this file's existing convention for query-string construction --
check whether other `request<...>` calls elsewhere in this file build query
strings with manual `encodeURIComponent` concatenation (as
`organismBlurb`'s path-param encoding suggests) or via `URLSearchParams`,
and use whichever the file already does consistently.

- [ ] **Step 2: Verify the frontend typechecks**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no new type errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/client.ts
git commit -m "feat: add failureExplanation API client function"
```

---

## Task 7: Render the explainer in ActivityView

**Files:**
- Modify: `frontend/src/components/ActivityView.tsx`

- [ ] **Step 1: Read the existing error block and surrounding component**

Read `ActivityView.tsx` around lines 355-375 (the `JobRow`-equivalent
component containing the `{job.error && (...)}` block shown in the design
research) in full, to see what state/hooks the enclosing component already
has, before adding new state to it.

- [ ] **Step 2: Add the expander**

Add local state for whether the explanation has been requested/is loading,
and the fetched result, to the component containing the error block (likely
via `useState`, matching this file's existing patterns elsewhere -- check
how `logOpen` toggling is implemented just below the error block, since
it's the same "toggle a detail section" shape this needs).

Replace:

```tsx
{job.error && (
  <div style={{ color: "var(--error)", fontSize: 11, marginTop: 3 }}>
    {job.error.code}: {job.error.message}
  </div>
)}
```

with:

```tsx
{job.error && (
  <div style={{ color: "var(--error)", fontSize: 11, marginTop: 3 }}>
    {job.error.code}: {job.error.message}
    <FailureExplanationExpander code={job.error.code} message={job.error.message} />
  </div>
)}
```

- [ ] **Step 3: Write the expander component**

Add this new component in the same file, near the top-level component it's
used from (or as a separate export if this file already splits multiple
components out -- match the file's existing convention):

```tsx
/**
 * "Explain this error" -- click-triggered only, never generated
 * automatically on job failure. A model that is not configured or that
 * produces nothing means the button simply does not appear; the raw
 * error text above it is never replaced or hidden.
 */
function FailureExplanationExpander({
  code,
  message,
}: {
  code: string;
  message: string;
}) {
  const [state, setState] = useState<
    | { status: "idle" }
    | { status: "loading" }
    | { status: "unavailable" }
    | { status: "shown"; text: string; model: string | null }
  >({ status: "idle" });

  if (state.status === "unavailable") return null;

  if (state.status === "shown") {
    return (
      <div style={{ marginTop: 4, color: "var(--text-secondary)" }}>
        {state.text}
        {state.model && (
          <span style={{ color: "var(--text-faint)" }}> (AI-generated, {state.model})</span>
        )}
      </div>
    );
  }

  return (
    <button
      type="button"
      className="btn-text"
      style={{ marginLeft: 8 }}
      disabled={state.status === "loading"}
      onClick={async () => {
        setState({ status: "loading" });
        try {
          const result = await api.failureExplanation(code, message);
          if (result == null) {
            setState({ status: "unavailable" });
          } else {
            setState({ status: "shown", text: result.text, model: result.model });
          }
        } catch {
          setState({ status: "unavailable" });
        }
      }}
    >
      {state.status === "loading" ? "Explaining…" : "Explain this error"}
    </button>
  );
}
```

Confirm `useState` and `api` are already imported in this file (both are
near-certainly already present, given the file's existing query/mutation
usage) -- add the imports only if genuinely missing.

- [ ] **Step 4: Verify typecheck**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no new errors

- [ ] **Step 5: Manual verification in the browser**

Start the worktree stack:

```bash
./ops/worktree-up.sh
```

Trigger a job failure (e.g. launch an alignment against a malformed or
missing reference), open the Activity view, and confirm:
- The raw `code: message` line still renders exactly as before.
- With no provider routed to `FAILURE_EXPLANATION`, clicking "Explain this
  error" makes the button disappear (loading, then gone) rather than
  showing an error.
- After configuring a provider and routing `FAILURE_EXPLANATION` to it (the
  settings page's per-slot row, added automatically by Task 1), clicking
  produces an explanation below the raw error line.
- Clicking "Explain this error" on a second, unrelated job failure with the
  same underlying tool error produces the explanation without a
  perceptible delay (cache hit) -- confirm via the network tab or backend
  logs (`failure_explanation_generated` should log only once for two
  identical code/message pairs).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/ActivityView.tsx
git commit -m "feat: render on-demand failure explanation in ActivityView"
```

---

## Task 8: Full test suite check

**Files:** none (verification task)

- [ ] **Step 1: Run the full backend suite**

From a worktree: `./backend/run-worktree-tests.sh tests/ -q`
From the main checkout: `docker compose exec api python -m pytest tests/ -q`

Expected: all tests pass; read the printed count -- it should have grown by
exactly the number of new tests added in Tasks 1-5.

- [ ] **Step 2: Run the frontend typecheck**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no errors

- [ ] **Step 3: Fix any regressions**

Diagnose against the specific new code from Tasks 1-7 if anything fails --
do not modify unrelated passing tests to make them pass.

---

## Task 9: Merge to main and push

**Files:** none (merge task)

- [ ] **Step 1: Merge**

Per this repo's CLAUDE.md: once the suite is green and `main` is clean,
merge and push without waiting for further permission.

```bash
git checkout main
git pull
git merge --no-ff <feature-branch>
docker compose exec api python -m pytest tests/ -q
git push origin main
```

If `main` has moved since this branch was created, re-run the full suite
after merging rather than trusting the pre-merge green.
