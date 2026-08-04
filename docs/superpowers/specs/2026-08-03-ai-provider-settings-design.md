# AI provider settings

A settings page that replaces the single hardcoded model server with a list of
configured providers and a per-task routing table. Two adapters cover every
provider named: OpenAI-compatible, and Anthropic.

## Why this, and what already exists

`app/services/llm_client.py` is 145 lines of stdlib `urllib` speaking
OpenAI-compatible chat completions to one server, whose address is a global:
`settings.llm_base_url`, defaulting to `http://host.docker.internal:11234`.
`settings.llm_model` optionally pins a model; empty means "ask `/v1/models` and
use whatever reports `loaded`".

Two call sites consume it -- `queue/summary_handlers.py` (narrative file
summaries) and `services/organism_service.py` (organism blurbs) -- and
`api/v1/pipelines.py:160` exposes `GET /pipelines/llm/status`, which the UI uses
to decide whether to show the summarize button.

The module's opening docstring states the invariant everything else depends on:
every failure is "return None and log", never "raise and fail the job". Summaries
are additive, so a dead model server means nothing appears and nothing breaks.
**That invariant survives this design unchanged.** What changes is that a
failure now leaves a trace.

Two pieces of the current implementation are LM Studio-specific rather than
OpenAI-spec: the `/health` probe used by `is_available()`, and the `loaded` flag
read from `/v1/models`. Both stop being load-bearing here.

There is no settings page today. The app's routes are `/`, `/p/:projectId`,
`/search`, `/activity`, and five `/help/*` pages. There is also no credential
handling anywhere in this codebase -- this is the first secret it stores.

## Scope

In:

- A `/settings` route, master-detail, with an AI section.
- Multiple configured providers, each with its own base URL, key, and model.
- Per-task routing: named slots in code, each assignable to a provider, with a
  default for unassigned slots.
- Two adapters (`openai_compat`, `anthropic`) plus a preset table of base URLs.
- API keys encrypted at rest, never returned by the API.
- Typed failure reasons recorded on both the provider and the job.

Out, deliberately:

- **A fast/cheap tier toggle.** Attractive -- one switch moving every slot to
  the cheapest model -- but it needs a per-model cost notion that does not exist
  yet. The slot model extends to it later without a migration.
- **Automatic fallback** to another provider when the routed one fails. Silently
  spending money on a hosted provider because the local one was off is the same
  class of surprise as an environment variable quietly activating OpenAI. If a
  provider fails, the summary does not appear and the reason is visible.
- **Environment-variable key fallback.** Rejected for the same reason: a key in
  the environment that activates a paid provider the user never configured is
  magical in the bad direction.
- **Custom provider kinds** beyond the two adapters. "Local / custom" is a
  preset with an editable URL, not a plugin system.
- **Per-provider cost tracking, streaming, retries.**

## Data model

One new collection and one singleton document.

`ai_providers`, one document per configured provider:

```
_id            ObjectId
name           str            # user-facing label, e.g. "Local (LM Studio)"
kind           "openai_compat" | "anthropic"
base_url       str            # seeded from the preset, editable
api_key_enc    bytes | None   # Fernet ciphertext; None for keyless local servers
key_hint       str | None     # "sk-ant-...4f2a" -- the only form the API returns
model          str            # chosen model id
models_cache   list[str]      # last successful /v1/models fetch
status         "ok" | "failed" | "untested"
status_reason  str | None     # see Failure taxonomy
checked_at     datetime | None
created_at     datetime
updated_at     datetime
```

`ai_routing`, a singleton keyed by a fixed `_id`:

```
default        ObjectId | None         # used by any slot not named in `slots`
slots          {slot_name: ObjectId}   # only explicitly overridden slots
```

Slot names come from a `TaskSlot` enum in code, initially `FILE_SUMMARY` and
`ORGANISM_BLURB`. The settings page renders one row per member, so adding a
feature means adding an enum member and a row appears. A slot absent from
`slots` means "use default" -- a real state, not a null needing cleanup.

Two choices worth naming:

- **`key_hint` is stored, not derived.** It cannot be recomputed without
  decrypting, and every read path wants it.
- **Deleting a routed provider clears the affected slots** back to default
  rather than refusing the delete. The alternative is an error telling the user
  to go undo three things first.

## Backend structure

`app/services/llm_client.py` becomes the package `app/services/ai/`:

- **`presets.py`** -- the static table: `{id, label, kind, base_url, needs_key}`
  for OpenAI, Anthropic, DeepSeek, Qwen (DashScope), Moonshot, Zhipu,
  OpenRouter, and Local/custom. Pure data. A new provider is one entry.
- **`adapters.py`** -- `OpenAICompatAdapter` and `AnthropicAdapter`. Each exposes
  `complete(system, user, model, max_tokens) -> Completion | Failure` and
  `list_models() -> list[str] | Failure`, and is constructed from a resolved
  provider. Today's `llm_client` body lands here with an `Authorization` header
  added and the globals removed. Anthropic differs in four ways that this class
  exists to absorb: `/v1/messages`, `x-api-key` rather than `Authorization`, a
  required `anthropic-version` header, and the system prompt as a top-level
  field rather than a message.
- **`crypto.py`** -- the key file at `{BIOINFO_HOME}/.biopipe/secret.key`,
  generated on first use with mode `0600`, plus `encrypt`/`decrypt`.
  `.biopipe/` already holds the sentinel and lock files.
- **`provider_service.py`** -- CRUD over `ai_providers`, plus
  `fetch_models(provider_id)`, the single action that both tests a connection and
  refreshes `models_cache`, `status`, and `checked_at`.
- **`router.py`** -- `resolve(slot) -> ResolvedProvider | None`. Reads
  `ai_routing`, falls back to default, decrypts the key. The one function call
  sites touch.
- **`redaction.py`** -- scrubs known key values from log lines and upstream error
  bodies before either is stored.

Call sites change shape minimally:

```python
provider = ai.router.resolve(TaskSlot.FILE_SUMMARY)
if provider is None:
    return  # nothing configured -- the same non-event as today
result = ai.complete(provider, system=..., user=...)
```

`result` is a completion (text plus model id, as today) or a typed `Failure`.
`complete` returns; it does not raise.

Settings that survive: `llm_summaries_enabled` (master off switch),
`llm_timeout_seconds`, `llm_health_timeout_seconds`, `llm_max_tokens`. Settings
that are removed: `llm_base_url` and `llm_model`, now per-provider.

**Migration.** On first startup after this ships, if `ai_providers` is empty,
seed one provider from the existing `LLM_BASE_URL` / `LLM_MODEL` values (kind
`openai_compat`, no key, name "Local") and point `default` at it. An existing
setup keeps working without anyone opening the settings page.

## API

New router at `app/api/v1/settings.py`, mounted under `/api/v1/settings`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/settings/ai/presets` | The preset table, for the add-provider form |
| `GET` | `/settings/ai/providers` | All providers; `key_hint` and `has_key` only |
| `POST` | `/settings/ai/providers` | Create; accepts a full `api_key` |
| `PATCH` | `/settings/ai/providers/{id}` | Update; omitted `api_key` preserves the stored one, explicit `null` clears it |
| `DELETE` | `/settings/ai/providers/{id}` | Delete; clears any slots routed to it |
| `POST` | `/settings/ai/providers/{id}/fetch-models` | Fetch `/v1/models`; updates `models_cache`, `status`, `checked_at`; returns the list |
| `GET` | `/settings/ai/routing` | Routing doc plus the slot catalog (name and human label) so the UI does not hardcode it |
| `PUT` | `/settings/ai/routing` | Set default and slot assignments |

**No endpoint returns a full API key**, in any response shape. This is the
security property doing the most work here and it is asserted in tests.

The `PATCH` key semantics are what make a write-only key field work: the UI omits
`api_key` unless the user typed a new one, so editing an unrelated field cannot
wipe the key.

`GET /pipelines/llm/status` changes meaning: from "is the LM Studio server up?"
to "is the provider routed to `FILE_SUMMARY` usable?", returning `{available,
reason, model, provider_name}`. For a **local** provider it probes live, because
that answer genuinely changes minute to minute -- the server is a process the
user starts and stops by hand, which is what today's code comments say. For a
**hosted** provider it reports the stored status without a network call: the
failure mode there is not "the server is down" but "the key is wrong or the
credit ran out", which is not worth polling for. The existing summarize button
needs no frontend change beyond richer copy.

These routes are deliberately **not owner-scoped**, matching the precedent in
`api/v1/pipelines.py:160-172`: one machine, one set of providers, and a profile
header should not change what the settings page shows.
`tests/api/test_route_owner_scoping.py` has an exemption list the new paths must
join.

## Connection testing and model discovery

These are one action, not two. Fetching `/v1/models` proves the base URL
resolves, proves the key is accepted, and returns the model dropdown's contents
in a single round trip. There is no separate "test connection" concept.

Anthropic's model list is `GET /v1/models` with `x-api-key` and
`anthropic-version` -- a different endpoint, same idea, absorbed by the adapter.

**The model field is a combo box**, not a strict dropdown: options come from
`models_cache`, but free text is accepted. Some OpenAI-compatible servers
implement `/v1/models` poorly, OpenRouter returns hundreds of entries, and a
model id the user knows is valid should not be blocked by a listing endpoint
having a bad day.

A failed fetch keeps the previous `models_cache` and sets `status: failed` with a
reason. LM Studio's `loaded` flag, where present, sorts resident models first --
opportunistic, never required.

## Frontend

New route `/settings` (with `/settings/ai`), reached from the header nav
alongside Projects, Search, and Activity. Master-detail:

```
Settings > AI
+--------------+------------------------------------+
| Local (LMS)  |  Anthropic                         |
| Anthropic  * |  Preset      [Anthropic         v] |
| DeepSeek     |  Base URL    api.anthropic.com     |
| + Add        |  API key     set (sk-ant-...4f2a)  |
| ------------ |              leave blank to keep   |
| Task routing |  Model       [claude-...  v |type] |
+--------------+  [Fetch models]  [Delete]          |
               |  ok - checked 2 min ago            |
               |  Used by: Organism blurbs          |
               +------------------------------------+
```

Components: `SettingsView.tsx` (route and shell), `ProviderList.tsx`,
`ProviderForm.tsx`, `ModelCombo.tsx`, `TaskRoutingPanel.tsx` (a Default row plus
one row per slot, each defaulting to "Use default").

**The "Used by" line buys back what master-detail costs.** Its one real weakness
is that routing lives behind a click, so "what is actually using Anthropic?"
would otherwise be unanswerable while looking at Anthropic.

**Security copy on the page**, stated plainly rather than reassuringly: *API keys
are encrypted at rest. Anyone with access to this machine can decrypt them --
this is not a hardened system.* That is the honest scope of the protection: the
key file sits on the same disk as the database, so encryption defends against a
Mongo-level look (an opened Compass window, a stray `mongodump`) and not against
shell access.

The status badge shows `ok` / `failed (reason)` / `untested` with the age of
`checked_at`, and can turn red from a failed job rather than only from a manual
fetch.

Verification is manual in the browser, per `CLAUDE.md` -- there is no
component-testing setup in this repo and none is expected. From a worktree,
`./ops/worktree-up.sh` serves this UI at localhost:5273.

## Failure handling

Adapters map upstream responses onto one small enum, since a 401 from Anthropic
and a 401 from DeepSeek mean the same thing to the user:

| Reason | Trigger |
|---|---|
| `invalid_key` | 401 or 403 |
| `rate_limited` | 429 |
| `model_not_found` | 404, or a 400 naming the model |
| `unreachable` | connection refused, DNS failure, timeout |
| `bad_response` | 200 with an unparseable body |

A failure returns; it never raises. Then it is written in two places: onto the
**provider document** (`status`, `status_reason`, `checked_at`), so the settings
badge reflects real usage and not merely the last manual fetch; and onto the
**job record**, so a summary that did not appear has a visible reason instead of
being a silent no-op.

This is the one place the original "return None and log" contract is extended.
It was written for a single local server that is free to call and often simply
off, where invisibility costs nothing. Once keys and money are involved, an
expired key that silently stops producing summaries is a configuration problem
the user cannot see. The job still cannot fail.

Redaction runs over every log line and every upstream error body before storage.
Some providers echo part of the key back in an error, and those bodies are now
persisted.

## Testing

Backend via pytest. From a worktree this must be `./backend/run-worktree-tests.sh
tests/ -q` -- `docker compose exec api` silently tests main's code instead.

- `crypto.py`: round-trip; key file created `0600`; an existing key file is
  reused, not regenerated.
- `adapters.py`, both kinds against stubbed `urlopen`: success, each failure
  reason, and the Anthropic system-prompt-as-top-level-field shape.
- `provider_service.py`: PATCH with `api_key` omitted preserves the stored key;
  explicit null clears it. This is the test that matters most -- it is the one
  whose failure silently destroys a credential.
- `router.py`: a slot override wins over default; an absent slot falls back to
  default; no default returns None.
- Deleting a routed provider clears its slots rather than orphaning them.
- **No settings response contains a full key**, asserted across every response
  shape.
- Migration seeds a provider from legacy env values when the collection is empty.

Per `CLAUDE.md`'s warning that a green suite can describe hand-built fixtures
rather than reality, one check against a live provider -- `docker compose exec
api python -c ...` against the actual LM Studio server -- is worth more here than
another adapter fixture.
