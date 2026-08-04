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
