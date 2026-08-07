# Job failure explainer

**Date:** 2026-08-04
**Status:** Approved, not yet implemented

## Problem

`ActivityView.tsx` shows a failed job's `error.code` and `error.message`
verbatim (`ActivityView.tsx:367-369`) -- whatever a Python exception's
`str()` happened to produce, or a `PermanentError`'s own message. That is
sometimes clear ("no space left on device") and sometimes not
("CalledProcessError: Command '[...]' returned non-zero exit status 1"),
and the person reading it has no way to tell which kind of problem it is
without knowing the tool that failed. There is no interpretation layer
between "the raw error text" and "the scientist," the same gap `FILE_SUMMARY`
already closes for QC numbers.

## What ships

A new `TaskSlot.FAILURE_EXPLANATION`, and a cached, on-demand plain-language
explanation of a job's `error.code` + `error.message`, shown via an
"Explain this error" expander under the existing raw error line in
`ActivityView.tsx`. Generated synchronously at request time -- like
`ORGANISM_BLURB`, not like `FILE_SUMMARY` -- because this is wanted the
moment the user clicks, not something to have ready by the time they look.

### Data model

A new `FailureExplanation` document, modeled directly on `OrganismBlurb`
(`app/models/organism.py`):

```python
class FailureExplanation(TimestampedDocument):
    """A cached plain-language explanation of one job error."""

    # sha256(code + "\x00" + message)[:32] -- see normalize_failure() below.
    failure_key: str
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

`code` and `message` are stored alongside the hash purely for
inspectability (so a developer can read the collection and see what a key
maps to) -- `failure_key` is the only field ever queried on.

### Cache key

`normalize_failure(code: str, message: str) -> str` hashes `code` and
`message` together (not just `message` alone -- the same message text can
mean different things depending on which code raised it). Unlike
`normalize_organism`'s human-readable lowercase key, a hash is required
here: unlike species names, error messages are unbounded in length and
character content (embedded paths, quotes, newlines), which makes them
unsuitable as a literal indexed string key.

`traceback_tail` is deliberately excluded from both the cache key and the
prompt. It is mostly file paths and line numbers with no interpretive
content for a scientist, and including it would fragment the cache: the
same logical error (e.g. minimap2 exiting 1 on a missing index) recurs
across many jobs with a traceback that differs only in line numbers, and a
key that includes it would treat every occurrence as new.

### Service

`app/services/failure_explanation_service.py`, mirroring
`organism_service.get_or_generate` exactly:

```python
async def get_or_generate(code: str, message: str) -> FailureExplanation | None:
```

Read-through cache: hash the inputs, look up `FailureExplanation`, return it
on a hit. On a miss, resolve `TaskSlot.FAILURE_EXPLANATION`; if no provider,
return `None`. Otherwise call the model with `FAILURE_SYSTEM_PROMPT` and
`build_failure_prompt(code, message)`, capped at a short `max_tokens` (same
reasoning as the organism blurb's 250-token cap -- this is two or three
sentences, not an essay). Upsert on success, same race-safe upsert pattern
`organism_service.py` already uses for two jobs failing with the same error
concurrently.

### Prompt

`build_failure_prompt(code, message)` hands the model exactly those two
strings, nothing else. `FAILURE_SYSTEM_PROMPT` follows the same discipline
as every other summary prompt in this app:

- Write 1-3 sentences of plain prose explaining, in everyday language, what
  kind of problem this error text describes -- restate and contextualize,
  do not diagnose beyond what the text supports.
- Name the general category when the text supports it: an input/file
  problem, a configuration problem, a resource problem (disk, memory), or
  an environment problem (a missing tool, a permission issue) -- but only
  when the given text actually indicates that category, not by guessing.
- Never propose a specific fix, a command to run, or a root cause the text
  does not state. This app's other AI features restate given numbers;
  this one restates a given error string, and the same rule applies: only
  use what you were given.
- Never claim certainty about the cause. Prefer "this usually means" over
  "this means."
- If the text is opaque or too generic to say anything useful about (bare
  exception class names with no message, for example), say so briefly
  rather than inventing an explanation.

### UI

In `ActivityView.tsx`, beneath the existing raw `job.error.code:
job.error.message` line, an "Explain this error" text button. Clicking it
calls the new endpoint and, on a non-empty response, replaces the button
with the explanation text (plus a small "AI-generated" note, matching how
`AiSummary` labels its model). No auto-generation on job failure, and no
polling -- purely click-triggered. If the call returns nothing (no
provider, or the model produced nothing), the button simply disappears
rather than showing an error -- same self-suppressing contract as
`AiSummary` and the organism blurb.

The raw `error.code`/`error.message` line is never replaced or hidden; the
explanation is additive underneath it.

### API

```
GET /pipelines/failure-explanation?code=...&message=...
Response: { "text": "...", "model": "..." } | null
```

GET with query params, not POST with a JSON body: the closer precedent
turned out to be `GET /pipelines/organism/{organism}` -- a cached
read-or-generate endpoint with a null-on-nothing response, not the
job-launch POST endpoints this spec originally reasoned from. FastAPI
handles query-param encoding transparently, so the earlier concern about
awkward URL-encoding of long/special-character messages does not actually
apply. Not owner-scoped, matching both `/pipelines/summary/status`'s and
`/pipelines/organism/{organism}`'s reasoning: there is one provider routing
for the whole machine, and the explanation depends only on the error text,
not on who is looking at it -- two profiles hitting the same tool crash
should share one generation.

### Error handling

- No provider configured for `FAILURE_EXPLANATION` -> endpoint returns
  `null`, button never renders an explanation.
- Model call fails or returns nothing -> same `null`, same silent
  non-appearance.

### Testing

- Unit tests for `normalize_failure` (same-code-different-message and
  same-message-different-code both produce different keys; whitespace
  differences in message do not, if the implementation trims).
- Unit tests for `build_failure_prompt` / `FAILURE_SYSTEM_PROMPT` existing
  and containing the "never propose a fix" rule, mirroring how
  `test_summary_prompt.py` asserts on rule text.
- Service tests for `get_or_generate`, mirroring the shape of any existing
  `organism_service` tests: cache hit skips the model call, cache miss
  calls it and stores the result, no provider returns None.
- API test for the endpoint's request/response shape and its 204 path.

## Out of scope

- Auto-generating explanations when a job fails or goes DEAD. This is
  strictly on-demand.
- Including `traceback_tail` in the prompt or cache key.
- Proposing fixes, commands, or specific remediation steps -- restating
  and categorizing only.
- Any change to how errors are captured or classified in
  `queue/executor.py` -- this feature reads `job.error.code` and
  `job.error.message` as they already exist today.
