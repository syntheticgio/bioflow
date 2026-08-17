"""The @test decorator and step primitives for the Python escape hatch.

Each primitive routes through ``ctx.step`` so Python tests get the same
per-step recording as YAML tests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class StepFailure(RuntimeError):
    """Raised when a step or assertion fails; stops the current test."""


@dataclass
class _RegisteredTest:
    name: str
    description: str
    fn: Callable[..., Awaitable[None]]


_REGISTRY: dict[str, _RegisteredTest] = {}


def test(name: str, description: str = ""):
    def deco(fn):
        _REGISTRY[name] = _RegisteredTest(name=name, description=description, fn=fn)
        return fn
    return deco


async def create_project(ctx, name: str) -> dict:
    return await ctx.step("create_project", ctx.op_create_project(name))


async def upload(ctx, file: str) -> dict:
    return await ctx.step("upload", ctx.op_upload(file))


async def mcp(ctx, tool: str, arguments: dict) -> dict:
    return await ctx.step("mcp", ctx.op_mcp(tool, arguments))


async def wait(
    # ASYNC109: a poll budget passed through to op_wait, not a cancel scope.
    ctx, tool: str, arguments: dict, *, timeout: float = 600.0,  # noqa: ASYNC109
    poll: float = 5.0,
) -> dict:
    return await ctx.step("wait", ctx.op_wait(tool, arguments, timeout, poll))


def assert_step(condition: bool, message: str = "") -> None:
    if not condition:
        raise StepFailure(message or "assertion failed")
