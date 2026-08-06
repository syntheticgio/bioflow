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
from typing import Literal

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


@dataclass(frozen=True)
class ToolCall:
    """The model asked to call one of the tools it was offered.

    Only one call is modeled per turn, deliberately -- both wire formats can
    carry more than one in a single response, but the project Q&A loop this
    exists for only ever needs one tool result before deciding its next move.
    A response with several is handled by taking the first and logging the
    rest as dropped (see each adapter's `complete()`), not by modeling a list
    here.
    """

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolSpec:
    """An adapter-neutral tool definition. Each adapter's `complete()`
    translates `parameters` into its own wire shape (OpenAI's
    `function.parameters`, Anthropic's `input_schema`) internally."""

    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ConversationTurn:
    """One turn in a replayed conversation, adapter-neutral.

    Four roles, not two, because a tool exchange is not representable as
    plain user/assistant text in either wire format. `tool_call` records
    what the model asked for -- needed to echo back the assistant's own
    `tool_calls`/`tool_use` block, which both APIs require present before a
    matching result. `tool_result` carries the JSON-string result keyed to
    the call it answers.
    """

    role: Literal["user", "assistant", "tool_call", "tool_result"]
    content: str = ""
    # Only set on role == "tool_call".
    tool_call: ToolCall | None = None
    # Only set on role == "tool_result".
    tool_call_id: str | None = None


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
                parsed = json.loads(response.read().decode())
            if not isinstance(parsed, dict):
                log.warning(
                    "ai_response_not_a_dict", url=url, type=type(parsed).__name__
                )
                return Failure(FailureReason.BAD_RESPONSE)
            return parsed
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

    @staticmethod
    def _render_messages(
        *, system: str, user: str, history: list[ConversationTurn] | None
    ) -> list[dict]:
        if not history:
            return [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        messages: list[dict] = [{"role": "system", "content": system}]
        for turn in history:
            if turn.role in ("user", "assistant"):
                messages.append({"role": turn.role, "content": turn.content})
            elif turn.role == "tool_call":
                call = turn.tool_call
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments),
                                },
                            }
                        ],
                    }
                )
            elif turn.role == "tool_result":
                messages.append(
                    {"role": "tool", "tool_call_id": turn.tool_call_id, "content": turn.content}
                )
        return messages

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        tools: list[ToolSpec] | None = None,
        history: list[ConversationTurn] | None = None,
    ) -> Completion | ToolCall | Failure:
        body = {
            "model": model,
            "messages": self._render_messages(system=system, user=user, history=history),
            # Low but not zero, carried over from llm_client: these summaries
            # restate measured numbers, so invention is the failure mode to
            # suppress, while a little variation keeps a re-run from being
            # pointlessly identical.
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        result = self._request(
            "/v1/chat/completions", body=body, timeout=settings.llm_timeout_seconds
        )
        if isinstance(result, Failure):
            return result

        try:
            message = result["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            log.warning("ai_response_unparseable", keys=sorted(result) if result else None)
            return Failure(FailureReason.BAD_RESPONSE)

        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
        if tool_calls:
            if len(tool_calls) > 1:
                log.info("ai_multi_tool_call_dropped", dropped=len(tool_calls) - 1)
            call = tool_calls[0]
            try:
                arguments = json.loads(call["function"]["arguments"])
            except (KeyError, TypeError, json.JSONDecodeError):
                log.warning("ai_tool_call_arguments_unparseable")
                return Failure(FailureReason.BAD_RESPONSE)
            return ToolCall(id=call["id"], name=call["function"]["name"], arguments=arguments)

        try:
            text = message["content"]
        except (KeyError, TypeError):
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

    def list_models_with_context(self) -> dict[str, int | None] | Failure:
        """Model id -> context_length, for compaction's use.

        A second method rather than widening `list_models()`'s return shape:
        that function has existing callers (the settings-page fetch-models
        flow) that only want the id list, and every one of them would need an
        unpacking step to satisfy a caller only this feature needs. Not every
        provider reports `context_length` (OpenAI's own /v1/models omits it)
        -- a model with no value maps to `None`, not dropped from the dict.
        """
        result = self._request(
            "/v1/models", body=None, timeout=settings.llm_health_timeout_seconds
        )
        if isinstance(result, Failure):
            return result

        entries = result.get("data") or []
        return {
            str(e["id"]): e.get("context_length")
            for e in entries
            if e.get("id")
        }


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

    @staticmethod
    def _render_messages(*, user: str, history: list[ConversationTurn] | None) -> list[dict]:
        if not history:
            return [{"role": "user", "content": user}]
        messages: list[dict] = []
        for turn in history:
            if turn.role in ("user", "assistant"):
                messages.append({"role": turn.role, "content": turn.content})
            elif turn.role == "tool_call":
                call = turn.tool_call
                messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call.id,
                                "name": call.name,
                                "input": call.arguments,
                            }
                        ],
                    }
                )
            elif turn.role == "tool_result":
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": turn.tool_call_id,
                                "content": turn.content,
                            }
                        ],
                    }
                )
        return messages

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        tools: list[ToolSpec] | None = None,
        history: list[ConversationTurn] | None = None,
    ) -> Completion | ToolCall | Failure:
        body = {
            "model": model,
            # Top-level, not a message with role "system". This is the single
            # biggest shape difference from the OpenAI format.
            "system": system,
            "messages": self._render_messages(user=user, history=history),
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        result = self._request(
            "/v1/messages", body=body, timeout=settings.llm_timeout_seconds
        )
        if isinstance(result, Failure):
            return result

        try:
            blocks = result["content"]
        except (KeyError, TypeError):
            log.warning("ai_response_unparseable", keys=sorted(result) if result else None)
            return Failure(FailureReason.BAD_RESPONSE)

        if not isinstance(blocks, list) or not blocks:
            return Failure(FailureReason.BAD_RESPONSE)

        tool_use_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        if tool_use_blocks:
            if len(tool_use_blocks) > 1:
                log.info("ai_multi_tool_call_dropped", dropped=len(tool_use_blocks) - 1)
            block = tool_use_blocks[0]
            try:
                return ToolCall(id=block["id"], name=block["name"], arguments=block["input"])
            except KeyError:
                log.warning("ai_tool_use_block_unparseable")
                return Failure(FailureReason.BAD_RESPONSE)

        try:
            text = blocks[0]["text"]
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

    def list_models_with_context(self) -> dict[str, int | None] | Failure:
        """Model id -> context_length. Never observed present on Anthropic's
        /v1/models -- every model maps to None -- but the method exists here
        too so a caller does not need to special-case the provider kind."""
        result = self._request(
            "/v1/models", body=None, timeout=settings.llm_health_timeout_seconds
        )
        if isinstance(result, Failure):
            return result
        entries = result.get("data") or []
        return {str(e["id"]): e.get("context_length") for e in entries if e.get("id")}


def adapter_for(kind: str, *, base_url: str, api_key: str | None) -> _BaseAdapter:
    from app.models.ai import ProviderKind

    if kind == ProviderKind.ANTHROPIC:
        return AnthropicAdapter(base_url=base_url, api_key=api_key)
    return OpenAICompatAdapter(base_url=base_url, api_key=api_key)
