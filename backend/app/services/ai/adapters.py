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
