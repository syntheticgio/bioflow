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
from typing import ClassVar

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
    PROVENANCE_NARRATIVE = "provenance_narrative"
    FAILURE_EXPLANATION = "failure_explanation"
    DE_SUMMARY = "de_summary"
    VARIANT_SUMMARY = "variant_summary"

    @property
    def label(self) -> str:
        return _SLOT_LABELS[self]


_SLOT_LABELS = {
    TaskSlot.FILE_SUMMARY: "File summaries",
    TaskSlot.ORGANISM_BLURB: "Organism blurbs",
    TaskSlot.PROVENANCE_NARRATIVE: "Methods narratives",
    TaskSlot.FAILURE_EXPLANATION: "Job failure explanations",
    TaskSlot.DE_SUMMARY: "Differential expression summaries",
    TaskSlot.VARIANT_SUMMARY: "Variant call summaries",
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
    # Model id -> context length, populated alongside models_cache. A model
    # missing here (or mapped to None) means the provider's /v1/models did
    # not report a context_length for it -- not every provider does (OpenAI's
    # own endpoint omits it) -- and callers fall back to a configured default.
    context_windows: dict[str, int] = Field(default_factory=dict)
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

    SINGLETON_ID: ClassVar[str] = "ai_routing"

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
