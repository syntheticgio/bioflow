# AI Provider Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single hardcoded model server with a settings page where the user configures multiple AI providers (OpenAI, Anthropic, DeepSeek, Qwen, Moonshot, Zhipu, OpenRouter, local) and routes each AI task to one of them.

**Architecture:** `app/services/llm_client.py` becomes the package `app/services/ai/`, holding two adapters (`openai_compat`, `anthropic`), a preset table of base URLs, Fernet encryption for API keys, and a router that resolves a named task slot to a configured provider. Two new Beanie documents (`AiProvider`, `AiRouting`) back a new `/api/v1/settings/ai` router and a master-detail `/settings` page in the frontend.

**Tech Stack:** FastAPI, Beanie (MongoDB ODM), Pydantic v2, `cryptography` (new dependency, for Fernet), stdlib `urllib` for HTTP, React + react-router + TanStack Query, pytest.

**Spec:** `docs/superpowers/specs/2026-08-03-ai-provider-settings-design.md`

---

## Conventions for every task

**Run tests from this worktree with:**

```bash
./backend/run-worktree-tests.sh <path> -q
```

Never `docker compose exec api python -m pytest` — from a worktree that silently runs *main's* code and every result describes the wrong tree. See CLAUDE.md, "Verifying changes".

**Beanie documents need a live Mongo.** Tests that instantiate or save `AiProvider` / `AiRouting` must request the `beanie_models` fixture from `backend/tests/conftest.py`. Pure-function tests (crypto, presets, adapters against a stubbed `urlopen`) must NOT request it — it drags a database dependency into tests that do not need one.

**`asyncio_mode = "auto"`** is set in `pyproject.toml`, so `async def test_...` needs no `@pytest.mark.asyncio` decorator.

**Adding a model to `ALL_MODELS`** in `backend/app/models/__init__.py` is what creates its indexes — a model missing from that list silently has none.

---

## File Structure

**Backend — created:**

| File | Responsibility |
|---|---|
| `backend/app/models/ai.py` | `AiProvider` and `AiRouting` documents, `TaskSlot` enum, `ProviderKind`, `FailureReason` |
| `backend/app/services/ai/__init__.py` | Package facade: re-exports `complete`, `resolve`, `TaskSlot` |
| `backend/app/services/ai/presets.py` | Static preset table. Pure data |
| `backend/app/services/ai/crypto.py` | Key file management, `encrypt` / `decrypt` |
| `backend/app/services/ai/redaction.py` | Scrub key values from log lines and error bodies |
| `backend/app/services/ai/adapters.py` | `OpenAICompatAdapter`, `AnthropicAdapter`, `Completion`, `Failure` |
| `backend/app/services/ai/provider_service.py` | CRUD over `ai_providers` + `fetch_models` |
| `backend/app/services/ai/router.py` | `resolve(slot) -> ResolvedProvider \| None` |
| `backend/app/services/ai/complete.py` | `complete(provider, ...)` — picks an adapter, records failures |
| `backend/app/api/v1/settings.py` | The 8 settings endpoints |
| `frontend/src/components/SettingsView.tsx` | `/settings` route, master-detail shell |
| `frontend/src/components/ProviderList.tsx` | Left rail: providers + "Task routing" entry |
| `frontend/src/components/ProviderForm.tsx` | Right pane: one provider's editable form |
| `frontend/src/components/ModelCombo.tsx` | Dropdown from `models_cache`, free text allowed |
| `frontend/src/components/TaskRoutingPanel.tsx` | Default row + one row per slot |

**Backend — modified:**

| File | Change |
|---|---|
| `backend/pyproject.toml` | Add `cryptography` dependency |
| `backend/app/models/__init__.py` | Register `AiProvider`, `AiRouting` in `ALL_MODELS` |
| `backend/app/config.py:156-175` | Remove `llm_base_url`, `llm_model`; keep the rest; add `ai_legacy_base_url` for migration |
| `backend/app/api/v1/__init__.py` | Mount `settings.router` |
| `backend/app/api/v1/pipelines.py:160-189` | `/summary/status` reads the routed provider |
| `backend/app/queue/summary_handlers.py:55-78` | Resolve `FILE_SUMMARY`, record failure reason |
| `backend/app/services/organism_service.py:90-110` | Resolve `ORGANISM_BLURB` |
| `backend/app/main.py` | Call the migration on startup |
| `backend/tests/api/test_route_owner_scoping.py:502` | Add the settings routes to the unscoped assertion |

**Backend — deleted:** `backend/app/services/llm_client.py` (Task 16), and `backend/tests/services/test_llm_client.py` (superseded by adapter tests).

**Frontend — modified:** `frontend/src/App.tsx` (route), `frontend/src/components/Header.tsx` (nav link), `frontend/src/api/client.ts` + `types.ts` (8 calls + types), `frontend/src/styles.css` (settings classes).

---

## Task 1: Add the `cryptography` dependency

**Files:**
- Modify: `backend/pyproject.toml:6-22`

- [ ] **Step 1: Add the dependency**

In `backend/pyproject.toml`, add to the `dependencies` list after `"pysam>=0.23",`:

```toml
    # Fernet, for API keys at rest. See services/ai/crypto.py -- the threat
    # model is a curious look at the Mongo collection, not shell access.
    "cryptography>=44.0",
```

- [ ] **Step 2: Rebuild the image so the dependency is installed**

```bash
docker compose -p biopipe-verify build api
```

Expected: build succeeds. (Using an explicit `-p` name because bare `docker compose` from a worktree is blocked by a `PreToolUse` hook — see CLAUDE.md.)

- [ ] **Step 3: Verify it imports**

```bash
docker run --rm biopipe-verify-api python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key()[:8])"
```

Expected: 8 bytes of base64 printed, no `ModuleNotFoundError`.

- [ ] **Step 4: Commit**

```bash
git add backend/pyproject.toml
git commit -m "Add cryptography dependency for API key encryption"
```

---

## Task 2: The models

**Files:**
- Create: `backend/app/models/ai.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/models/test_ai_models.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/models/test_ai_models.py`:

```python
"""Shape of the AI provider and routing documents."""

import pytest

from app.models.ai import AiProvider, AiRouting, FailureReason, ProviderKind, TaskSlot


class TestTaskSlot:
    def test_slots_have_human_labels(self):
        """The settings page renders one row per slot and must not hardcode
        the labels -- they come from here via the routing endpoint."""
        assert TaskSlot.FILE_SUMMARY.label == "File summaries"
        assert TaskSlot.ORGANISM_BLURB.label == "Organism blurbs"

    def test_every_slot_has_a_label(self):
        for slot in TaskSlot:
            assert slot.label
            assert slot.label != slot.value


class TestAiProvider:
    async def test_defaults(self, beanie_models):
        p = AiProvider(name="Local", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1234")
        assert p.status == "untested"
        assert p.models_cache == []
        assert p.api_key_enc is None
        assert p.key_hint is None
        assert p.model == ""

    async def test_round_trips(self, beanie_models):
        p = AiProvider(
            name="Anthropic",
            kind=ProviderKind.ANTHROPIC,
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-6",
        )
        await p.insert()
        found = await AiProvider.get(p.id)
        assert found is not None
        assert found.kind == ProviderKind.ANTHROPIC


class TestAiRouting:
    async def test_singleton_id_is_fixed(self, beanie_models):
        """A fixed id is what makes this a singleton -- two concurrent
        writers upsert the same document rather than creating a second one
        that silently shadows the first."""
        r = await AiRouting.load()
        r2 = await AiRouting.load()
        assert r.id == r2.id == AiRouting.SINGLETON_ID

    async def test_empty_slots_mean_use_default(self, beanie_models):
        r = await AiRouting.load()
        assert r.slots == {}
        assert r.default is None


class TestFailureReason:
    def test_values(self):
        assert FailureReason.INVALID_KEY == "invalid_key"
        assert FailureReason.RATE_LIMITED == "rate_limited"
        assert FailureReason.MODEL_NOT_FOUND == "model_not_found"
        assert FailureReason.UNREACHABLE == "unreachable"
        assert FailureReason.BAD_RESPONSE == "bad_response"
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/models/test_ai_models.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.ai'`

- [ ] **Step 3: Write the models**

Create `backend/app/models/ai.py`:

```python
"""Configured AI providers, and which task each one serves.

Two documents. `AiProvider` is one configured endpoint -- a base URL, an
optional encrypted key, and a chosen model. `AiRouting` is a singleton mapping
named task slots onto providers, with a default for everything unassigned.

Slots are an enum rather than free-form strings so the settings page can
enumerate them: a routing UI that cannot list what exists until the app has run
cannot be rendered. Adding an AI feature means adding a member here, and a row
appears in the UI.
"""

from datetime import datetime
from enum import StrEnum

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models.base import TimestampedDocument, utcnow


class ProviderKind(StrEnum):
    """Which adapter speaks to this provider.

    Only two, because only two wire formats exist among the providers this
    supports: Anthropic's `/v1/messages`, and everyone else's OpenAI-compatible
    `/v1/chat/completions`. A "provider" beyond that is a base URL and a label.
    """

    OPENAI_COMPAT = "openai_compat"
    ANTHROPIC = "anthropic"


class FailureReason(StrEnum):
    """Why a call did not produce text.

    Deliberately coarse: a 401 from Anthropic and a 401 from DeepSeek mean the
    same thing to the person who has to fix it.
    """

    INVALID_KEY = "invalid_key"
    RATE_LIMITED = "rate_limited"
    MODEL_NOT_FOUND = "model_not_found"
    UNREACHABLE = "unreachable"
    BAD_RESPONSE = "bad_response"


class TaskSlot(StrEnum):
    """An AI-using feature that can be pointed at a provider.

    The `label` is what the settings page shows. It lives here rather than in
    the frontend so that adding a slot is a one-place change.
    """

    FILE_SUMMARY = "file_summary"
    ORGANISM_BLURB = "organism_blurb"

    @property
    def label(self) -> str:
        return _SLOT_LABELS[self]


_SLOT_LABELS = {
    TaskSlot.FILE_SUMMARY: "File summaries",
    TaskSlot.ORGANISM_BLURB: "Organism blurbs",
}


class AiProvider(TimestampedDocument):
    """One configured endpoint.

    `api_key_enc` is Fernet ciphertext and never leaves the backend decrypted;
    `key_hint` is the masked form every read path shows. The hint is stored
    rather than derived because deriving it would mean decrypting on every list
    request, and listing is the common case.
    """

    name: str
    kind: ProviderKind
    base_url: str
    api_key_enc: bytes | None = None
    key_hint: str | None = None
    model: str = ""
    # Last successful /v1/models fetch. Kept across a failed fetch: a listing
    # endpoint having a bad day should not empty the model dropdown.
    models_cache: list[str] = Field(default_factory=list)
    status: str = "untested"  # ok | failed | untested
    status_reason: FailureReason | None = None
    checked_at: datetime | None = None

    def mark_ok(self) -> None:
        self.status = "ok"
        self.status_reason = None
        self.checked_at = utcnow()
        self.touch()

    def mark_failed(self, reason: FailureReason) -> None:
        self.status = "failed"
        self.status_reason = reason
        self.checked_at = utcnow()
        self.touch()

    class Settings:
        name = "ai_providers"
        indexes = [
            IndexModel([("name", ASCENDING)], name="uniq_name", unique=True),
        ]


class AiRouting(Document):
    """Which provider serves which slot. Exactly one document.

    Not a TimestampedDocument: it carries no `owner`, deliberately. There is one
    machine and one set of providers here, matching the reasoning that leaves
    `/pipelines/summary/status` unscoped -- a profile header should not change
    which model writes a summary.

    A slot absent from `slots` means "use the default". That is a real state,
    not a null needing cleanup, so the UI's "Use default" option writes a
    deletion rather than a value.
    """

    SINGLETON_ID = "ai_routing"

    id: str = Field(default=SINGLETON_ID)
    default: str | None = None  # str(AiProvider.id)
    slots: dict[str, str] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utcnow)

    @classmethod
    async def load(cls) -> "AiRouting":
        """The routing document, creating it on first read.

        Upsert-on-read rather than a migration: there is exactly one, its empty
        state is meaningful, and a missing one is indistinguishable from a fresh
        install.
        """
        found = await cls.get(cls.SINGLETON_ID)
        if found is not None:
            return found
        doc = cls()
        await doc.insert()
        return doc

    def provider_for(self, slot: TaskSlot) -> str | None:
        """The provider id serving `slot`, falling back to the default."""
        return self.slots.get(slot.value) or self.default

    class Settings:
        name = "ai_routing"
```

- [ ] **Step 4: Register the models**

In `backend/app/models/__init__.py`, add the import after the `blob` import:

```python
from app.models.ai import AiProvider, AiRouting, FailureReason, ProviderKind, TaskSlot
```

Add to `ALL_MODELS` (order does not matter):

```python
    AiProvider,
    AiRouting,
```

Add to `__all__`, keeping it alphabetical:

```python
    "AiProvider",
    "AiRouting",
    "FailureReason",
    "ProviderKind",
    "TaskSlot",
```

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/models/test_ai_models.py -q
```

Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/ai.py backend/app/models/__init__.py backend/tests/models/test_ai_models.py
git commit -m "Add AiProvider and AiRouting models"
```

---

## Task 3: Key encryption

**Files:**
- Create: `backend/app/services/ai/__init__.py`, `backend/app/services/ai/crypto.py`
- Test: `backend/tests/services/ai/test_crypto.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/ai/__init__.py` (empty file) and `backend/tests/services/ai/test_crypto.py`:

```python
"""The key file and the round trip.

No `beanie_models` here: this touches the filesystem and nothing else.
"""

import stat

import pytest

from app.services.ai import crypto


@pytest.fixture
def key_dir(tmp_path, monkeypatch):
    """Point crypto at a throwaway BIOINFO_HOME."""
    monkeypatch.setattr(crypto.settings, "bioinfo_home", tmp_path)
    crypto._fernet.cache_clear()
    yield tmp_path
    crypto._fernet.cache_clear()


class TestKeyFile:
    def test_creates_the_key_file_on_first_use(self, key_dir):
        crypto.encrypt("sk-test")
        assert crypto.key_path().exists()

    def test_key_file_is_owner_read_write_only(self, key_dir):
        crypto.encrypt("sk-test")
        mode = stat.S_IMODE(crypto.key_path().stat().st_mode)
        assert mode == 0o600

    def test_reuses_an_existing_key(self, key_dir):
        """Regenerating would silently make every stored key undecryptable."""
        token = crypto.encrypt("sk-test")
        first = crypto.key_path().read_bytes()
        crypto._fernet.cache_clear()
        assert crypto.decrypt(token) == "sk-test"
        assert crypto.key_path().read_bytes() == first


class TestRoundTrip:
    def test_round_trips(self, key_dir):
        assert crypto.decrypt(crypto.encrypt("sk-ant-secret")) == "sk-ant-secret"

    def test_ciphertext_does_not_contain_the_plaintext(self, key_dir):
        """The whole point: a look at the Mongo collection shows nothing."""
        assert b"secret" not in crypto.encrypt("sk-ant-secret")

    def test_decrypt_returns_none_on_garbage(self, key_dir):
        """A key encrypted under a lost key file must not crash the settings
        page -- it reads as a provider whose key needs re-entering."""
        assert crypto.decrypt(b"not-a-fernet-token") is None


class TestHint:
    def test_hint_masks_all_but_the_last_four(self):
        assert crypto.hint("sk-ant-api03-abcdefgh4f2a") == "sk-ant-…4f2a"

    def test_short_keys_are_fully_masked(self):
        """Never leak a short key by showing most of it."""
        assert crypto.hint("abc") == "…"

    def test_hint_of_empty_is_none(self):
        assert crypto.hint("") is None
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_crypto.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ai'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/ai/__init__.py`:

```python
"""AI providers: adapters, routing, and the settings behind both.

Replaces the single-server `llm_client` module. The invariant that module
established still holds and is the reason this package exists in the shape it
does: **an AI call never raises into a job.** It returns a `Completion` or a
`Failure`, and the caller carries on either way.

What changed is that a failure now leaves a trace (see `complete.py`). The old
contract was written for one local server that is free to call and often simply
off, where silence costs nothing. Once keys and money are involved, an expired
key that silently stops producing summaries is a configuration problem the user
cannot see.
"""
```

Create `backend/app/services/ai/crypto.py`:

```python
"""API keys at rest.

Fernet, with the key in a file next to the database rather than in an
environment variable -- an env var means the key sits in `.env`, in the compose
config, and in `docker inspect` output.

**The honest scope of this:** the key file is on the same disk as the Mongo
data, so anyone with shell access to this machine has both and can decrypt
everything. What it defends against is a look at the collection -- an opened
Compass window, a stray `mongodump` in a backup. That is the threat this tool
has, and the settings page says so rather than implying more.
"""

from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)


def key_path() -> Path:
    """Where the encryption key lives.

    Under `.biopipe/`, which already holds the mount sentinel and the lock
    file, so this needs no new directory and follows a relocated BIOINFO_HOME.
    """
    return settings.bioinfo_home / ".biopipe" / "secret.key"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """The cipher, generating the key file on first use.

    Cached because reading the file per call would be per-request filesystem IO
    for a value that cannot change while the process runs. Tests clear it.
    """
    path = key_path()
    if path.exists():
        return Fernet(path.read_bytes())

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Written 0600 from the start rather than chmod-ed after: a world-readable
    # window, however short, is the kind of thing that survives into a backup.
    path.touch(mode=0o600, exist_ok=True)
    path.write_bytes(key)
    log.info("ai_key_file_created", path=str(path))
    return Fernet(key)


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt(token: bytes) -> str | None:
    """The plaintext key, or None if it cannot be decrypted.

    None rather than an exception because the realistic cause is a key file
    that was deleted or replaced, and the useful response to that is a settings
    page saying the key needs re-entering -- not a 500 on every page load.
    """
    try:
        return _fernet().decrypt(token).decode()
    except (InvalidToken, ValueError):
        log.warning("ai_key_undecryptable")
        return None


def hint(plaintext: str) -> str | None:
    """The masked form shown in the UI: prefix, ellipsis, last four.

    Short strings are masked entirely. A key short enough that "all but the
    last four" would show most of it is a key this must not partially print.
    """
    if not plaintext:
        return None
    if len(plaintext) < 12:
        return "…"
    prefix = plaintext[:7] if plaintext.startswith("sk-") else plaintext[:3]
    return f"{prefix}…{plaintext[-4:]}"
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_crypto.py -q
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai/ backend/tests/services/ai/
git commit -m "Add Fernet encryption for AI provider keys"
```

---

## Task 4: The preset table

**Files:**
- Create: `backend/app/services/ai/presets.py`
- Test: `backend/tests/services/ai/test_presets.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/ai/test_presets.py`:

```python
"""The preset table is pure data, and these tests keep it honest."""

from app.models.ai import ProviderKind
from app.services.ai import presets


class TestPresets:
    def test_every_named_provider_is_present(self):
        """The list the settings page offers. Missing one means a user who
        wants it has to know its base URL by heart."""
        ids = {p.id for p in presets.ALL}
        assert ids == {
            "openai",
            "anthropic",
            "deepseek",
            "qwen",
            "moonshot",
            "zhipu",
            "openrouter",
            "local",
        }

    def test_only_anthropic_uses_the_anthropic_adapter(self):
        """Everything else speaks the OpenAI wire format -- that fact is what
        makes two adapters enough."""
        anthropic = [p.id for p in presets.ALL if p.kind == ProviderKind.ANTHROPIC]
        assert anthropic == ["anthropic"]

    def test_hosted_presets_carry_an_https_base_url(self):
        for p in presets.ALL:
            if p.id == "local":
                continue
            assert p.base_url.startswith("https://"), p.id

    def test_local_needs_no_key(self):
        """LM Studio, Ollama and vLLM accept anything or nothing; requiring a
        key would make the common local setup unconfigurable."""
        local = presets.by_id("local")
        assert local is not None
        assert local.needs_key is False

    def test_hosted_presets_need_a_key(self):
        for p in presets.ALL:
            if p.id != "local":
                assert p.needs_key is True, p.id

    def test_by_id_returns_none_for_unknown(self):
        assert presets.by_id("nope") is None
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_presets.py -q
```

Expected: FAIL — `ImportError: cannot import name 'presets'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/ai/presets.py`:

```python
"""Known providers, as base URLs and labels.

Pure data on purpose. Every entry here is served by one of the two adapters, so
adding a provider next year is one line rather than a class -- which is the
whole reason the adapter split is by wire format rather than by vendor.

Base URLs are the OpenAI-compatible root, without the trailing `/v1`: adapters
append their own paths.
"""

from dataclasses import dataclass

from app.models.ai import ProviderKind


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    kind: ProviderKind
    base_url: str
    needs_key: bool


ALL: list[Preset] = [
    Preset("openai", "OpenAI", ProviderKind.OPENAI_COMPAT, "https://api.openai.com", True),
    Preset("anthropic", "Anthropic", ProviderKind.ANTHROPIC, "https://api.anthropic.com", True),
    Preset("deepseek", "DeepSeek", ProviderKind.OPENAI_COMPAT, "https://api.deepseek.com", True),
    # DashScope's OpenAI-compatible endpoint, not the native DashScope API.
    # The international host; mainland accounts use .aliyuncs.com without the
    # `-intl`, which is why this field stays editable after the preset is picked.
    Preset(
        "qwen",
        "Qwen (DashScope)",
        ProviderKind.OPENAI_COMPAT,
        "https://dashscope-intl.aliyuncs.com/compatible-mode",
        True,
    ),
    Preset("moonshot", "Moonshot (Kimi)", ProviderKind.OPENAI_COMPAT, "https://api.moonshot.ai", True),
    Preset("zhipu", "Zhipu (GLM)", ProviderKind.OPENAI_COMPAT, "https://open.bigmodel.cn/api/paas", True),
    Preset("openrouter", "OpenRouter", ProviderKind.OPENAI_COMPAT, "https://openrouter.ai/api", True),
    # The default this feature started as. Not a plugin system: a base URL the
    # user edits, covering LM Studio, Ollama, vLLM and anything else local.
    Preset(
        "local",
        "Local / custom",
        ProviderKind.OPENAI_COMPAT,
        "http://host.docker.internal:11234",
        False,
    ),
]


def by_id(preset_id: str) -> Preset | None:
    return next((p for p in ALL if p.id == preset_id), None)
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_presets.py -q
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai/presets.py backend/tests/services/ai/test_presets.py
git commit -m "Add AI provider preset table"
```

---

## Task 5: Redaction

**Files:**
- Create: `backend/app/services/ai/redaction.py`
- Test: `backend/tests/services/ai/test_redaction.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/ai/test_redaction.py`:

```python
"""Keys must not reach the log or a stored error body.

Some providers echo part of the key back in a 401, and those bodies are now
persisted on the provider document -- so this is not hypothetical.
"""

from app.services.ai import redaction


class TestScrub:
    def test_removes_the_key(self):
        out = redaction.scrub("Bearer sk-ant-secret123 rejected", "sk-ant-secret123")
        assert "sk-ant-secret123" not in out
        assert "[redacted]" in out

    def test_leaves_other_text_intact(self):
        out = redaction.scrub("invalid x-api-key header", "sk-ant-secret123")
        assert out == "invalid x-api-key header"

    def test_handles_no_key(self):
        assert redaction.scrub("some error", None) == "some error"

    def test_ignores_an_empty_key(self):
        """An empty needle would otherwise match everywhere and blank the text."""
        assert redaction.scrub("some error", "") == "some error"

    def test_removes_every_occurrence(self):
        out = redaction.scrub("sk-abc123456789 then sk-abc123456789", "sk-abc123456789")
        assert "sk-abc" not in out

    def test_truncates_long_bodies(self):
        """Upstream error bodies can be an HTML error page; storing it whole
        buys nothing and makes the provider document unreadable."""
        assert len(redaction.scrub("x" * 5000, None)) <= 500
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_redaction.py -q
```

Expected: FAIL — `ImportError: cannot import name 'redaction'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/ai/redaction.py`:

```python
"""Keep API keys out of logs and stored error bodies.

Substring removal rather than a pattern for key-shaped strings: we know the
exact secret at the call site, and matching `sk-[A-Za-z0-9]+` would both miss
providers that do not use that prefix and mangle innocent text that happens to
look like one.
"""

MAX_BODY_CHARS = 500

REDACTED = "[redacted]"


def scrub(text: str, key: str | None) -> str:
    """`text` with `key` removed and the result truncated for storage."""
    if key:
        text = text.replace(key, REDACTED)
    return text[:MAX_BODY_CHARS]
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_redaction.py -q
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai/redaction.py backend/tests/services/ai/test_redaction.py
git commit -m "Add key redaction for AI logs and error bodies"
```

---

## Task 6: The OpenAI-compatible adapter

**Files:**
- Create: `backend/app/services/ai/adapters.py`
- Test: `backend/tests/services/ai/test_openai_adapter.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/ai/test_openai_adapter.py`:

```python
"""The OpenAI-compatible adapter, against a stubbed urlopen.

No network and no database: this is a request-builder and a response-parser,
and both are worth testing in isolation from either.
"""

import json
import urllib.error

import pytest

from app.models.ai import FailureReason
from app.services.ai import adapters
from app.services.ai.adapters import Completion, Failure, OpenAICompatAdapter


class _Response:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code: int, body: str = "{}"):
    def raise_it(*a, **k):
        raise urllib.error.HTTPError("http://x", code, "err", {}, None)

    return raise_it


@pytest.fixture
def adapter():
    return OpenAICompatAdapter(base_url="http://model:1234", api_key="sk-test123456")


CHAT_OK = {"choices": [{"message": {"content": "The reads look usable."}}]}


class TestComplete:
    def test_returns_the_text_and_model(self, adapter, monkeypatch):
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(CHAT_OK)
        )
        result = adapter.complete(system="s", user="u", model="m", max_tokens=100)
        assert isinstance(result, Completion)
        assert result.text == "The reads look usable."
        assert result.model == "m"

    def test_sends_a_bearer_header(self, adapter, monkeypatch):
        seen = {}

        def capture(request, *a, **k):
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data)
            return _Response(CHAT_OK)

        monkeypatch.setattr(adapters.urllib.request, "urlopen", capture)
        adapter.complete(system="s", user="u", model="m", max_tokens=100)
        assert seen["auth"] == "Bearer sk-test123456"

    def test_sends_system_as_a_message(self, adapter, monkeypatch):
        """The OpenAI shape. Contrast with the Anthropic adapter, where the
        system prompt is a top-level field."""
        seen = {}

        def capture(request, *a, **k):
            seen["body"] = json.loads(request.data)
            return _Response(CHAT_OK)

        monkeypatch.setattr(adapters.urllib.request, "urlopen", capture)
        adapter.complete(system="be brief", user="hello", model="m", max_tokens=100)
        assert seen["body"]["messages"] == [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hello"},
        ]

    def test_omits_the_auth_header_when_keyless(self, monkeypatch):
        """A local server given `Authorization: Bearer None` can 400."""
        seen = {}

        def capture(request, *a, **k):
            seen["auth"] = request.get_header("Authorization")
            return _Response(CHAT_OK)

        monkeypatch.setattr(adapters.urllib.request, "urlopen", capture)
        OpenAICompatAdapter(base_url="http://m:1", api_key=None).complete(
            system="s", user="u", model="m", max_tokens=10
        )
        assert seen["auth"] is None

    @pytest.mark.parametrize(
        "code,reason",
        [
            (401, FailureReason.INVALID_KEY),
            (403, FailureReason.INVALID_KEY),
            (429, FailureReason.RATE_LIMITED),
            (404, FailureReason.MODEL_NOT_FOUND),
            (500, FailureReason.UNREACHABLE),
        ],
    )
    def test_maps_http_status_to_a_reason(self, adapter, monkeypatch, code, reason):
        monkeypatch.setattr(adapters.urllib.request, "urlopen", _http_error(code))
        result = adapter.complete(system="s", user="u", model="m", max_tokens=10)
        assert isinstance(result, Failure)
        assert result.reason == reason

    def test_connection_refused_is_unreachable(self, adapter, monkeypatch):
        def refuse(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(adapters.urllib.request, "urlopen", refuse)
        result = adapter.complete(system="s", user="u", model="m", max_tokens=10)
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.UNREACHABLE

    def test_unparseable_200_is_bad_response(self, adapter, monkeypatch):
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response({"weird": 1})
        )
        result = adapter.complete(system="s", user="u", model="m", max_tokens=10)
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.BAD_RESPONSE

    def test_empty_text_is_bad_response(self, adapter, monkeypatch):
        payload = {"choices": [{"message": {"content": "   "}}]}
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        result = adapter.complete(system="s", user="u", model="m", max_tokens=10)
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.BAD_RESPONSE

    def test_the_key_is_scrubbed_from_the_error_detail(self, adapter, monkeypatch):
        """Providers echo the key back. The detail is stored, so this matters."""

        def raise_with_key(*a, **k):
            raise urllib.error.HTTPError(
                "http://x", 401, "err", {}, _BodyIO(b"bad key sk-test123456")
            )

        monkeypatch.setattr(adapters.urllib.request, "urlopen", raise_with_key)
        result = adapter.complete(system="s", user="u", model="m", max_tokens=10)
        assert "sk-test123456" not in (result.detail or "")


class _BodyIO:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


class TestListModels:
    def test_returns_sorted_ids(self, adapter, monkeypatch):
        payload = {"data": [{"id": "zeta"}, {"id": "alpha"}]}
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        assert adapter.list_models() == ["alpha", "zeta"]

    def test_puts_loaded_models_first(self, adapter, monkeypatch):
        """LM Studio reports which model is resident. Asking for one it would
        have to load from disk turns a few-second call into a slow one, so a
        resident model is the better default -- opportunistic, never required."""
        payload = {"data": [{"id": "alpha"}, {"id": "zeta", "loaded": True}]}
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )
        assert adapter.list_models() == ["zeta", "alpha"]

    def test_maps_401_to_invalid_key(self, adapter, monkeypatch):
        monkeypatch.setattr(adapters.urllib.request, "urlopen", _http_error(401))
        result = adapter.list_models()
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.INVALID_KEY

    def test_empty_list_is_not_a_failure(self, adapter, monkeypatch):
        """A reachable server with no models loaded is configured correctly and
        merely empty -- the key is valid, which is what the fetch proves."""
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response({"data": []})
        )
        assert adapter.list_models() == []
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_openai_adapter.py -q
```

Expected: FAIL — `ImportError: cannot import name 'adapters'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/ai/adapters.py`:

```python
"""Two adapters, because there are two wire formats.

Every provider this supports except Anthropic serves the OpenAI-compatible
`POST /v1/chat/completions` with a Bearer token. Anthropic differs in four
ways -- `/v1/messages`, `x-api-key` rather than `Authorization`, a required
`anthropic-version` header, and the system prompt as a top-level field rather
than a message -- and `AnthropicAdapter` exists to absorb exactly those.

Stdlib `urllib` rather than httpx, carried over from the `llm_client` module
this replaces: httpx is a dev-only dependency here, these are simple JSON POSTs,
and the worker calls them from a thread, so an async client buys nothing.

**Neither adapter raises.** Every path returns `Completion`, `Failure`, or a
list. See `app/services/ai/__init__.py` for why that invariant is load-bearing.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.config import settings
from app.logging import get_logger
from app.models.ai import FailureReason
from app.services.ai import redaction

log = get_logger(__name__)

# Anthropic pins its wire format by date. Sending a version it does not know is
# a 400, so this is bumped deliberately rather than tracking "latest".
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class Completion:
    text: str
    model: str


@dataclass(frozen=True)
class Failure:
    reason: FailureReason
    # The upstream body, scrubbed and truncated. Stored on the provider, so it
    # must never contain the key.
    detail: str | None = None


def _reason_for_status(code: int) -> FailureReason:
    """Map an HTTP status onto the coarse vocabulary the UI shows.

    5xx lands on UNREACHABLE rather than a status of its own: to the person
    reading the settings page, "their server is broken" and "I cannot reach
    their server" call for the same response, which is to wait.
    """
    if code in (401, 403):
        return FailureReason.INVALID_KEY
    if code == 429:
        return FailureReason.RATE_LIMITED
    if code == 404:
        return FailureReason.MODEL_NOT_FOUND
    return FailureReason.UNREACHABLE


class _BaseAdapter:
    """Shared request plumbing. Subclasses supply paths, headers, and shapes."""

    def __init__(self, *, base_url: str, api_key: str | None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(
        self, path: str, *, body: dict | None, timeout: float
    ) -> dict | Failure:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers=self._headers(),
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()
            except Exception:  # noqa: BLE001 - diagnostics must not raise
                pass
            detail = redaction.scrub(detail, self.api_key)
            log.warning("ai_http_error", url=url, status=e.code, detail=detail)
            return Failure(_reason_for_status(e.code), detail or None)
        except Exception as e:  # noqa: BLE001 - down is a normal state
            detail = redaction.scrub(str(e), self.api_key)
            log.info("ai_unreachable", url=url, error=detail)
            return Failure(FailureReason.UNREACHABLE, detail)

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError


class OpenAICompatAdapter(_BaseAdapter):
    """OpenAI, DeepSeek, Qwen, Moonshot, Zhipu, OpenRouter, and local servers."""

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # Omitted rather than sent empty: a local server handed
        # `Authorization: Bearer ` can reject the request outright.
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def complete(
        self, *, system: str, user: str, model: str, max_tokens: int
    ) -> Completion | Failure:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Low but not zero, carried over from llm_client: these summaries
            # restate measured numbers, so invention is the failure mode to
            # suppress, while a little variation keeps a re-run from being
            # pointlessly identical.
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
        }
        result = self._request(
            "/v1/chat/completions", body=body, timeout=settings.llm_timeout_seconds
        )
        if isinstance(result, Failure):
            return result

        try:
            text = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            log.warning("ai_response_unparseable", keys=sorted(result) if result else None)
            return Failure(FailureReason.BAD_RESPONSE)

        if not isinstance(text, str) or not text.strip():
            return Failure(FailureReason.BAD_RESPONSE)

        return Completion(text.strip(), model)

    def list_models(self) -> list[str] | Failure:
        result = self._request(
            "/v1/models", body=None, timeout=settings.llm_health_timeout_seconds
        )
        if isinstance(result, Failure):
            return result

        entries = result.get("data") or []
        ids = [str(e["id"]) for e in entries if e.get("id")]
        loaded = {str(e["id"]) for e in entries if e.get("loaded") and e.get("id")}
        # Resident models first, then alphabetical. LM Studio is the only server
        # that reports `loaded`; everywhere else this is a plain sort.
        return sorted(ids, key=lambda i: (i not in loaded, i))


class AnthropicAdapter(_BaseAdapter):
    """Anthropic's Messages API."""

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def complete(
        self, *, system: str, user: str, model: str, max_tokens: int
    ) -> Completion | Failure:
        body = {
            "model": model,
            # Top-level, not a message with role "system". This is the single
            # biggest shape difference from the OpenAI format.
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        result = self._request(
            "/v1/messages", body=body, timeout=settings.llm_timeout_seconds
        )
        if isinstance(result, Failure):
            return result

        try:
            text = result["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            log.warning("ai_response_unparseable", keys=sorted(result) if result else None)
            return Failure(FailureReason.BAD_RESPONSE)

        if not isinstance(text, str) or not text.strip():
            return Failure(FailureReason.BAD_RESPONSE)

        return Completion(text.strip(), model)

    def list_models(self) -> list[str] | Failure:
        result = self._request(
            "/v1/models", body=None, timeout=settings.llm_health_timeout_seconds
        )
        if isinstance(result, Failure):
            return result
        entries = result.get("data") or []
        return sorted(str(e["id"]) for e in entries if e.get("id"))


def adapter_for(kind: str, *, base_url: str, api_key: str | None) -> _BaseAdapter:
    from app.models.ai import ProviderKind

    if kind == ProviderKind.ANTHROPIC:
        return AnthropicAdapter(base_url=base_url, api_key=api_key)
    return OpenAICompatAdapter(base_url=base_url, api_key=api_key)
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_openai_adapter.py -q
```

Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai/adapters.py backend/tests/services/ai/test_openai_adapter.py
git commit -m "Add OpenAI-compatible AI adapter"
```

---

## Task 7: The Anthropic adapter tests

The implementation landed in Task 6; this task proves the four shape differences are right.

**Files:**
- Test: `backend/tests/services/ai/test_anthropic_adapter.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/ai/test_anthropic_adapter.py`:

```python
"""Anthropic differs from the OpenAI format in exactly four ways. Each gets a
test, because each is a silent 400 if wrong.
"""

import json

import pytest

from app.models.ai import FailureReason, ProviderKind
from app.services.ai import adapters
from app.services.ai.adapters import (
    ANTHROPIC_VERSION,
    AnthropicAdapter,
    Completion,
    Failure,
    OpenAICompatAdapter,
    adapter_for,
)


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


MESSAGES_OK = {"content": [{"type": "text", "text": "Escherichia coli is a bacterium."}]}


@pytest.fixture
def adapter():
    return AnthropicAdapter(base_url="https://api.anthropic.com", api_key="sk-ant-secret99")


@pytest.fixture
def capture(monkeypatch):
    seen = {}

    def _capture(request, *a, **k):
        seen["url"] = request.full_url
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["body"] = json.loads(request.data) if request.data else None
        return _Response(MESSAGES_OK)

    monkeypatch.setattr(adapters.urllib.request, "urlopen", _capture)
    return seen


class TestWireFormat:
    def test_posts_to_v1_messages(self, adapter, capture):
        adapter.complete(system="s", user="u", model="claude-x", max_tokens=100)
        assert capture["url"] == "https://api.anthropic.com/v1/messages"

    def test_uses_x_api_key_not_authorization(self, adapter, capture):
        adapter.complete(system="s", user="u", model="claude-x", max_tokens=100)
        assert capture["headers"]["x-api-key"] == "sk-ant-secret99"
        assert "authorization" not in capture["headers"]

    def test_sends_the_version_header(self, adapter, capture):
        adapter.complete(system="s", user="u", model="claude-x", max_tokens=100)
        assert capture["headers"]["anthropic-version"] == ANTHROPIC_VERSION

    def test_system_is_top_level_not_a_message(self, adapter, capture):
        adapter.complete(system="be brief", user="hello", model="claude-x", max_tokens=100)
        assert capture["body"]["system"] == "be brief"
        assert capture["body"]["messages"] == [{"role": "user", "content": "hello"}]


class TestComplete:
    def test_parses_the_content_block(self, adapter, capture):
        result = adapter.complete(system="s", user="u", model="claude-x", max_tokens=100)
        assert isinstance(result, Completion)
        assert result.text == "Escherichia coli is a bacterium."
        assert result.model == "claude-x"

    def test_unparseable_body_is_bad_response(self, adapter, monkeypatch):
        monkeypatch.setattr(
            adapters.urllib.request, "urlopen", lambda *a, **k: _Response({"nope": 1})
        )
        result = adapter.complete(system="s", user="u", model="claude-x", max_tokens=10)
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.BAD_RESPONSE


class TestAdapterFor:
    def test_anthropic_kind_gets_the_anthropic_adapter(self):
        a = adapter_for(ProviderKind.ANTHROPIC, base_url="https://x", api_key="k")
        assert isinstance(a, AnthropicAdapter)

    def test_everything_else_gets_openai_compat(self):
        a = adapter_for(ProviderKind.OPENAI_COMPAT, base_url="https://x", api_key="k")
        assert isinstance(a, OpenAICompatAdapter)
```

- [ ] **Step 2: Run the tests**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_anthropic_adapter.py -q
```

Expected: 8 passed. (The implementation exists from Task 6; if any fail, fix `adapters.py`.)

- [ ] **Step 3: Commit**

```bash
git add backend/tests/services/ai/test_anthropic_adapter.py
git commit -m "Test the Anthropic adapter's wire format"
```

---

## Task 8: Provider CRUD

**Files:**
- Create: `backend/app/services/ai/provider_service.py`
- Test: `backend/tests/services/ai/test_provider_service.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/ai/test_provider_service.py`:

```python
"""Provider CRUD. The key-preservation test is the one that matters most --
its failure silently destroys a credential.
"""

import pytest

from app.models.ai import AiProvider, AiRouting, FailureReason, ProviderKind, TaskSlot
from app.services.ai import crypto, provider_service


@pytest.fixture(autouse=True)
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto.settings, "bioinfo_home", tmp_path)
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest.fixture(autouse=True)
async def clean(beanie_models):
    await AiProvider.find_all().delete()
    await AiRouting.find_all().delete()


class TestCreate:
    async def test_stores_the_key_encrypted(self):
        p = await provider_service.create(
            name="Anthropic",
            kind=ProviderKind.ANTHROPIC,
            base_url="https://api.anthropic.com",
            model="claude-x",
            api_key="sk-ant-secret12345",
        )
        assert p.api_key_enc is not None
        assert b"secret" not in p.api_key_enc

    async def test_stores_a_hint(self):
        p = await provider_service.create(
            name="A", kind=ProviderKind.ANTHROPIC, base_url="https://x",
            model="m", api_key="sk-ant-secret12345",
        )
        assert p.key_hint == "sk-ant-…2345"

    async def test_keyless_provider_has_no_hint(self):
        p = await provider_service.create(
            name="Local", kind=ProviderKind.OPENAI_COMPAT,
            base_url="http://x:1", model="m", api_key=None,
        )
        assert p.api_key_enc is None
        assert p.key_hint is None


class TestUpdate:
    async def test_omitted_key_preserves_the_stored_one(self):
        """The single most important behaviour here. The UI submits the form
        without an api_key unless the user typed a new one, so if this were
        wrong, editing the model name would wipe the credential."""
        p = await provider_service.create(
            name="A", kind=ProviderKind.ANTHROPIC, base_url="https://x",
            model="m", api_key="sk-ant-original123",
        )
        before = p.api_key_enc

        updated = await provider_service.update(str(p.id), {"model": "m2"})

        assert updated.api_key_enc == before
        assert updated.key_hint == "sk-ant-…l123"
        assert crypto.decrypt(updated.api_key_enc) == "sk-ant-original123"

    async def test_explicit_none_clears_the_key(self):
        p = await provider_service.create(
            name="A", kind=ProviderKind.ANTHROPIC, base_url="https://x",
            model="m", api_key="sk-ant-original123",
        )
        updated = await provider_service.update(str(p.id), {"api_key": None})
        assert updated.api_key_enc is None
        assert updated.key_hint is None

    async def test_a_new_key_replaces_the_old(self):
        p = await provider_service.create(
            name="A", kind=ProviderKind.ANTHROPIC, base_url="https://x",
            model="m", api_key="sk-ant-original123",
        )
        updated = await provider_service.update(str(p.id), {"api_key": "sk-ant-replaced99"})
        assert crypto.decrypt(updated.api_key_enc) == "sk-ant-replaced99"

    async def test_unknown_id_returns_none(self):
        from bson import ObjectId

        assert await provider_service.update(str(ObjectId()), {"model": "m"}) is None


class TestDelete:
    async def test_clears_slots_routed_to_the_deleted_provider(self):
        """Refusing the delete would mean an error telling the user to go undo
        three things first. Clearing to default is the kinder equivalent."""
        p = await provider_service.create(
            name="A", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1",
            model="m", api_key=None,
        )
        routing = await AiRouting.load()
        routing.slots[TaskSlot.FILE_SUMMARY.value] = str(p.id)
        await routing.save()

        await provider_service.delete(str(p.id))

        after = await AiRouting.load()
        assert TaskSlot.FILE_SUMMARY.value not in after.slots

    async def test_clears_the_default_when_it_is_deleted(self):
        p = await provider_service.create(
            name="A", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1",
            model="m", api_key=None,
        )
        routing = await AiRouting.load()
        routing.default = str(p.id)
        await routing.save()

        await provider_service.delete(str(p.id))

        assert (await AiRouting.load()).default is None

    async def test_leaves_other_slots_alone(self):
        keep = await provider_service.create(
            name="Keep", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1",
            model="m", api_key=None,
        )
        drop = await provider_service.create(
            name="Drop", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:2",
            model="m", api_key=None,
        )
        routing = await AiRouting.load()
        routing.slots = {
            TaskSlot.FILE_SUMMARY.value: str(keep.id),
            TaskSlot.ORGANISM_BLURB.value: str(drop.id),
        }
        await routing.save()

        await provider_service.delete(str(drop.id))

        after = await AiRouting.load()
        assert after.slots == {TaskSlot.FILE_SUMMARY.value: str(keep.id)}


class TestFetchModels:
    async def test_success_caches_the_list_and_marks_ok(self, monkeypatch):
        p = await provider_service.create(
            name="A", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1",
            model="", api_key=None,
        )
        monkeypatch.setattr(
            provider_service, "_list_models", lambda prov: ["alpha", "zeta"]
        )
        models = await provider_service.fetch_models(str(p.id))

        assert models == ["alpha", "zeta"]
        refreshed = await AiProvider.get(p.id)
        assert refreshed.models_cache == ["alpha", "zeta"]
        assert refreshed.status == "ok"
        assert refreshed.checked_at is not None

    async def test_failure_keeps_the_previous_cache(self, monkeypatch):
        """A listing endpoint having a bad day must not empty the dropdown."""
        from app.services.ai.adapters import Failure

        p = await provider_service.create(
            name="A", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1",
            model="", api_key=None,
        )
        p.models_cache = ["previously-fetched"]
        await p.save()

        monkeypatch.setattr(
            provider_service,
            "_list_models",
            lambda prov: Failure(FailureReason.INVALID_KEY, "nope"),
        )
        result = await provider_service.fetch_models(str(p.id))

        assert isinstance(result, Failure)
        refreshed = await AiProvider.get(p.id)
        assert refreshed.models_cache == ["previously-fetched"]
        assert refreshed.status == "failed"
        assert refreshed.status_reason == FailureReason.INVALID_KEY
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_provider_service.py -q
```

Expected: FAIL — `ImportError: cannot import name 'provider_service'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/ai/provider_service.py`:

```python
"""CRUD over configured providers, and the one action that tests them.

`fetch_models` is deliberately both the connection test and the model
discovery: hitting `/v1/models` proves the base URL resolves, proves the key is
accepted, and returns the dropdown's contents in one round trip. Two separate
concepts would mean two requests proving the same thing.
"""

import asyncio

from bson import ObjectId
from bson.errors import InvalidId

from app.logging import get_logger
from app.models.ai import AiProvider, AiRouting, FailureReason
from app.services.ai import crypto
from app.services.ai.adapters import Failure, adapter_for

log = get_logger(__name__)


def _oid(provider_id: str) -> ObjectId | None:
    try:
        return ObjectId(provider_id)
    except (InvalidId, TypeError):
        return None


async def list_all() -> list[AiProvider]:
    return await AiProvider.find_all().sort("+name").to_list()


async def get(provider_id: str) -> AiProvider | None:
    oid = _oid(provider_id)
    return await AiProvider.get(oid) if oid else None


async def create(
    *, name: str, kind: str, base_url: str, model: str, api_key: str | None
) -> AiProvider:
    provider = AiProvider(name=name, kind=kind, base_url=base_url, model=model)
    if api_key:
        provider.api_key_enc = crypto.encrypt(api_key)
        provider.key_hint = crypto.hint(api_key)
    await provider.insert()
    log.info("ai_provider_created", name=name, kind=str(kind))
    return provider


async def update(provider_id: str, changes: dict) -> AiProvider | None:
    """Apply `changes`. The `api_key` key has three-way semantics.

    Absent from `changes` preserves the stored key; present-and-None clears it;
    present-and-a-string replaces it. This is what lets the UI render a
    write-only key field: the form submits without `api_key` unless the user
    typed one, so editing the model cannot wipe the credential.
    """
    provider = await get(provider_id)
    if provider is None:
        return None

    if "api_key" in changes:
        api_key = changes.pop("api_key")
        if api_key:
            provider.api_key_enc = crypto.encrypt(api_key)
            provider.key_hint = crypto.hint(api_key)
        else:
            provider.api_key_enc = None
            provider.key_hint = None

    for field in ("name", "kind", "base_url", "model"):
        if field in changes:
            setattr(provider, field, changes[field])

    provider.touch()
    await provider.save()
    return provider


async def delete(provider_id: str) -> bool:
    """Delete, clearing any routing that pointed here.

    Clearing rather than refusing: a delete blocked by "three slots use this"
    makes the user go undo three things first, and the slots fall back to the
    default perfectly well on their own.
    """
    provider = await get(provider_id)
    if provider is None:
        return False

    routing = await AiRouting.load()
    dirty = False
    if routing.default == provider_id:
        routing.default = None
        dirty = True
    for slot, assigned in list(routing.slots.items()):
        if assigned == provider_id:
            del routing.slots[slot]
            dirty = True
    if dirty:
        await routing.save()

    await provider.delete()
    log.info("ai_provider_deleted", name=provider.name)
    return True


def _list_models(provider: AiProvider) -> list[str] | Failure:
    """The blocking call, factored out so tests can replace it.

    Separate from `fetch_models` because that function's job -- persisting the
    result -- is what the tests are about, and stubbing a socket to test a
    database write is the wrong seam.
    """
    key = crypto.decrypt(provider.api_key_enc) if provider.api_key_enc else None
    adapter = adapter_for(provider.kind, base_url=provider.base_url, api_key=key)
    return adapter.list_models()


async def fetch_models(provider_id: str) -> list[str] | Failure | None:
    """Fetch and cache the model list. Returns None if the provider is gone.

    Doubles as the connection test: on success the provider is marked ok, on
    failure it carries the reason, and either way `checked_at` moves.
    """
    provider = await get(provider_id)
    if provider is None:
        return None

    # Off the event loop: urllib blocks, and an unreachable host is slow rather
    # than instant.
    result = await asyncio.to_thread(_list_models, provider)

    if isinstance(result, Failure):
        provider.mark_failed(result.reason)
        # models_cache deliberately untouched -- a bad day at the listing
        # endpoint should not empty the user's model dropdown.
        await provider.save()
        return result

    provider.models_cache = result
    provider.mark_ok()
    await provider.save()
    return result


async def record_failure(provider_id: str, reason: FailureReason) -> None:
    """Mark a provider failed from a real job, not a manual test.

    This is what makes the settings badge reflect usage: a key that expired
    between fetches shows as failed the next time a summary is attempted.
    """
    provider = await get(provider_id)
    if provider is None:
        return
    provider.mark_failed(reason)
    await provider.save()


async def record_success(provider_id: str) -> None:
    provider = await get(provider_id)
    if provider is None:
        return
    provider.mark_ok()
    await provider.save()
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_provider_service.py -q
```

Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai/provider_service.py backend/tests/services/ai/test_provider_service.py
git commit -m "Add AI provider CRUD and model fetching"
```

---

## Task 9: The router

**Files:**
- Create: `backend/app/services/ai/router.py`
- Test: `backend/tests/services/ai/test_router.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/ai/test_router.py`:

```python
"""Slot resolution: override wins, then default, then nothing."""

import pytest

from app.models.ai import AiProvider, AiRouting, ProviderKind, TaskSlot
from app.services.ai import crypto, provider_service, router


@pytest.fixture(autouse=True)
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto.settings, "bioinfo_home", tmp_path)
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest.fixture(autouse=True)
async def clean(beanie_models):
    await AiProvider.find_all().delete()
    await AiRouting.find_all().delete()


async def _provider(name: str, key: str | None = None) -> AiProvider:
    return await provider_service.create(
        name=name, kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1", model="m", api_key=key,
    )


class TestResolve:
    async def test_a_slot_override_wins_over_the_default(self):
        default = await _provider("Default")
        special = await _provider("Special")
        routing = await AiRouting.load()
        routing.default = str(default.id)
        routing.slots = {TaskSlot.ORGANISM_BLURB.value: str(special.id)}
        await routing.save()

        resolved = await router.resolve(TaskSlot.ORGANISM_BLURB)
        assert resolved.name == "Special"

    async def test_an_unassigned_slot_falls_back_to_the_default(self):
        default = await _provider("Default")
        routing = await AiRouting.load()
        routing.default = str(default.id)
        await routing.save()

        resolved = await router.resolve(TaskSlot.FILE_SUMMARY)
        assert resolved.name == "Default"

    async def test_no_default_and_no_override_resolves_to_none(self):
        """A fresh install with nothing configured. Callers treat None as the
        same non-event as a model server being off."""
        assert await router.resolve(TaskSlot.FILE_SUMMARY) is None

    async def test_a_dangling_slot_id_resolves_to_none(self):
        """Deleting clears routing, so this should not happen -- but a hand-
        edited database should degrade to 'nothing configured', not a 500."""
        from bson import ObjectId

        routing = await AiRouting.load()
        routing.slots = {TaskSlot.FILE_SUMMARY.value: str(ObjectId())}
        await routing.save()
        assert await router.resolve(TaskSlot.FILE_SUMMARY) is None

    async def test_decrypts_the_key(self):
        p = await _provider("Keyed", key="sk-secret123456")
        routing = await AiRouting.load()
        routing.default = str(p.id)
        await routing.save()

        resolved = await router.resolve(TaskSlot.FILE_SUMMARY)
        assert resolved.api_key == "sk-secret123456"

    async def test_keyless_provider_resolves_with_no_key(self):
        p = await _provider("Local")
        routing = await AiRouting.load()
        routing.default = str(p.id)
        await routing.save()

        resolved = await router.resolve(TaskSlot.FILE_SUMMARY)
        assert resolved.api_key is None

    async def test_carries_the_provider_id_for_failure_recording(self):
        """complete() writes the failure reason back onto the provider, so the
        resolved value has to know which one it came from."""
        p = await _provider("Local")
        routing = await AiRouting.load()
        routing.default = str(p.id)
        await routing.save()

        resolved = await router.resolve(TaskSlot.FILE_SUMMARY)
        assert resolved.provider_id == str(p.id)

    async def test_a_provider_with_no_model_still_resolves(self):
        """The model can come from the cache's first entry at call time; an
        unset model is a nudge to configure, not a hard stop."""
        p = await provider_service.create(
            name="NoModel", kind=ProviderKind.OPENAI_COMPAT,
            base_url="http://x:1", model="", api_key=None,
        )
        routing = await AiRouting.load()
        routing.default = str(p.id)
        await routing.save()

        resolved = await router.resolve(TaskSlot.FILE_SUMMARY)
        assert resolved is not None
        assert resolved.model == ""
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_router.py -q
```

Expected: FAIL — `ImportError: cannot import name 'router'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/ai/router.py`:

```python
"""Which provider serves a given task.

The one function call sites use. Everything upstream of a `ResolvedProvider` --
the routing document, the fallback to default, the key decryption -- happens
here, so a caller needs to know only its own slot.
"""

from dataclasses import dataclass

from app.logging import get_logger
from app.models.ai import AiRouting, TaskSlot
from app.services.ai import crypto, provider_service

log = get_logger(__name__)


@dataclass(frozen=True)
class ResolvedProvider:
    """A provider with its key decrypted, ready to build an adapter from.

    `provider_id` rides along so a failure can be recorded back onto the
    document it came from -- that write is what makes the settings badge
    reflect real usage rather than only the last manual test.
    """

    provider_id: str
    name: str
    kind: str
    base_url: str
    api_key: str | None
    model: str
    models_cache: list[str]


async def resolve(slot: TaskSlot) -> ResolvedProvider | None:
    """The provider serving `slot`, or None if nothing is configured.

    None is a normal state, not an error: a fresh install has no providers, and
    every caller treats that the same way it treated a model server being off.
    """
    routing = await AiRouting.load()
    provider_id = routing.provider_for(slot)
    if not provider_id:
        return None

    provider = await provider_service.get(provider_id)
    if provider is None:
        # Deleting clears routing, so this means a hand-edited database.
        log.warning("ai_routing_dangling", slot=slot.value, provider_id=provider_id)
        return None

    key = crypto.decrypt(provider.api_key_enc) if provider.api_key_enc else None
    return ResolvedProvider(
        provider_id=str(provider.id),
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        api_key=key,
        model=provider.model,
        models_cache=list(provider.models_cache),
    )
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_router.py -q
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai/router.py backend/tests/services/ai/test_router.py
git commit -m "Add AI task-slot router"
```

---

## Task 10: `complete()` with failure recording

**Files:**
- Create: `backend/app/services/ai/complete.py`
- Modify: `backend/app/services/ai/__init__.py`
- Test: `backend/tests/services/ai/test_complete.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/ai/test_complete.py`:

```python
"""The call itself: pick an adapter, run it, record what happened.

This is where the "never raise" invariant meets the new "leave a trace"
behaviour, so both are asserted here.
"""

import pytest

from app.models.ai import AiProvider, FailureReason, ProviderKind
from app.services.ai import complete as complete_mod
from app.services.ai import crypto, provider_service
from app.services.ai.adapters import Completion, Failure
from app.services.ai.router import ResolvedProvider


@pytest.fixture(autouse=True)
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto.settings, "bioinfo_home", tmp_path)
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest.fixture(autouse=True)
async def clean(beanie_models):
    await AiProvider.find_all().delete()


async def _resolved(model: str = "m", cache: list[str] | None = None) -> ResolvedProvider:
    p = await provider_service.create(
        name="P", kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1", model=model, api_key=None,
    )
    if cache:
        p.models_cache = cache
        await p.save()
    return ResolvedProvider(
        provider_id=str(p.id), name="P", kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1", api_key=None, model=model,
        models_cache=cache or [],
    )


class TestComplete:
    async def test_returns_the_completion(self, monkeypatch):
        provider = await _resolved()
        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **kw: Completion("text", "m")
        )
        result = await complete_mod.complete(provider, system="s", user="u")
        assert isinstance(result, Completion)
        assert result.text == "text"

    async def test_success_marks_the_provider_ok(self, monkeypatch):
        provider = await _resolved()
        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **kw: Completion("text", "m")
        )
        await complete_mod.complete(provider, system="s", user="u")
        stored = await provider_service.get(provider.provider_id)
        assert stored.status == "ok"

    async def test_failure_records_the_reason_on_the_provider(self, monkeypatch):
        """The badge has to go red from a real job, not only from a manual
        fetch -- a key that expires between tests is the whole reason."""
        provider = await _resolved()
        monkeypatch.setattr(
            complete_mod, "_run", lambda p, **kw: Failure(FailureReason.INVALID_KEY)
        )
        result = await complete_mod.complete(provider, system="s", user="u")

        assert isinstance(result, Failure)
        stored = await provider_service.get(provider.provider_id)
        assert stored.status == "failed"
        assert stored.status_reason == FailureReason.INVALID_KEY

    async def test_never_raises(self, monkeypatch):
        """The invariant the whole package is built around."""

        def explode(p, **kw):
            raise RuntimeError("adapter blew up")

        provider = await _resolved()
        monkeypatch.setattr(complete_mod, "_run", explode)
        result = await complete_mod.complete(provider, system="s", user="u")
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.BAD_RESPONSE

    async def test_falls_back_to_the_first_cached_model(self, monkeypatch):
        """An unset model with a cached list is recoverable -- picking the
        first is better than refusing to run."""
        seen = {}

        def capture(p, **kw):
            seen["model"] = kw["model"]
            return Completion("t", kw["model"])

        provider = await _resolved(model="", cache=["cached-first", "other"])
        monkeypatch.setattr(complete_mod, "_run", capture)
        await complete_mod.complete(provider, system="s", user="u")
        assert seen["model"] == "cached-first"

    async def test_no_model_and_no_cache_is_model_not_found(self, monkeypatch):
        provider = await _resolved(model="", cache=[])
        result = await complete_mod.complete(provider, system="s", user="u")
        assert isinstance(result, Failure)
        assert result.reason == FailureReason.MODEL_NOT_FOUND

    async def test_max_tokens_defaults_to_the_setting(self, monkeypatch):
        from app.config import settings

        seen = {}

        def capture(p, **kw):
            seen["max_tokens"] = kw["max_tokens"]
            return Completion("t", "m")

        provider = await _resolved()
        monkeypatch.setattr(complete_mod, "_run", capture)
        await complete_mod.complete(provider, system="s", user="u")
        assert seen["max_tokens"] == settings.llm_max_tokens

    async def test_max_tokens_can_be_overridden(self, monkeypatch):
        seen = {}

        def capture(p, **kw):
            seen["max_tokens"] = kw["max_tokens"]
            return Completion("t", "m")

        provider = await _resolved()
        monkeypatch.setattr(complete_mod, "_run", capture)
        await complete_mod.complete(provider, system="s", user="u", max_tokens=250)
        assert seen["max_tokens"] == 250
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_complete.py -q
```

Expected: FAIL — `ImportError: cannot import name 'complete'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/ai/complete.py`:

```python
"""One chat completion against a resolved provider.

Where the package's two rules meet: **never raise**, and **leave a trace**.
The first is inherited from the `llm_client` module this replaces -- summaries
are additive, so a failure must not fail a job. The second is new, because a
hosted provider fails for reasons the user can fix (an expired key, an
exhausted quota) and silence hides them.
"""

import asyncio

from app.config import settings
from app.logging import get_logger
from app.models.ai import FailureReason
from app.services.ai import provider_service
from app.services.ai.adapters import Completion, Failure, adapter_for
from app.services.ai.router import ResolvedProvider

log = get_logger(__name__)


def _run(provider: ResolvedProvider, **kwargs) -> Completion | Failure:
    """The blocking adapter call. Its own function so tests have a seam that
    is not a socket."""
    adapter = adapter_for(
        provider.kind, base_url=provider.base_url, api_key=provider.api_key
    )
    return adapter.complete(**kwargs)


def _model_for(provider: ResolvedProvider) -> str | None:
    """The model to send.

    Falls back to the first cached model when none is pinned: a provider added
    and fetched but never given an explicit model is one click from working,
    and picking for the user beats refusing.
    """
    if provider.model:
        return provider.model
    return provider.models_cache[0] if provider.models_cache else None


async def complete(
    provider: ResolvedProvider,
    *,
    system: str,
    user: str,
    max_tokens: int | None = None,
) -> Completion | Failure:
    """Run one completion, recording the outcome on the provider document."""
    model = _model_for(provider)
    if model is None:
        log.info("ai_no_model", provider=provider.name)
        await provider_service.record_failure(
            provider.provider_id, FailureReason.MODEL_NOT_FOUND
        )
        return Failure(FailureReason.MODEL_NOT_FOUND)

    try:
        result = await asyncio.to_thread(
            _run,
            provider,
            system=system,
            user=user,
            model=model,
            max_tokens=max_tokens or settings.llm_max_tokens,
        )
    except Exception as e:  # noqa: BLE001 - the invariant: never raise into a job
        log.warning("ai_call_crashed", provider=provider.name, error=str(e))
        result = Failure(FailureReason.BAD_RESPONSE, str(e)[:500])

    if isinstance(result, Failure):
        await provider_service.record_failure(provider.provider_id, result.reason)
        return result

    await provider_service.record_success(provider.provider_id)
    return result


def complete_sync(
    provider: ResolvedProvider,
    *,
    system: str,
    user: str,
    max_tokens: int | None = None,
) -> Completion | Failure:
    """Blocking variant for thread handlers, which cannot await.

    Queue handlers run in a worker thread with no event loop, so they cannot
    reach the async version. They get no failure recording for the same reason
    -- the write needs the loop -- and the handler returns the reason in its
    result payload instead, where `results.py` persists it.
    """
    model = _model_for(provider)
    if model is None:
        return Failure(FailureReason.MODEL_NOT_FOUND)
    try:
        return _run(
            provider,
            system=system,
            user=user,
            model=model,
            max_tokens=max_tokens or settings.llm_max_tokens,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("ai_call_crashed", provider=provider.name, error=str(e))
        return Failure(FailureReason.BAD_RESPONSE, str(e)[:500])
```

- [ ] **Step 4: Export the facade**

Append to `backend/app/services/ai/__init__.py`:

```python

from app.models.ai import FailureReason, ProviderKind, TaskSlot
from app.services.ai.adapters import Completion, Failure
from app.services.ai.complete import complete, complete_sync
from app.services.ai.router import ResolvedProvider, resolve

__all__ = [
    "Completion",
    "Failure",
    "FailureReason",
    "ProviderKind",
    "ResolvedProvider",
    "TaskSlot",
    "complete",
    "complete_sync",
    "resolve",
]
```

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_complete.py -q
```

Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/ai/complete.py backend/app/services/ai/__init__.py backend/tests/services/ai/test_complete.py
git commit -m "Add AI complete() with failure recording"
```

---

## Task 11: Config changes and legacy migration

**Files:**
- Modify: `backend/app/config.py:156-176`
- Create: `backend/app/services/ai/migration.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/services/ai/test_migration.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/ai/test_migration.py`:

```python
"""Seeding a provider from the pre-settings environment variables.

Without this, the first run after this ships silently stops producing summaries
on an installation that was working -- the base URL moved from config into a
document that does not exist yet.
"""

import pytest

from app.models.ai import AiProvider, AiRouting, ProviderKind
from app.services.ai import crypto, migration, provider_service


@pytest.fixture(autouse=True)
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto.settings, "bioinfo_home", tmp_path)
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest.fixture(autouse=True)
async def clean(beanie_models):
    await AiProvider.find_all().delete()
    await AiRouting.find_all().delete()


class TestSeedLegacyProvider:
    async def test_creates_a_provider_from_the_legacy_url(self, monkeypatch):
        monkeypatch.setattr(
            migration.settings, "ai_legacy_base_url", "http://host.docker.internal:11234"
        )
        await migration.seed_legacy_provider()

        providers = await provider_service.list_all()
        assert len(providers) == 1
        assert providers[0].base_url == "http://host.docker.internal:11234"
        assert providers[0].kind == ProviderKind.OPENAI_COMPAT
        assert providers[0].api_key_enc is None

    async def test_points_the_default_at_it(self, monkeypatch):
        """Seeding a provider nothing routes to would leave the install just as
        broken as seeding nothing."""
        monkeypatch.setattr(migration.settings, "ai_legacy_base_url", "http://x:1234")
        await migration.seed_legacy_provider()

        routing = await AiRouting.load()
        providers = await provider_service.list_all()
        assert routing.default == str(providers[0].id)

    async def test_carries_the_legacy_model_when_pinned(self, monkeypatch):
        monkeypatch.setattr(migration.settings, "ai_legacy_base_url", "http://x:1234")
        monkeypatch.setattr(migration.settings, "ai_legacy_model", "pinned-model")
        await migration.seed_legacy_provider()

        assert (await provider_service.list_all())[0].model == "pinned-model"

    async def test_does_nothing_when_providers_already_exist(self, monkeypatch):
        """Idempotent: this runs on every startup, and a second provider named
        'Local' would collide on the unique index anyway."""
        await provider_service.create(
            name="Existing", kind=ProviderKind.OPENAI_COMPAT,
            base_url="http://y:1", model="m", api_key=None,
        )
        monkeypatch.setattr(migration.settings, "ai_legacy_base_url", "http://x:1234")

        await migration.seed_legacy_provider()

        providers = await provider_service.list_all()
        assert len(providers) == 1
        assert providers[0].name == "Existing"

    async def test_does_nothing_when_no_legacy_url_is_set(self, monkeypatch):
        """A fresh install with no history gets an empty settings page, not a
        provider pointing at a port nothing is listening on."""
        monkeypatch.setattr(migration.settings, "ai_legacy_base_url", "")
        await migration.seed_legacy_provider()
        assert await provider_service.list_all() == []
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_migration.py -q
```

Expected: FAIL — `ImportError: cannot import name 'migration'`

- [ ] **Step 3: Update the config**

In `backend/app/config.py`, replace lines 156-175 (the `--- Local narrative summaries ---` block) with:

```python
    # --- AI summaries ---
    # The master switch. Off means no AI feature runs, whatever is configured
    # in the settings page.
    llm_summaries_enabled: bool = True
    # Generous: a small local model on CPU is not fast, and the alternative to
    # waiting is a summary that never appears.
    llm_timeout_seconds: float = 120.0
    # The model-list probe decides *whether* to bother, so it must be quick --
    # a slow "is it up" check would cost more than it saves.
    llm_health_timeout_seconds: float = 3.0
    # A few sentences, with headroom. Small models overshoot a length request,
    # and a summary cut off mid-sentence reads worse than a slightly long one.
    llm_max_tokens: int = 400

    # Read once, at first startup after providers moved into the database, to
    # seed a provider from however this installation was already configured.
    # Nothing else reads these -- the base URL and model now live on the
    # provider document. See services/ai/migration.py.
    ai_legacy_base_url: str = Field(default="", alias="LLM_BASE_URL")
    ai_legacy_model: str = Field(default="", alias="LLM_MODEL")
```

Note: `SettingsConfigDict` already sets `extra="ignore"`, and `Field(alias=...)` keeps the existing `LLM_BASE_URL` env var working for the migration. Add `populate_by_name=True` to `model_config` on line 40 so the field is also settable by its Python name in tests:

```python
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)
```

- [ ] **Step 4: Write the migration**

Create `backend/app/services/ai/migration.py`:

```python
"""Carry the pre-settings configuration into the database, once.

Before this feature the model server was `LLM_BASE_URL` in the environment.
After it, providers are documents. An installation that was working must keep
working without anyone opening the settings page -- so on the first startup
where no providers exist, the old environment values become one.

Runs on every startup and does nothing after the first: the collection is empty
exactly once.
"""

from app.config import settings
from app.logging import get_logger
from app.models.ai import AiRouting, ProviderKind
from app.services.ai import provider_service

log = get_logger(__name__)


async def seed_legacy_provider() -> None:
    if not settings.ai_legacy_base_url:
        # A fresh install. An empty settings page is the honest state; a
        # seeded provider pointing at a port nothing is listening on is not.
        return

    existing = await provider_service.list_all()
    if existing:
        return

    provider = await provider_service.create(
        name="Local",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url=settings.ai_legacy_base_url,
        model=settings.ai_legacy_model,
        api_key=None,
    )
    routing = await AiRouting.load()
    routing.default = str(provider.id)
    await routing.save()
    log.info("ai_legacy_provider_seeded", base_url=settings.ai_legacy_base_url)
```

- [ ] **Step 5: Call it on startup**

In `backend/app/main.py`, find the lifespan/startup function that calls `connect_to_mongo()` and add immediately after it:

```python
    from app.services.ai.migration import seed_legacy_provider

    await seed_legacy_provider()
```

- [ ] **Step 6: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/services/ai/test_migration.py -q
```

Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add backend/app/config.py backend/app/main.py backend/app/services/ai/migration.py backend/tests/services/ai/test_migration.py
git commit -m "Seed an AI provider from legacy env config on first startup"
```

---

## Task 12: The settings API

**Files:**
- Create: `backend/app/api/v1/settings.py`
- Modify: `backend/app/api/v1/__init__.py`
- Test: `backend/tests/api/test_settings_ai.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_settings_ai.py`:

```python
"""The settings endpoints.

The test that matters most is `TestKeysNeverLeak` -- it asserts the security
property the whole design rests on, across every response shape.
"""

import pytest

from app.models.ai import AiProvider, AiRouting, ProviderKind, TaskSlot
from app.services.ai import crypto, provider_service


@pytest.fixture(autouse=True)
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto.settings, "bioinfo_home", tmp_path)
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest.fixture(autouse=True)
async def clean(beanie_models):
    await AiProvider.find_all().delete()
    await AiRouting.find_all().delete()


SECRET = "sk-ant-supersecret9999"


class TestPresets:
    async def test_lists_presets(self, client):
        resp = await client.get("/api/v1/settings/ai/presets")
        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()}
        assert "anthropic" in ids
        assert "local" in ids


class TestCreate:
    async def test_creates_a_provider(self, client):
        resp = await client.post(
            "/api/v1/settings/ai/providers",
            json={
                "name": "Anthropic",
                "kind": "anthropic",
                "base_url": "https://api.anthropic.com",
                "model": "claude-x",
                "api_key": SECRET,
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["has_key"] is True
        assert resp.json()["key_hint"] == "sk-ant-…9999"

    async def test_rejects_a_duplicate_name(self, client):
        body = {"name": "A", "kind": "openai_compat", "base_url": "http://x:1", "model": "m"}
        assert (await client.post("/api/v1/settings/ai/providers", json=body)).status_code == 201
        assert (await client.post("/api/v1/settings/ai/providers", json=body)).status_code == 409


class TestUpdate:
    async def test_omitting_the_key_preserves_it(self, client):
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "anthropic", "base_url": "https://x",
                  "model": "m", "api_key": SECRET},
        )
        pid = created.json()["id"]

        resp = await client.patch(
            f"/api/v1/settings/ai/providers/{pid}", json={"model": "m2"}
        )
        assert resp.status_code == 200
        assert resp.json()["has_key"] is True

        stored = await provider_service.get(pid)
        assert crypto.decrypt(stored.api_key_enc) == SECRET

    async def test_explicit_null_clears_the_key(self, client):
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "anthropic", "base_url": "https://x",
                  "model": "m", "api_key": SECRET},
        )
        pid = created.json()["id"]
        resp = await client.patch(
            f"/api/v1/settings/ai/providers/{pid}", json={"api_key": None}
        )
        assert resp.json()["has_key"] is False

    async def test_unknown_id_is_404(self, client):
        from bson import ObjectId

        resp = await client.patch(
            f"/api/v1/settings/ai/providers/{ObjectId()}", json={"model": "m"}
        )
        assert resp.status_code == 404


class TestDelete:
    async def test_deletes(self, client):
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "openai_compat", "base_url": "http://x:1", "model": "m"},
        )
        pid = created.json()["id"]
        assert (await client.delete(f"/api/v1/settings/ai/providers/{pid}")).status_code == 204
        assert (await client.get("/api/v1/settings/ai/providers")).json() == []


class TestFetchModels:
    async def test_returns_the_model_list(self, client, monkeypatch):
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "openai_compat", "base_url": "http://x:1", "model": ""},
        )
        pid = created.json()["id"]
        monkeypatch.setattr(provider_service, "_list_models", lambda p: ["m1", "m2"])

        resp = await client.post(f"/api/v1/settings/ai/providers/{pid}/fetch-models")
        assert resp.status_code == 200
        assert resp.json()["models"] == ["m1", "m2"]
        assert resp.json()["status"] == "ok"

    async def test_reports_a_failure_without_erroring(self, client, monkeypatch):
        """A 200 with a failure inside, not a 502: the request succeeded, the
        provider is what failed, and the UI renders that as a badge."""
        from app.models.ai import FailureReason
        from app.services.ai.adapters import Failure

        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "openai_compat", "base_url": "http://x:1", "model": ""},
        )
        pid = created.json()["id"]
        monkeypatch.setattr(
            provider_service,
            "_list_models",
            lambda p: Failure(FailureReason.INVALID_KEY, "bad key"),
        )
        resp = await client.post(f"/api/v1/settings/ai/providers/{pid}/fetch-models")
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"
        assert resp.json()["reason"] == "invalid_key"


class TestRouting:
    async def test_returns_the_slot_catalog(self, client):
        """The UI must not hardcode slot names or labels."""
        resp = await client.get("/api/v1/settings/ai/routing")
        assert resp.status_code == 200
        slots = {s["name"]: s["label"] for s in resp.json()["catalog"]}
        assert slots["file_summary"] == "File summaries"
        assert slots["organism_blurb"] == "Organism blurbs"

    async def test_sets_the_default_and_a_slot(self, client):
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "openai_compat", "base_url": "http://x:1", "model": "m"},
        )
        pid = created.json()["id"]

        resp = await client.put(
            "/api/v1/settings/ai/routing",
            json={"default": pid, "slots": {"organism_blurb": pid}},
        )
        assert resp.status_code == 200
        assert resp.json()["default"] == pid
        assert resp.json()["slots"]["organism_blurb"] == pid

    async def test_rejects_an_unknown_slot_name(self, client):
        resp = await client.put(
            "/api/v1/settings/ai/routing", json={"default": None, "slots": {"nope": "x"}}
        )
        assert resp.status_code == 422

    async def test_rejects_an_unknown_provider_id(self, client):
        from bson import ObjectId

        resp = await client.put(
            "/api/v1/settings/ai/routing", json={"default": str(ObjectId()), "slots": {}}
        )
        assert resp.status_code == 422

    async def test_reports_which_slots_use_each_provider(self, client):
        """The 'Used by' line, which is what stops master-detail hiding the
        routing behind a click."""
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "openai_compat", "base_url": "http://x:1", "model": "m"},
        )
        pid = created.json()["id"]
        await client.put(
            "/api/v1/settings/ai/routing",
            json={"default": None, "slots": {"organism_blurb": pid}},
        )
        listing = (await client.get("/api/v1/settings/ai/providers")).json()
        assert listing[0]["used_by"] == ["Organism blurbs"]


class TestKeysNeverLeak:
    async def test_no_settings_response_contains_a_full_key(self, client, monkeypatch):
        """The security property the design rests on, asserted across every
        response shape rather than only the obvious one."""
        created = await client.post(
            "/api/v1/settings/ai/providers",
            json={"name": "A", "kind": "anthropic", "base_url": "https://x",
                  "model": "m", "api_key": SECRET},
        )
        pid = created.json()["id"]
        monkeypatch.setattr(provider_service, "_list_models", lambda p: ["m1"])

        responses = [
            created,
            await client.get("/api/v1/settings/ai/providers"),
            await client.patch(f"/api/v1/settings/ai/providers/{pid}", json={"model": "m2"}),
            await client.post(f"/api/v1/settings/ai/providers/{pid}/fetch-models"),
            await client.get("/api/v1/settings/ai/routing"),
            await client.get("/api/v1/settings/ai/presets"),
        ]
        for resp in responses:
            assert SECRET not in resp.text, resp.url
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/api/test_settings_ai.py -q
```

Expected: FAIL — 404s, because the router does not exist.

- [ ] **Step 3: Write the router**

Create `backend/app/api/v1/settings.py`:

```python
"""Configuration the user edits: AI providers and task routing.

Deliberately **not owner-scoped**, matching the precedent in `pipelines.py`'s
`/summary/status`: there is one machine and one set of providers here, so a
profile header cannot change the answer, and gating these behind one would hide
the settings page from a client that has not resolved a profile yet.

**No response from this module ever contains an API key.** Keys go in via
`api_key` on create and update; they come back only as `key_hint` and
`has_key`. `tests/api/test_settings_ai.py::TestKeysNeverLeak` asserts it across
every shape here.
"""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.models.ai import AiRouting, ProviderKind, TaskSlot
from app.services.ai import presets as presets_mod
from app.services.ai import provider_service
from app.services.ai.adapters import Failure

router = APIRouter(prefix="/settings", tags=["settings"])


class PresetOut(BaseModel):
    id: str
    label: str
    kind: ProviderKind
    base_url: str
    needs_key: bool


class ProviderOut(BaseModel):
    id: str
    name: str
    kind: ProviderKind
    base_url: str
    model: str
    key_hint: str | None
    has_key: bool
    models_cache: list[str]
    status: str
    status_reason: str | None
    checked_at: str | None
    # Human labels of the slots routed here, so the detail pane can say what
    # depends on this provider without a second request.
    used_by: list[str]


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1)
    kind: ProviderKind
    base_url: str = Field(min_length=1)
    model: str = ""
    api_key: str | None = None


class ProviderUpdate(BaseModel):
    """Every field optional, and `api_key` has three-way semantics.

    Absent preserves the stored key, explicit null clears it, a string replaces
    it. `model_fields_set` is what distinguishes absent from null -- which is
    why this cannot be a plain dict with defaults.
    """

    name: str | None = None
    kind: ProviderKind | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None


class SlotOut(BaseModel):
    name: str
    label: str


class RoutingOut(BaseModel):
    default: str | None
    slots: dict[str, str]
    catalog: list[SlotOut]


class RoutingIn(BaseModel):
    default: str | None = None
    slots: dict[str, str] = Field(default_factory=dict)


class FetchModelsOut(BaseModel):
    status: str
    models: list[str]
    reason: str | None = None
    detail: str | None = None


async def _used_by_map() -> dict[str, list[str]]:
    """provider id -> labels of the slots routed to it."""
    routing = await AiRouting.load()
    out: dict[str, list[str]] = {}
    for slot_name, provider_id in routing.slots.items():
        try:
            label = TaskSlot(slot_name).label
        except ValueError:
            continue  # a slot removed from the enum; ignore rather than 500
        out.setdefault(provider_id, []).append(label)
    if routing.default:
        out.setdefault(routing.default, []).append("Default")
    return out


def _to_out(provider, used_by: list[str]) -> ProviderOut:
    return ProviderOut(
        id=str(provider.id),
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        model=provider.model,
        key_hint=provider.key_hint,
        has_key=provider.api_key_enc is not None,
        models_cache=provider.models_cache,
        status=provider.status,
        status_reason=provider.status_reason,
        checked_at=provider.checked_at.isoformat() if provider.checked_at else None,
        used_by=used_by,
    )


@router.get("/ai/presets", response_model=list[PresetOut])
async def list_presets() -> list[PresetOut]:
    return [
        PresetOut(
            id=p.id, label=p.label, kind=p.kind, base_url=p.base_url, needs_key=p.needs_key
        )
        for p in presets_mod.ALL
    ]


@router.get("/ai/providers", response_model=list[ProviderOut])
async def list_providers() -> list[ProviderOut]:
    used = await _used_by_map()
    return [_to_out(p, used.get(str(p.id), [])) for p in await provider_service.list_all()]


@router.post(
    "/ai/providers", response_model=ProviderOut, status_code=status.HTTP_201_CREATED
)
async def create_provider(body: ProviderCreate) -> ProviderOut:
    from pymongo.errors import DuplicateKeyError

    try:
        provider = await provider_service.create(
            name=body.name,
            kind=body.kind,
            base_url=body.base_url,
            model=body.model,
            api_key=body.api_key,
        )
    except DuplicateKeyError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A provider named {body.name!r} already exists"
        ) from None
    return _to_out(provider, [])


@router.patch("/ai/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(provider_id: str, body: ProviderUpdate) -> ProviderOut:
    # exclude_unset is what preserves an omitted key: without it every field
    # arrives as None and the key is wiped on any edit.
    changes = body.model_dump(exclude_unset=True)
    provider = await provider_service.update(provider_id, changes)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such provider")
    used = await _used_by_map()
    return _to_out(provider, used.get(str(provider.id), []))


@router.delete("/ai/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(provider_id: str) -> None:
    if not await provider_service.delete(provider_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such provider")


@router.post("/ai/providers/{provider_id}/fetch-models", response_model=FetchModelsOut)
async def fetch_models(provider_id: str) -> FetchModelsOut:
    """Fetch the model list, which doubles as the connection test.

    A provider failure is a 200 with `status: failed`, not a 502: the request
    itself succeeded, and the UI renders the outcome as a badge rather than an
    error toast.
    """
    result = await provider_service.fetch_models(provider_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such provider")
    if isinstance(result, Failure):
        provider = await provider_service.get(provider_id)
        return FetchModelsOut(
            status="failed",
            models=provider.models_cache if provider else [],
            reason=result.reason,
            detail=result.detail,
        )
    return FetchModelsOut(status="ok", models=result)


@router.get("/ai/routing", response_model=RoutingOut)
async def get_routing() -> RoutingOut:
    routing = await AiRouting.load()
    return RoutingOut(
        default=routing.default,
        slots=routing.slots,
        catalog=[SlotOut(name=s.value, label=s.label) for s in TaskSlot],
    )


@router.put("/ai/routing", response_model=RoutingOut)
async def set_routing(body: RoutingIn) -> RoutingOut:
    valid_slots = {s.value for s in TaskSlot}
    unknown = set(body.slots) - valid_slots
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown task slots: {sorted(unknown)}"
        )

    # Every referenced provider must exist. Writing a dangling id would give a
    # silently non-functional route that resolve() reports only in the log.
    for provider_id in {*body.slots.values(), *( [body.default] if body.default else [] )}:
        if await provider_service.get(provider_id) is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"No such provider: {provider_id}"
            )

    routing = await AiRouting.load()
    routing.default = body.default
    routing.slots = dict(body.slots)
    await routing.save()
    return RoutingOut(
        default=routing.default,
        slots=routing.slots,
        catalog=[SlotOut(name=s.value, label=s.label) for s in TaskSlot],
    )
```

- [ ] **Step 4: Mount the router**

In `backend/app/api/v1/__init__.py`, add `settings` to the import list (alphabetically, after `search`):

```python
    settings,
```

and add the include after `api_router.include_router(system.router)`:

```python
api_router.include_router(settings.router)
```

- [ ] **Step 5: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/api/test_settings_ai.py -q
```

Expected: 15 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/settings.py backend/app/api/v1/__init__.py backend/tests/api/test_settings_ai.py
git commit -m "Add AI settings API"
```

---

## Task 13: Record the settings routes as unscoped

**Files:**
- Modify: `backend/tests/api/test_route_owner_scoping.py:502-517`

- [ ] **Step 1: Extend the existing assertion**

In `backend/tests/api/test_route_owner_scoping.py`, inside `test_global_pipeline_reads_stay_open`, append after the `aligners/minimap2/schema` assertion:

```python
        # AI settings, unscoped for the same reason as /summary/status above:
        # one machine, one set of providers, and a profile header cannot change
        # which model writes a summary. Scoping them later would break the
        # settings page for a client that has not resolved a profile.
        assert (await client.get("/api/v1/settings/ai/presets")).status_code == 200
        assert (await client.get("/api/v1/settings/ai/providers")).status_code == 200
        assert (await client.get("/api/v1/settings/ai/routing")).status_code == 200
```

- [ ] **Step 2: Run the test**

```bash
./backend/run-worktree-tests.sh tests/api/test_route_owner_scoping.py -q
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/api/test_route_owner_scoping.py
git commit -m "Assert the AI settings routes are deliberately unscoped"
```

---

## Task 14: Rewire the summary handler

**Files:**
- Modify: `backend/app/queue/summary_handlers.py:20,55-90`
- Test: `backend/tests/queue/test_summary_handler.py`

- [ ] **Step 1: Rewrite the existing tests**

Replace the `llm_client` monkeypatches in `backend/tests/queue/test_summary_handler.py`. The handler runs in a worker thread with no event loop, so it resolves its provider synchronously. Replace every `monkeypatch.setattr(llm_client, ...)` with the new seam — change the import at line 14 to:

```python
from app.services import summary_prompt
from app.services.ai.adapters import Completion, Failure
from app.models.ai import FailureReason
from app.queue import summary_handlers
```

and rewrite the patches. The four patterns:

```python
# "server down" becomes "nothing configured"
monkeypatch.setattr(summary_handlers, "_resolve_sync", lambda: None)

# "available but returns nothing"
monkeypatch.setattr(summary_handlers, "_resolve_sync", lambda: _fake_provider())
monkeypatch.setattr(
    summary_handlers, "_complete", lambda p, **kw: Failure(FailureReason.BAD_RESPONSE)
)

# success
monkeypatch.setattr(summary_handlers, "_resolve_sync", lambda: _fake_provider())
monkeypatch.setattr(
    summary_handlers,
    "_complete",
    lambda p, **kw: Completion("The reads look usable.", "test-model"),
)
```

Add this helper near the top of the module:

```python
def _fake_provider():
    """A resolved provider that no test actually calls out to."""
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
```

Then add two new tests to the module:

```python
class TestFailureReasons:
    def test_a_failure_is_reported_in_the_result(self, monkeypatch):
        """The new behaviour: a summary that did not appear says why, instead
        of being a silent no-op."""
        monkeypatch.setattr(summary_handlers, "_resolve_sync", lambda: _fake_provider())
        monkeypatch.setattr(
            summary_handlers,
            "_complete",
            lambda p, **kw: Failure(FailureReason.INVALID_KEY),
        )
        result = summarize_object(_ctx(_payload()))
        assert result["skipped"] == "invalid_key"

    def test_nothing_configured_is_reported_distinctly(self, monkeypatch):
        monkeypatch.setattr(summary_handlers, "_resolve_sync", lambda: None)
        result = summarize_object(_ctx(_payload()))
        assert result["skipped"] == "no_provider"
```

`_ctx()` and `_payload()` already exist at the top of this module (lines 16-33)
— reuse them rather than building a second ctx helper.

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/queue/test_summary_handler.py -q
```

Expected: FAIL — `AttributeError: module 'app.queue.summary_handlers' has no attribute '_resolve_sync'`

- [ ] **Step 3: Rewrite the handler**

In `backend/app/queue/summary_handlers.py`, replace the `llm_client` import on line 20:

```python
from app.services import summary_prompt
from app.services.ai import complete as ai_complete
from app.services.ai.adapters import Completion
```

Add these two seams above the handler function:

```python
def _resolve_sync():
    """Resolve the FILE_SUMMARY provider from a worker thread.

    Thread handlers have no event loop, and `router.resolve` is async because
    it reads Mongo. `asyncio.run` on a fresh loop is the standard escape here
    and is cheap next to the model call that follows.
    """
    import asyncio

    from app.models.ai import TaskSlot
    from app.services.ai import router

    return asyncio.run(router.resolve(TaskSlot.FILE_SUMMARY))


def _complete(provider, **kwargs):
    """The model call. Separate so tests replace it without a socket."""
    return ai_complete.complete_sync(provider, **kwargs)
```

Replace lines 55-58 (the `llm_client.is_available()` block) with:

```python
    provider = _resolve_sync()
    if provider is None:
        log.info("summary_skipped_no_provider", object_id=object_id)
        return {"object_id": object_id, "skipped": "no_provider"}
```

Replace the `completion = llm_client.complete(...)` block (lines ~78-83) with:

```python
    result = _complete(provider, system=summary_prompt.SYSTEM_PROMPT, user=prompt)
    if not isinstance(result, Completion):
        # A typed reason rather than a bare "nothing happened": an expired key
        # is a configuration problem the user can fix, and silence hides it.
        log.info("summary_not_generated", object_id=object_id, reason=result.reason)
        return {"object_id": object_id, "skipped": str(result.reason)}

    text, model = result.text, result.model
```

- [ ] **Step 4: Run to verify it passes**

```bash
./backend/run-worktree-tests.sh tests/queue/test_summary_handler.py -q
```

Expected: all pass, including the two new tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/summary_handlers.py backend/tests/queue/test_summary_handler.py
git commit -m "Route file summaries through the configured provider"
```

---

## Task 15: Rewire organism blurbs and `/summary/status`

**Files:**
- Modify: `backend/app/services/organism_service.py:20,90-110`
- Modify: `backend/app/api/v1/pipelines.py:160-189`
- Test: `backend/tests/services/test_organism_service.py`, `backend/tests/api/test_summary_status.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_summary_status.py`:

```python
"""What /pipelines/summary/status answers now.

It used to mean "is the LM Studio server up?". It now means "is the provider
routed to file summaries usable?" -- and the answer differs by provider kind.
"""

import pytest

from app.models.ai import AiProvider, AiRouting, ProviderKind, TaskSlot
from app.services.ai import crypto, provider_service


@pytest.fixture(autouse=True)
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto.settings, "bioinfo_home", tmp_path)
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest.fixture(autouse=True)
async def clean(beanie_models):
    await AiProvider.find_all().delete()
    await AiRouting.find_all().delete()


async def _route(kind: ProviderKind, base_url: str, status: str = "untested"):
    p = await provider_service.create(
        name="P", kind=kind, base_url=base_url, model="m",
        api_key=None if kind == ProviderKind.OPENAI_COMPAT else "sk-x123456789",
    )
    if status != "untested":
        p.status = status
        await p.save()
    routing = await AiRouting.load()
    routing.default = str(p.id)
    await routing.save()
    return p


class TestSummaryStatus:
    async def test_nothing_configured(self, client):
        resp = await client.get("/api/v1/pipelines/summary/status")
        assert resp.status_code == 200
        assert resp.json() == {"available": False, "reason": "no_provider"}

    async def test_disabled_by_the_master_switch(self, client, monkeypatch):
        from app.api.v1 import pipelines

        await _route(ProviderKind.OPENAI_COMPAT, "http://host.docker.internal:1")
        monkeypatch.setattr(pipelines.settings, "llm_summaries_enabled", False)
        assert (await client.get("/api/v1/pipelines/summary/status")).json() == {
            "available": False,
            "reason": "disabled",
        }

    async def test_a_local_provider_is_probed_live(self, client, monkeypatch):
        """The local server is a process the user starts and stops by hand, so
        a remembered answer is the one most likely to be wrong."""
        from app.api.v1 import pipelines

        await _route(ProviderKind.OPENAI_COMPAT, "http://localhost:11234", status="ok")
        monkeypatch.setattr(pipelines, "_probe_local", lambda p: False)

        body = (await client.get("/api/v1/pipelines/summary/status")).json()
        assert body["available"] is False
        assert body["reason"] == "server_unavailable"

    async def test_a_hosted_provider_reports_stored_status(self, client, monkeypatch):
        """No network call: a hosted provider's failure mode is a bad key, not
        a down server, and that is not worth a round trip on every page load."""
        from app.api.v1 import pipelines

        def must_not_probe(p):
            raise AssertionError("hosted providers must not be probed")

        await _route(ProviderKind.ANTHROPIC, "https://api.anthropic.com", status="ok")
        monkeypatch.setattr(pipelines, "_probe_local", must_not_probe)

        body = (await client.get("/api/v1/pipelines/summary/status")).json()
        assert body["available"] is True
        assert body["provider_name"] == "P"

    async def test_a_failed_hosted_provider_is_unavailable(self, client):
        await _route(ProviderKind.ANTHROPIC, "https://api.anthropic.com", status="failed")
        body = (await client.get("/api/v1/pipelines/summary/status")).json()
        assert body["available"] is False
```

- [ ] **Step 2: Run to verify it fails**

```bash
./backend/run-worktree-tests.sh tests/api/test_summary_status.py -q
```

Expected: FAIL — the endpoint still references `llm_client`.

- [ ] **Step 3: Rewrite the status endpoint**

In `backend/app/api/v1/pipelines.py`, replace the body of the `/summary/status` handler (lines ~174-189) with:

```python
    from app.models.ai import ProviderKind, TaskSlot
    from app.services.ai import router as ai_router

    if not settings.llm_summaries_enabled:
        return {"available": False, "reason": "disabled"}

    provider = await ai_router.resolve(TaskSlot.FILE_SUMMARY)
    if provider is None:
        return {"available": False, "reason": "no_provider"}

    # Local servers are probed live; hosted ones are not. A local model server
    # is a process the user starts and stops by hand, so a remembered answer is
    # the one most likely to be wrong. A hosted provider does not go down --
    # it rejects a stale key -- and that is not worth a network round trip on
    # every page load, nor a billable request.
    if _is_local(provider.base_url):
        alive = await asyncio.to_thread(_probe_local, provider)
        if not alive:
            return {"available": False, "reason": "server_unavailable"}
    elif provider.status == "failed":
        return {
            "available": False,
            "reason": provider_status_reason(provider.provider_id) or "failed",
            "provider_name": provider.name,
        }

    return {
        "available": True,
        "model": provider.model or (provider.models_cache[0] if provider.models_cache else None),
        "provider_name": provider.name,
    }
```

Add these helpers at module level in `pipelines.py`:

```python
def _is_local(base_url: str) -> bool:
    """Whether this base URL points at something on this machine.

    Governs whether availability is probed live or remembered. `host.docker.
    internal` counts: from inside these containers that *is* the host.
    """
    return any(
        h in base_url
        for h in ("localhost", "127.0.0.1", "host.docker.internal", "0.0.0.0")
    )


def _probe_local(provider) -> bool:
    """Cheap liveness check against a local server: can it list models?

    `/v1/models` rather than the LM Studio-specific `/health` the old client
    used -- one fewer non-standard dependency, and it is the same call the
    settings page's fetch makes.
    """
    from app.services.ai.adapters import Failure, adapter_for

    adapter = adapter_for(
        provider.kind, base_url=provider.base_url, api_key=provider.api_key
    )
    return not isinstance(adapter.list_models(), Failure)
```

Replace the `provider_status_reason(...)` call above with a simpler read — the resolved provider does not carry `status_reason`, so return the stored one directly:

```python
    elif provider.status == "failed":
        stored = await provider_service.get(provider.provider_id)
        return {
            "available": False,
            "reason": str(stored.status_reason) if stored and stored.status_reason else "failed",
            "provider_name": provider.name,
        }
```

with `from app.services.ai import provider_service` added to the local imports at the top of the handler.

- [ ] **Step 4: Rewrite organism_service**

In `backend/app/services/organism_service.py`, replace the `llm_client` import on line 20:

```python
from app.services import summary_prompt
from app.services.ai import complete as ai_complete
from app.services.ai import router as ai_router
from app.services.ai.adapters import Completion
```

Replace lines 100-112 (the `is_available` check through `if completion is None`) with:

```python
    from app.models.ai import TaskSlot

    provider = await ai_router.resolve(TaskSlot.ORGANISM_BLURB)
    if provider is None:
        return None

    result = await ai_complete.complete(
        provider,
        system=summary_prompt.ORGANISM_SYSTEM_PROMPT,
        user=summary_prompt.build_organism_prompt(organism.strip()),
        # Shorter than a file summary: this is two or three sentences, and the
        # cap is what stops a chatty model from writing an essay.
        max_tokens=250,
    )
    if not isinstance(result, Completion):
        return None

    text, model = result.text, result.model
```

- [ ] **Step 5: Update the organism service tests**

In `backend/tests/services/test_organism_service.py`, replace any `llm_client` monkeypatches with the equivalent on `ai_router.resolve` / `ai_complete.complete`. Where a test previously patched `llm_client.is_available` to `False`, patch:

```python
monkeypatch.setattr(organism_service.ai_router, "resolve", _async_none)
```

with this helper at module level:

```python
async def _async_none(*a, **k):
    return None
```

Where it patched `llm_client.complete` to return a tuple, patch:

```python
async def _completion(*a, **k):
    from app.services.ai.adapters import Completion

    return Completion("A bacterium.", "test-model")

monkeypatch.setattr(organism_service.ai_complete, "complete", _completion)
```

and resolve returns a provider:

```python
async def _provider(*a, **k):
    from app.models.ai import ProviderKind
    from app.services.ai.router import ResolvedProvider

    return ResolvedProvider(
        provider_id="000000000000000000000000", name="Test",
        kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1",
        api_key=None, model="test-model", models_cache=[],
    )

monkeypatch.setattr(organism_service.ai_router, "resolve", _provider)
```

- [ ] **Step 6: Run both test files**

```bash
./backend/run-worktree-tests.sh tests/api/test_summary_status.py tests/services/test_organism_service.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/v1/pipelines.py backend/app/services/organism_service.py backend/tests/api/test_summary_status.py backend/tests/services/test_organism_service.py
git commit -m "Route organism blurbs and summary status through configured providers"
```

---

## Task 16: Delete the old client and run the full suite

**Files:**
- Delete: `backend/app/services/llm_client.py`, `backend/tests/services/test_llm_client.py`

- [ ] **Step 1: Verify nothing still imports it**

```bash
grep -rn "llm_client\|llm_base_url\|settings.llm_model" backend/app backend/tests --include="*.py" | grep -v __pycache__
```

Expected: no output. Any hit must be fixed before deleting.

- [ ] **Step 2: Delete both files**

```bash
git rm backend/app/services/llm_client.py backend/tests/services/test_llm_client.py
```

- [ ] **Step 3: Run the full suite**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass. **Read the count**, not just the exit code — CLAUDE.md is explicit that "green" means reading the number. Note the count for comparison against the pre-change baseline (~1872 passing).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "Remove the single-server llm_client, superseded by services/ai"
```

---

## Task 17: Frontend API client and types

**Files:**
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts`

- [ ] **Step 1: Add the types**

Append to `frontend/src/api/types.ts`:

```typescript
/** A known provider, offered in the add-provider form. Picking one pre-fills
 *  the base URL; it stays editable afterwards, which is how a mainland
 *  DashScope account or a non-default local port gets configured. */
export interface AiPreset {
  id: string;
  label: string;
  kind: "openai_compat" | "anthropic";
  base_url: string;
  needs_key: boolean;
}

/** A configured provider. Note what is absent: there is no field carrying the
 *  API key. `key_hint` is the masked form and `has_key` is the boolean the
 *  form needs -- the real value never leaves the backend. */
export interface AiProvider {
  id: string;
  name: string;
  kind: "openai_compat" | "anthropic";
  base_url: string;
  model: string;
  key_hint: string | null;
  has_key: boolean;
  models_cache: string[];
  status: "ok" | "failed" | "untested";
  status_reason: string | null;
  checked_at: string | null;
  /** Human labels of the task slots routed here, including "Default". */
  used_by: string[];
}

export interface AiSlot {
  name: string;
  label: string;
}

export interface AiRouting {
  default: string | null;
  /** Only explicitly-overridden slots. An absent slot means "use default". */
  slots: Record<string, string>;
  catalog: AiSlot[];
}

export interface AiFetchModelsResult {
  status: "ok" | "failed";
  models: string[];
  reason: string | null;
  detail: string | null;
}

/** Create and update share a shape, but update omits `api_key` unless the user
 *  typed a new one -- that omission is what preserves the stored key. */
export interface AiProviderInput {
  name?: string;
  kind?: "openai_compat" | "anthropic";
  base_url?: string;
  model?: string;
  api_key?: string | null;
}
```

- [ ] **Step 2: Add the API calls**

In `frontend/src/api/client.ts`, add these imports to the type import block (alphabetically): `AiFetchModelsResult`, `AiPreset`, `AiProvider`, `AiProviderInput`, `AiRouting`.

Add to the `api` object, after `summaryStatus`:

```typescript
  /** The known-provider table. Static; safe to cache indefinitely. */
  aiPresets: () => request<AiPreset[]>("/settings/ai/presets"),

  aiProviders: () => request<AiProvider[]>("/settings/ai/providers"),

  createAiProvider: (body: AiProviderInput) =>
    request<AiProvider>("/settings/ai/providers", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Update a provider. **Omit `api_key` to keep the stored key.**
   *
   * The backend distinguishes an absent field from an explicit null, so a form
   * that always sent `api_key` -- even as an empty string -- would wipe the
   * credential every time the user renamed a provider. Send the field only
   * when the user typed something, and send `null` only to deliberately clear.
   */
  updateAiProvider: (id: string, body: AiProviderInput) =>
    request<AiProvider>(`/settings/ai/providers/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  deleteAiProvider: (id: string) =>
    request<void>(`/settings/ai/providers/${id}`, { method: "DELETE" }),

  /** Fetch the model list, which is also the connection test. A provider
   *  failure comes back as a 200 with `status: "failed"`, not a thrown error --
   *  it renders as a badge, not a toast. */
  fetchAiModels: (id: string) =>
    request<AiFetchModelsResult>(`/settings/ai/providers/${id}/fetch-models`, {
      method: "POST",
    }),

  aiRouting: () => request<AiRouting>("/settings/ai/routing"),

  setAiRouting: (body: { default: string | null; slots: Record<string, string> }) =>
    request<AiRouting>("/settings/ai/routing", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
```

- [ ] **Step 3: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "Add AI settings API client and types"
```

---

## Task 18: The model combo box

**Files:**
- Create: `frontend/src/components/ModelCombo.tsx`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/ModelCombo.tsx`:

```tsx
/**
 * A model id: pick from the fetched list, or type one.
 *
 * Deliberately not a plain `<select>`. Some OpenAI-compatible servers implement
 * `/v1/models` poorly or not at all, OpenRouter returns hundreds of entries,
 * and a model id the user knows is valid must not be blocked by a listing
 * endpoint having a bad day. A datalist gives the dropdown when the list is
 * useful and gets out of the way when it is not.
 */
export function ModelCombo({
  value,
  options,
  onChange,
  id = "model-combo",
}: {
  value: string;
  options: string[];
  onChange: (value: string) => void;
  id?: string;
}) {
  return (
    <>
      <input
        className="settings-input"
        list={`${id}-options`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={options.length ? "Choose or type a model id" : "Type a model id"}
        spellCheck={false}
        autoComplete="off"
      />
      <datalist id={`${id}-options`}>
        {options.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
      {options.length === 0 && (
        <p className="settings-hint">
          No models fetched yet — press Fetch models, or type an id directly.
        </p>
      )}
    </>
  );
}
```

- [ ] **Step 2: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ModelCombo.tsx
git commit -m "Add model combo box"
```

---

## Task 19: The provider form

**Files:**
- Create: `frontend/src/components/ProviderForm.tsx`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/ProviderForm.tsx`:

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { AiProvider } from "../api/types";
import { notify } from "../stores/messageStore";
import { ModelCombo } from "./ModelCombo";

/**
 * One provider's editable detail.
 *
 * The key field is **write-only**: it renders empty regardless of whether a key
 * is stored, and an empty submit omits `api_key` entirely so the backend keeps
 * what it has. That is the whole reason the PATCH endpoint distinguishes an
 * absent field from a null one -- without it, renaming a provider would wipe
 * its credential, silently, with the failure only surfacing hours later when a
 * summary stopped appearing.
 */
export function ProviderForm({ provider }: { provider: AiProvider }) {
  const queryClient = useQueryClient();

  const [name, setName] = useState(provider.name);
  const [baseUrl, setBaseUrl] = useState(provider.base_url);
  const [model, setModel] = useState(provider.model);
  const [apiKey, setApiKey] = useState("");

  // Re-seed when the selected provider changes: the form is one component
  // reused across the list, so without this it keeps the previous one's values.
  useEffect(() => {
    setName(provider.name);
    setBaseUrl(provider.base_url);
    setModel(provider.model);
    setApiKey("");
  }, [provider.id, provider.name, provider.base_url, provider.model]);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["ai", "providers"] });
    queryClient.invalidateQueries({ queryKey: ["ai", "routing"] });
    queryClient.invalidateQueries({ queryKey: ["pipelines", "summaryStatus"] });
  };

  const save = useMutation({
    mutationFn: () =>
      api.updateAiProvider(provider.id, {
        name,
        base_url: baseUrl,
        model,
        // Present only when the user typed one. See the component docstring.
        ...(apiKey ? { api_key: apiKey } : {}),
      }),
    onSuccess: () => {
      setApiKey("");
      invalidate();
      notify.success("Saved.");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const clearKey = useMutation({
    mutationFn: () => api.updateAiProvider(provider.id, { api_key: null }),
    onSuccess: () => {
      invalidate();
      notify.success("Key cleared.");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const fetchModels = useMutation({
    mutationFn: () => api.fetchAiModels(provider.id),
    onSuccess: (result) => {
      invalidate();
      if (result.status === "ok") {
        notify.success(`Found ${result.models.length} model(s).`);
      } else {
        notify.error(`Could not reach this provider: ${result.reason}`);
      }
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteAiProvider(provider.id),
    onSuccess: () => {
      invalidate();
      notify.success("Provider deleted.");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  return (
    <div className="settings-detail">
      <h2>{provider.name}</h2>

      <label className="settings-field">
        <span>Name</span>
        <input
          className="settings-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>

      <label className="settings-field">
        <span>Base URL</span>
        <input
          className="settings-input"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          spellCheck={false}
        />
      </label>

      <label className="settings-field">
        <span>API key</span>
        <input
          className="settings-input"
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={
            provider.has_key
              ? `Key set (${provider.key_hint}) — leave blank to keep`
              : "No key set"
          }
          autoComplete="off"
        />
      </label>
      {provider.has_key && (
        <button
          className="settings-link-button"
          onClick={() => clearKey.mutate()}
          disabled={clearKey.isPending}
        >
          Clear stored key
        </button>
      )}

      <label className="settings-field">
        <span>Model</span>
        <ModelCombo
          value={model}
          options={provider.models_cache}
          onChange={setModel}
          id={`model-${provider.id}`}
        />
      </label>

      <div className="settings-actions">
        <button onClick={() => save.mutate()} disabled={save.isPending}>
          Save
        </button>
        <button onClick={() => fetchModels.mutate()} disabled={fetchModels.isPending}>
          {fetchModels.isPending ? "Fetching…" : "Fetch models"}
        </button>
        <button
          className="settings-danger"
          onClick={() => {
            if (confirm(`Delete ${provider.name}? Any task using it falls back to the default.`)) {
              remove.mutate();
            }
          }}
          disabled={remove.isPending}
        >
          Delete
        </button>
      </div>

      <ProviderStatus provider={provider} />

      {provider.used_by.length > 0 && (
        <p className="settings-hint">Used by: {provider.used_by.join(", ")}</p>
      )}
    </div>
  );
}

/** The badge. Shows the age of the check, because "ok" from a week ago and
 *  "ok" from a minute ago are different claims. */
function ProviderStatus({ provider }: { provider: AiProvider }) {
  const age = provider.checked_at ? relativeAge(provider.checked_at) : null;

  if (provider.status === "untested") {
    return <p className="settings-status settings-status-untested">Not tested yet</p>;
  }
  if (provider.status === "failed") {
    return (
      <p className="settings-status settings-status-failed">
        Failed{provider.status_reason ? ` — ${humanReason(provider.status_reason)}` : ""}
        {age ? ` · ${age}` : ""}
      </p>
    );
  }
  return (
    <p className="settings-status settings-status-ok">
      Working{age ? ` · checked ${age}` : ""}
    </p>
  );
}

function humanReason(reason: string): string {
  const map: Record<string, string> = {
    invalid_key: "the API key was rejected",
    rate_limited: "rate limited",
    model_not_found: "no such model",
    unreachable: "could not connect",
    bad_response: "unexpected response",
  };
  return map[reason] ?? reason;
}

function relativeAge(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}
```

- [ ] **Step 2: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ProviderForm.tsx
git commit -m "Add AI provider detail form"
```

---

## Task 20: The routing panel

**Files:**
- Create: `frontend/src/components/TaskRoutingPanel.tsx`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/TaskRoutingPanel.tsx`:

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { AiProvider, AiRouting } from "../api/types";
import { notify } from "../stores/messageStore";

/**
 * Which provider serves which task.
 *
 * One row per slot in the backend's catalog -- never a hardcoded list, so that
 * adding an AI feature is a backend-only change and its row appears here on its
 * own. "Use default" writes a *deletion* from the slots map rather than a
 * value, which is what makes the default actually follow later changes instead
 * of being copied once.
 */
export function TaskRoutingPanel({
  routing,
  providers,
}: {
  routing: AiRouting;
  providers: AiProvider[];
}) {
  const queryClient = useQueryClient();

  const save = useMutation({
    mutationFn: (next: { default: string | null; slots: Record<string, string> }) =>
      api.setAiRouting(next),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["ai", "routing"] });
      queryClient.invalidateQueries({ queryKey: ["ai", "providers"] });
      queryClient.invalidateQueries({ queryKey: ["pipelines", "summaryStatus"] });
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const setDefault = (value: string) =>
    save.mutate({ default: value || null, slots: routing.slots });

  const setSlot = (slot: string, value: string) => {
    const slots = { ...routing.slots };
    if (value) {
      slots[slot] = value;
    } else {
      // Deletion, not an empty string: absence is what "use default" means.
      delete slots[slot];
    }
    save.mutate({ default: routing.default, slots });
  };

  return (
    <div className="settings-detail">
      <h2>Task routing</h2>
      <p className="settings-hint">
        Each AI feature can use its own provider. Anything left on “Use default”
        follows the default below.
      </p>

      {providers.length === 0 ? (
        <p className="settings-hint">Add a provider first — there is nothing to route to.</p>
      ) : (
        <table className="settings-table">
          <tbody>
            <tr>
              <th scope="row">Default</th>
              <td>
                <select
                  className="settings-input"
                  value={routing.default ?? ""}
                  onChange={(e) => setDefault(e.target.value)}
                  disabled={save.isPending}
                >
                  <option value="">Nothing — AI features off</option>
                  {providers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </td>
            </tr>

            {routing.catalog.map((slot) => (
              <tr key={slot.name}>
                <th scope="row">{slot.label}</th>
                <td>
                  <select
                    className="settings-input"
                    value={routing.slots[slot.name] ?? ""}
                    onChange={(e) => setSlot(slot.name, e.target.value)}
                    disabled={save.isPending}
                  >
                    <option value="">Use default</option>
                    {providers.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/TaskRoutingPanel.tsx
git commit -m "Add task routing panel"
```

---

## Task 21: The settings view and add-provider flow

**Files:**
- Create: `frontend/src/components/SettingsView.tsx`, `frontend/src/components/ProviderList.tsx`
- Modify: `frontend/src/App.tsx`, `frontend/src/components/Header.tsx`

- [ ] **Step 1: Write the provider list**

Create `frontend/src/components/ProviderList.tsx`:

```tsx
import type { AiProvider } from "../api/types";

/** The left rail: providers, then the routing entry. Selection is lifted to
 *  SettingsView because the detail pane is its sibling, not its child. */
export function ProviderList({
  providers,
  selected,
  onSelect,
  onAdd,
}: {
  providers: AiProvider[];
  selected: string;
  onSelect: (id: string) => void;
  onAdd: () => void;
}) {
  return (
    <nav className="settings-rail">
      {providers.map((p) => (
        <button
          key={p.id}
          className={`settings-rail-item${selected === p.id ? " active" : ""}`}
          onClick={() => onSelect(p.id)}
        >
          <span className="settings-rail-name">{p.name}</span>
          <StatusDot status={p.status} />
        </button>
      ))}

      <button className="settings-rail-item settings-rail-add" onClick={onAdd}>
        + Add provider
      </button>

      <button
        className={`settings-rail-item settings-rail-routing${
          selected === "routing" ? " active" : ""
        }`}
        onClick={() => onSelect("routing")}
      >
        Task routing
      </button>
    </nav>
  );
}

/** Colour only, with a title for the reason -- the detail pane carries the
 *  words. A rail crowded with status text is harder to scan than one dot. */
function StatusDot({ status }: { status: AiProvider["status"] }) {
  const title =
    status === "ok" ? "Working" : status === "failed" ? "Failed" : "Not tested yet";
  return <span className={`settings-dot settings-dot-${status}`} title={title} />;
}
```

- [ ] **Step 2: Write the settings view**

Create `frontend/src/components/SettingsView.tsx`:

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import type { AiPreset } from "../api/types";
import { notify } from "../stores/messageStore";
import { ModalBackdrop } from "./ModalBackdrop";
import { ProviderForm } from "./ProviderForm";
import { ProviderList } from "./ProviderList";
import { TaskRoutingPanel } from "./TaskRoutingPanel";

/**
 * Where the AI providers are configured.
 *
 * Master-detail: the rail lists providers plus a routing entry, and the pane
 * shows whichever is selected. Because routing lives behind a click here, each
 * provider's detail carries a "Used by" line -- otherwise "what is actually
 * using Anthropic?" would be unanswerable while looking at Anthropic.
 */
export function SettingsView() {
  const [selected, setSelected] = useState<string>("routing");
  const [adding, setAdding] = useState(false);

  const providers = useQuery({ queryKey: ["ai", "providers"], queryFn: api.aiProviders });
  const routing = useQuery({ queryKey: ["ai", "routing"], queryFn: api.aiRouting });

  if (providers.isLoading || routing.isLoading) {
    return <div className="settings-page">Loading…</div>;
  }
  if (providers.isError || routing.isError) {
    return <div className="settings-page">Could not load settings.</div>;
  }

  const list = providers.data ?? [];
  const current = list.find((p) => p.id === selected);

  return (
    <div className="settings-page">
      <h1>Settings · AI</h1>

      <div className="settings-body">
        <ProviderList
          providers={list}
          selected={selected}
          onSelect={setSelected}
          onAdd={() => setAdding(true)}
        />

        {current ? (
          <ProviderForm provider={current} />
        ) : (
          <TaskRoutingPanel routing={routing.data!} providers={list} />
        )}
      </div>

      <p className="settings-security-note">
        API keys are encrypted at rest. Anyone with access to this machine can
        decrypt them — this is not a hardened system.
      </p>

      {adding && (
        <AddProviderModal
          onClose={() => setAdding(false)}
          onCreated={(id) => {
            setAdding(false);
            setSelected(id);
          }}
        />
      )}
    </div>
  );
}

/** Picking a preset fills the base URL and the adapter kind. Both stay
 *  editable: a mainland DashScope account and a non-default LM Studio port are
 *  the same provider with a different URL. */
function AddProviderModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const queryClient = useQueryClient();
  const presets = useQuery({
    queryKey: ["ai", "presets"],
    queryFn: api.aiPresets,
    staleTime: Infinity,
  });

  const [presetId, setPresetId] = useState("");
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");

  const preset: AiPreset | undefined = presets.data?.find((p) => p.id === presetId);

  const choosePreset = (id: string) => {
    setPresetId(id);
    const p = presets.data?.find((x) => x.id === id);
    if (p) {
      setBaseUrl(p.base_url);
      if (!name) setName(p.label);
    }
  };

  const create = useMutation({
    mutationFn: () =>
      api.createAiProvider({
        name,
        kind: preset?.kind ?? "openai_compat",
        base_url: baseUrl,
        model: "",
        ...(apiKey ? { api_key: apiKey } : {}),
      }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ["ai", "providers"] });
      notify.success(`Added ${created.name}. Press Fetch models to test it.`);
      onCreated(created.id);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  // ModalBackdrop's dismiss prop is `onClick`, not `onClose` -- it portals to
  // document.body and passes the handler straight to the backdrop div.
  return (
    <ModalBackdrop onClick={onClose}>
      <div className="settings-modal" onClick={(e) => e.stopPropagation()}>
        <h2>Add provider</h2>

        <label className="settings-field">
          <span>Provider</span>
          <select
            className="settings-input"
            value={presetId}
            onChange={(e) => choosePreset(e.target.value)}
          >
            <option value="">Choose…</option>
            {(presets.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </label>

        <label className="settings-field">
          <span>Name</span>
          <input
            className="settings-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="What to call this in the routing table"
          />
        </label>

        <label className="settings-field">
          <span>Base URL</span>
          <input
            className="settings-input"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            spellCheck={false}
          />
        </label>

        {preset?.needs_key !== false && (
          <label className="settings-field">
            <span>API key</span>
            <input
              className="settings-input"
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="off"
            />
          </label>
        )}

        <div className="settings-actions">
          <button
            onClick={() => create.mutate()}
            disabled={!name || !baseUrl || create.isPending}
          >
            Add
          </button>
          <button onClick={onClose}>Cancel</button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
```

Check `frontend/src/components/ModalBackdrop.tsx`'s actual prop signature before relying on `onClose`; match whatever it exports.

- [ ] **Step 3: Add the route**

In `frontend/src/App.tsx`, add the import alongside the other component imports:

```tsx
import { SettingsView } from "./components/SettingsView";
```

and the route after `/activity`:

```tsx
          <Route path="/settings" element={<SettingsView />} />
          <Route path="/settings/ai" element={<SettingsView />} />
```

- [ ] **Step 4: Add the nav link**

In `frontend/src/components/Header.tsx`, add to the `LINKS` array:

```tsx
  { to: "/settings", label: "Settings", title: "AI providers and task routing" },
```

- [ ] **Step 5: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/SettingsView.tsx frontend/src/components/ProviderList.tsx frontend/src/App.tsx frontend/src/components/Header.tsx
git commit -m "Add settings page with master-detail provider config"
```

---

## Task 22: Styles

**Files:**
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Add the styles**

Append to `frontend/src/styles.css`. Match the file's existing custom-property names — read the top of the file first and substitute the real variables for the ones below if they differ:

```css
/* --- Settings page --------------------------------------------------- */

.settings-page {
  max-width: 900px;
  margin: 0 auto;
  padding: 24px 16px 48px;
}

.settings-body {
  display: flex;
  gap: 20px;
  align-items: flex-start;
}

.settings-rail {
  flex: 0 0 190px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.settings-rail-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 7px 10px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.settings-rail-item:hover {
  background: rgba(127, 127, 127, 0.1);
}

.settings-rail-item.active {
  background: rgba(127, 127, 127, 0.16);
  font-weight: 600;
}

.settings-rail-add {
  opacity: 0.7;
}

.settings-rail-routing {
  margin-top: 8px;
  border-top: 1px solid rgba(127, 127, 127, 0.25);
  border-radius: 0 0 5px 5px;
  padding-top: 12px;
}

.settings-dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.settings-dot-ok { background: #3fa66a; }
.settings-dot-failed { background: #d05252; }
.settings-dot-untested { background: rgba(127, 127, 127, 0.45); }

.settings-detail {
  flex: 1 1 auto;
  min-width: 0;
}

.settings-field {
  display: block;
  margin-bottom: 12px;
}

.settings-field > span {
  display: block;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  opacity: 0.65;
  margin-bottom: 4px;
}

.settings-input {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid rgba(127, 127, 127, 0.35);
  border-radius: 4px;
  background: transparent;
  color: inherit;
  font: inherit;
}

.settings-actions {
  display: flex;
  gap: 8px;
  margin: 16px 0 8px;
}

.settings-danger {
  margin-left: auto;
}

.settings-link-button {
  border: 0;
  background: transparent;
  color: inherit;
  opacity: 0.7;
  font-size: 12px;
  text-decoration: underline;
  cursor: pointer;
  padding: 0 0 8px;
}

.settings-hint {
  font-size: 12px;
  opacity: 0.6;
  margin: 4px 0;
}

.settings-status {
  font-size: 13px;
  margin: 8px 0;
}

.settings-status-ok { color: #4cbf7e; }
.settings-status-failed { color: #e07070; }
.settings-status-untested { opacity: 0.6; }

.settings-table {
  width: 100%;
  border-collapse: collapse;
}

.settings-table th {
  text-align: left;
  font-weight: 400;
  padding: 8px 12px 8px 0;
  white-space: nowrap;
}

.settings-table td {
  padding: 8px 0;
}

.settings-security-note {
  margin-top: 32px;
  padding-top: 12px;
  border-top: 1px solid rgba(127, 127, 127, 0.25);
  font-size: 12px;
  opacity: 0.55;
}

.settings-modal {
  min-width: 380px;
  padding: 20px;
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/styles.css
git commit -m "Style the settings page"
```

---

## Task 23: Manual verification against the real stack

Per CLAUDE.md: manual browser testing is the verification step for anything UI-facing, and a green suite can describe hand-built fixtures rather than reality.

- [ ] **Step 1: Bring up the worktree stack**

```bash
./ops/worktree-up.sh
```

Expected: UI on http://localhost:5273, API on http://localhost:8100. This does not disturb the main instance on 5173.

- [ ] **Step 2: Confirm the migration seeded the legacy provider**

```bash
curl -s http://localhost:8100/api/v1/settings/ai/providers | python3 -m json.tool
```

Expected: one provider named "Local" with `base_url` matching the previous `LLM_BASE_URL`, and `used_by` containing `"Default"`. This is the check that an existing installation keeps working without opening the settings page.

- [ ] **Step 3: Fetch models against the real local server**

With LM Studio (or whatever local server this machine runs) up, open http://localhost:5273/settings, select "Local", press **Fetch models**.

Expected: the model dropdown populates with real ids and the badge turns green. If the server is not running, the badge should read "Failed — could not connect", which is the other half of the check.

- [ ] **Step 4: Verify a summary still generates end to end**

The worker does not hot-reload, so restart it first — a handler change that is not picked up reads as "the fix didn't work":

```bash
COMPOSE_PROJECT_NAME=biopipe-wt docker compose restart worker
```

(Substitute the project name `worktree-up.sh` actually used; `docker ps` shows it.) Then in the UI at localhost:5273, open a file with QC data and press the summarize button. Expected: a summary appears, as before this change.

- [ ] **Step 5: Verify the key is not readable from Mongo**

```bash
curl -s -X POST http://localhost:8100/api/v1/settings/ai/providers \
  -H 'Content-Type: application/json' \
  -d '{"name":"KeyTest","kind":"anthropic","base_url":"https://api.anthropic.com","model":"m","api_key":"sk-ant-plaintext-canary-999"}'
```

Then look at the stored document:

```bash
COMPOSE_PROJECT_NAME=biopipe-wt docker compose exec mongo mongosh biopipe --quiet \
  --eval 'db.ai_providers.findOne({name:"KeyTest"})'
```

Expected: `api_key_enc` is binary, and the string `plaintext-canary` appears nowhere. Delete the test provider afterwards through the UI.

- [ ] **Step 6: Verify the key-preservation path in the browser**

Edit "KeyTest"'s model field and press Save without touching the key field. Re-open it. Expected: the placeholder still reads "Key set (sk-ant-…999)". This is the behaviour whose failure silently destroys a credential, and it is worth confirming by hand even though Task 8 tests it.

- [ ] **Step 7: Tear down**

```bash
./ops/worktree-up.sh --down
```

- [ ] **Step 8: Commit any fixes**

If steps 2-6 turned up problems, fix them, re-run the affected tests, and commit.

---

## Task 24: Documentation and close-out

**Files:**
- Modify: `CLAUDE.md`
- Check: `docs/TODO.md`

- [ ] **Step 1: Check whether a TODO entry covers this**

```bash
grep -n -i "llm\|model server\|summar\|ai " docs/TODO.md
```

If an entry describes this work, follow CLAUDE.md's close-out rules: append ` — FIXED` to its heading, write a short note saying what shipped and where the code lives, say what the implementation did differently from its plan, and **move the whole entry to `docs/TODO-done.md`**. If no entry matches, skip to Step 2.

- [ ] **Step 2: Add a CLAUDE.md section**

Append to `CLAUDE.md`, after the "Adding a pipeline tool" section:

```markdown
## Adding an AI-using feature

AI calls go through `app/services/ai/`, never directly to an HTTP endpoint.
The path is always the same two lines:

```python
provider = await ai.resolve(TaskSlot.YOUR_SLOT)   # None means nothing configured
result = await ai.complete(provider, system=..., user=...)
```

Three things about that are easy to get wrong.

**A new feature needs a new `TaskSlot` member** in `app/models/ai.py`, plus a
label in `_SLOT_LABELS`. The settings page renders one row per member, so the
enum is what makes a feature routable -- a call site that reuses
`FILE_SUMMARY` because it is already there silently ties two unrelated
features to one provider, and the user has no way to separate them.

**`complete()` never raises and never returns None.** It returns `Completion`
or `Failure`. Checking `if result is None` -- the shape the old `llm_client`
had -- passes type-checking, reads as correct, and treats every failure as a
success. Check `isinstance(result, Completion)`.

**Thread handlers use `complete_sync` and `_resolve_sync`.** Queue handlers run
in a worker thread with no event loop, so they cannot await either function.
They also get no automatic failure recording (that write needs the loop), so
they return the reason in their result payload instead.

Failures are recorded on the provider document, which is what the settings
badge reads. That means a provider can go red from a real job rather than only
from pressing "Fetch models" -- deliberate, and the reason an expired key is
visible rather than silently stopping summaries.
```

- [ ] **Step 3: Run the full suite one more time**

```bash
./backend/run-worktree-tests.sh tests/ -q
```

Expected: all pass. Read the count.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/TODO.md docs/TODO-done.md
git commit -m "Document the AI provider architecture"
```

- [ ] **Step 5: Merge to main and push**

Per CLAUDE.md, once the suite is green and `main` is clean, merge and push without asking. Check `main` has not moved first:

```bash
git fetch origin && git log --oneline main..origin/main
```

If that shows commits, merge them in and **re-run the suite** before merging — a green from before someone else's changes is not a green. Then:

```bash
git checkout main && git merge --no-ff claude/ai-provider-settings-e1171f && git push origin main
```

---

## Self-Review

**Spec coverage.** Every section of the spec maps to a task: data model → 2; backend package → 3-10; API → 12; connection testing/model discovery → 6, 8, 12; frontend → 17-22; failure handling → 6, 10, 14; testing → embedded throughout plus 16 and 23. The two non-obvious spec commitments — the owner-scoping exemption and the "one real check against a live provider" — are Tasks 13 and 23 respectively.

**Two assumptions were checked against the real code rather than guessed**, and both were wrong on the first pass: `ModalBackdrop` takes `onClick`, not `onClose` (Task 21), and the summary handler's test helpers are `_ctx(payload)` / `_payload(**overrides)`, not a single combined builder (Task 14). Both are now stated as facts with line references.

**One place still needs a look before editing:** the custom-property names in `frontend/src/styles.css` (Task 22). The styles use literal `rgba(127,127,127,…)` values so they work either way, but they should adopt the file's existing variables where those exist.

**Type consistency.** `ResolvedProvider` carries `provider_id`, `name`, `kind`, `base_url`, `api_key`, `model`, `models_cache` and is constructed identically in Tasks 9, 10, 14, and 15. `Failure` is `(reason, detail)` throughout. `fetch_models` returns `list[str] | Failure | None` in the service and is unwrapped to `FetchModelsOut` in the API. The frontend `AiProvider` fields match `ProviderOut` one-for-one.
