"""Run orchestration and step execution."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable

from .config import Config
from .http_client import upload_object
from .mcp_client import McpClient
from .model import Step, Test
from .primitives import StepFailure
from .store import ResultStore

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "dead"})


@dataclass
class RunContext:
    run_id: str
    config: Config
    mcp: McpClient
    store: ResultStore
    fixtures_dir: Path
    test_name: str = ""
    state: dict = field(default_factory=dict)
    project_id: str | None = None
    _step_index: int = field(default=0, init=False)

    async def step(self, verb: str, coro: Awaitable) -> Any:
        """Record and execute one step. Returns its result, or raises StepFailure."""
        index = self._step_index
        self._step_index += 1
        start = time.monotonic()
        await self.store.start_step(self.run_id, self.test_name, index, verb)
        try:
            result = await coro
        except Exception as exc:  # noqa: BLE001 — record any failure, don't crash the run
            await self.store.finish_step(
                self.run_id, self.test_name, index, "failed",
                int((time.monotonic() - start) * 1000), "", str(exc), None,
            )
            raise StepFailure(str(exc)) from exc
        await self.store.finish_step(
            self.run_id, self.test_name, index, "passed",
            int((time.monotonic() - start) * 1000), "", None,
            json.dumps(result) if result is not None else None,
        )
        return result

    # ---- low-level operations (shared by YAML dispatch and Python primitives) ----

    async def op_create_project(self, name: str) -> dict:
        result = await self.mcp.call_tool("create_project", {"name": name})
        pid = result.get("id") or result.get("project_id")
        if not pid:
            raise StepFailure(f"create_project returned no id: {result}")
        self.project_id = str(pid)
        self.state["project_id"] = self.project_id
        return result

    async def op_upload(self, file: str) -> dict:
        if not self.project_id:
            raise StepFailure("upload requires a prior create_project step")
        fixture = self.fixtures_dir / file
        if not fixture.is_file():
            raise StepFailure(f"fixture not found: {fixture}")
        return await upload_object(
            self.config.base_url, self.config.profile, self.project_id, str(fixture)
        )

    async def op_mcp(self, tool: str, arguments: dict) -> dict:
        return await self.mcp.call_tool(tool, arguments)

    async def op_wait(self, tool: str, arguments: dict, timeout: float, poll: float) -> dict:
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = await self.mcp.call_tool(tool, arguments)
            if last.get("state") in TERMINAL_STATES:
                return last
            await asyncio.sleep(poll)
        raise StepFailure(f"wait for {tool} timed out after {timeout}s (last: {last})")


# ---- reference resolution ----


def _resolve(value: Any, ctx: RunContext) -> Any:
    if isinstance(value, str):
        v = value.replace("{{run_id}}", ctx.run_id)
        if v.startswith("$."):
            return _lookup(ctx.state, v[2:].split("."), v)
        return v
    if isinstance(value, list):
        return [_resolve(x, ctx) for x in value]
    if isinstance(value, dict):
        return {k: _resolve(x, ctx) for k, x in value.items()}
    return value


def _lookup(state: dict, parts: list[str], ref: str) -> Any:
    cur: Any = state
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            raise StepFailure(f"unresolved reference {ref!r} (missing {p!r})")
        cur = cur[p]
    return cur


def _default_key(verb: str, args: dict) -> str:
    if verb == "create_project":
        return "project"
    if verb == "upload":
        return "upload"
    if verb in ("mcp", "wait"):
        return args.get("tool", verb)
    return verb


async def _dispatch(ctx: RunContext, verb: str, args: dict) -> Any:
    if verb == "create_project":
        return await ctx.op_create_project(_resolve(args.get("name", ""), ctx))
    if verb == "upload":
        return await ctx.op_upload(_resolve(args.get("file", ""), ctx))
    if verb == "mcp":
        tool = args.get("tool")
        if not tool:
            raise StepFailure("mcp step requires 'tool'")
        return await ctx.op_mcp(tool, _resolve(args.get("args", {}), ctx))
    if verb == "wait":
        tool = args.get("tool", "get_job")
        return await ctx.op_wait(
            tool,
            _resolve(args.get("args", {}), ctx),
            timeout=float(args.get("timeout", 600)),
            poll=float(args.get("poll", 5)),
        )
    if verb == "assert":
        return _do_assert(ctx, args)
    raise StepFailure(f"unknown step verb: {verb!r}")


def _do_assert(ctx: RunContext, args: dict) -> None:
    fact = _resolve(args.get("fact"), ctx)
    if "equals" in args:
        expected = _resolve(args.get("equals"), ctx)
        if fact != expected:
            raise StepFailure(f"assert failed: {fact!r} != {expected!r}")
    if "contains_format" in args:
        fmt = args["contains_format"]
        if isinstance(fact, list):
            items = fact
        elif isinstance(fact, dict):
            items = fact.get("objects") or []
        else:
            items = []
        if not any(isinstance(i, dict) and i.get("format") == fmt for i in items):
            raise StepFailure(f"assert failed: no object with format {fmt!r}")


async def _run_yaml_step(ctx: RunContext, step: Step) -> Any:
    args = dict(step.args)
    as_key = args.pop("as", None)
    result = await ctx.step(step.verb, _dispatch(ctx, step.verb, args))
    if result is not None:
        ctx.state[as_key or _default_key(step.verb, args)] = result
    return result


async def _run_one(ctx: RunContext, test: Test) -> None:
    ctx.test_name = test.name
    ctx._step_index = 0
    ctx.state = {}
    ctx.project_id = None
    if test.callable is not None:
        await test.callable(ctx)
    else:
        for step in test.steps or []:
            await _run_yaml_step(ctx, step)


async def run_batch(
    run_id: str,
    store: ResultStore,
    config: Config,
    fixtures_dir: Path,
    tests: list[Test],
    names: list[str] | None,
) -> None:
    """Run a batch of tests under one run record. Marks the run passed/failed."""
    selected = [t for t in tests if t.name in names] if names else tests
    any_failed = False
    error: str | None = None
    try:
        async with McpClient(config.base_url, config.profile) as mcp:
            for test in selected:
                ctx = RunContext(
                    run_id=run_id, config=config, mcp=mcp, store=store, fixtures_dir=fixtures_dir
                )
                try:
                    await _run_one(ctx, test)
                except StepFailure:
                    any_failed = True
                except Exception as exc:  # noqa: BLE001 — bug in test code
                    any_failed = True
                    error = f"{test.name}: {exc}"
    except Exception as exc:  # noqa: BLE001 — e.g. BioFlow unreachable
        any_failed = True
        error = f"cannot reach BioFlow at {config.base_url}: {exc}"
    await store.finish_run(run_id, "failed" if any_failed else "passed", error)
