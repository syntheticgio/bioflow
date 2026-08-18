"""Data types shared across the harness backend."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# The step verbs (ET-11). `launch` and `find` were added for the reads path:
# the QC/trim/align flow is driven by POSTing a `suggest_next` card's
# `launch` body to the REST API, and its outputs are reached by selecting
# from `list_objects` -- since `get_job` deliberately does not return a job's
# `result`. Neither is expressible with the original five.
CREATE_PROJECT = "create_project"
UPLOAD = "upload"
MCP = "mcp"
WAIT = "wait"
ASSERT = "assert"
LAUNCH = "launch"
FIND = "find"
PATCH = "patch"
GET_OBJECT = "get_object"
VERBS = frozenset(
    {CREATE_PROJECT, UPLOAD, MCP, WAIT, ASSERT, LAUNCH, FIND, PATCH, GET_OBJECT}
)


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
