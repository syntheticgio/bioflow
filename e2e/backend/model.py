"""Data types shared across the harness backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# The five step verbs (ET-11).
CREATE_PROJECT = "create_project"
UPLOAD = "upload"
MCP = "mcp"
WAIT = "wait"
ASSERT = "assert"
VERBS = frozenset({CREATE_PROJECT, UPLOAD, MCP, WAIT, ASSERT})


@dataclass
class Step:
    verb: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Test:
    name: str
    kind: str  # "yaml" | "python"
    description: str = ""
    steps: list[Step] | None = None
    callable: Callable[..., Awaitable[None]] | None = None

    __test__ = False  # pytest: not a test class
