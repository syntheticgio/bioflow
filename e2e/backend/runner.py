"""Run orchestration and step execution."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .http_client import (
    get_object,
    launch_pipeline,
    patch_object,
    upload_object,
)
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

    async def op_get_object(
        self,
        object_id: str,
        await_fact: str | None = None,
        timeout: float = 600,  # noqa: ASYNC109 — a polling deadline, see op_wait
        poll: float = 2,
    ) -> dict:
        """Read an object over REST, for its `facts` -- see http_client.

        `await_fact` polls until that key appears. Facts arrive from jobs
        that run *after* the one the test waited on: an alignment's
        `mapped_pct` is recorded by the `index_bam` job the align flow
        queues for its own output, so reading the BAM the moment the align
        job reports success finds no measurement yet. Without this the test
        fails intermittently, and only on a fast machine.
        """
        deadline = time.monotonic() + timeout
        obj: dict = {}
        while True:
            obj = await get_object(
                self.config.base_url, self.config.profile, object_id
            )
            if not await_fact or (obj.get("facts") or {}).get(await_fact) is not None:
                return obj
            if time.monotonic() >= deadline:
                raise StepFailure(
                    f"fact {await_fact!r} never appeared on object {object_id} "
                    f"after {timeout}s (facts: {sorted(obj.get('facts') or {})})"
                )
            await asyncio.sleep(poll)

    async def op_patch(self, object_id: str, body: dict) -> dict:
        """PATCH an object -- setting a role, a name, or tags."""
        return await patch_object(
            self.config.base_url, self.config.profile, object_id, body
        )

    async def op_launch_direct(self, endpoint: str, body: dict) -> dict:
        """POST a launch body written out in the test.

        For pipelines with no suggestion card -- QC, whose body is just
        `{object_id}`. Prefer `op_launch` wherever a card exists: a body
        written here is a copy of a schema that can drift.
        """
        return await launch_pipeline(
            self.config.base_url, self.config.profile, endpoint, body
        )

    async def op_launch(self, suggestions: Any, kind: str) -> dict:
        """Run the card of `kind` from a `suggest_next` result.

        The card is looked up rather than the body being written out in the
        test, because the body is exactly what this application builds
        server-side and what the UI posts unmodified. A test that hand-wrote
        it would keep passing after the real body changed shape.

        An unavailable card fails with its own `reason` -- the field exists
        to say why something cannot run, and it is a far better failure
        message than a schema error from the endpoint.
        """
        cards = suggestions
        if isinstance(cards, dict):
            cards = cards.get("suggestions") or []
        card = next(
            (c for c in cards if isinstance(c, dict) and c.get("kind") == kind), None
        )
        if card is None:
            available = sorted(
                str(c.get("kind")) for c in cards if isinstance(c, dict)
            )
            raise StepFailure(
                f"no {kind!r} card in suggestions (got: {available})"
            )
        # `needs_install` is not a fault and does keep its launch payload, but
        # posting it would start an image pull inside a test run whose timeout
        # is sized for the pipeline, not for a download. Failing here names
        # that distinctly rather than reporting it as a dead end.
        status = card.get("status")
        if status == "needs_install":
            raise StepFailure(
                f"{kind!r} card needs a tool installed first "
                f"({card.get('reason') or 'no reason given'}) -- install it on "
                "the stack under test rather than pulling mid-run"
            )
        if status != "available":
            raise StepFailure(
                f"{kind!r} card is {status}: "
                f"{card.get('reason') or 'no reason given'}"
            )
        launch = card.get("launch") or {}
        endpoint, body = launch.get("endpoint"), launch.get("body")
        if not endpoint or body is None:
            raise StepFailure(f"{kind!r} card carries no launch endpoint/body: {launch}")
        return await launch_pipeline(
            self.config.base_url, self.config.profile, endpoint, body
        )

    # ASYNC109 wants `asyncio.timeout()`, but this timeout is a polling
    # deadline (`time.monotonic() + timeout`) bounding repeated tool calls,
    # not a cancellation scope around one await -- they are not equivalent.
    async def op_wait(
        self,
        tool: str,
        arguments: dict,
        timeout: float,  # noqa: ASYNC109
        poll: float,
        field: str = "state",
        until: frozenset[str] | None = None,
    ) -> dict:
        """Poll `tool` until `field` reaches one of `until`.

        Defaults to a job's `state` reaching a terminal one, which is what
        every job wait needs. The parameters exist for objects: an upload
        returns as soon as the bytes are in, with `status` still `ingesting`
        while format detection runs, so anything launched against a freshly
        uploaded file races that and is rejected with "is not ready". Waiting
        on `status: ready` is the fix, and it is the same polling loop.
        """
        want = until or TERMINAL_STATES
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = await self.mcp.call_tool(tool, arguments)
            value = last.get(field)
            if value in want:
                return last
            # A file that failed to ingest will never become ready; polling
            # to the timeout would report it as slowness rather than error.
            if field == "status" and value == "error":
                raise StepFailure(f"{tool} reported status=error: {last}")
            await asyncio.sleep(poll)
        raise StepFailure(
            f"wait for {tool} timed out after {timeout}s "
            f"({field} never reached {sorted(want)}; last: {last})"
        )


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
    if verb == "launch":
        return args.get("kind") or verb
    if verb == "find":
        return "found"
    if verb == "patch":
        return "patched"
    if verb == "get_object":
        return "object"
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
        until = args.get("until")
        return await ctx.op_wait(
            tool,
            _resolve(args.get("args", {}), ctx),
            timeout=float(args.get("timeout", 600)),
            poll=float(args.get("poll", 5)),
            field=args.get("field", "state"),
            until=frozenset([until] if isinstance(until, str) else until)
            if until
            else None,
        )
    if verb == "launch":
        # Two forms. `kind:` takes the body from a suggest_next card and is
        # the one to prefer -- it tracks whatever the application builds. An
        # explicit `endpoint:`/`body:` covers the pipelines that have no card
        # at all: QC is launched from the UI's QC tab, not the Actions grid,
        # and its body is only `{object_id}`, so there is nothing for a card
        # lookup to track.
        if args.get("endpoint"):
            return await ctx.op_launch_direct(
                args["endpoint"], _resolve(args.get("body", {}), ctx)
            )
        kind = args.get("kind")
        if not kind:
            raise StepFailure("launch step requires 'kind' or 'endpoint'")
        return await ctx.op_launch(_resolve(args.get("from", "$.suggest"), ctx), kind)
    if verb == "get_object":
        object_id = _resolve(args.get("object_id"), ctx)
        if not object_id:
            raise StepFailure("get_object step requires 'object_id'")
        return await ctx.op_get_object(
            str(object_id),
            await_fact=args.get("await_fact"),
            timeout=float(args.get("timeout", 600)),
            poll=float(args.get("poll", 2)),
        )
    if verb == "patch":
        object_id = _resolve(args.get("object_id"), ctx)
        if not object_id:
            raise StepFailure("patch step requires 'object_id'")
        return await ctx.op_patch(str(object_id), _resolve(args.get("body", {}), ctx))
    if verb == "find":
        return _do_find(ctx, args)
    if verb == "assert":
        return _do_assert(ctx, args)
    raise StepFailure(f"unknown step verb: {verb!r}")


def _do_find(ctx: RunContext, args: dict) -> dict:
    """Pick one item out of a list by field value, and keep it under `as:`.

    The reads path needs this because `get_job` deliberately does not return
    a job's `result` -- so the BAM an alignment produced is reached the way a
    user reaches it, by looking at what is now in the project. Written as a
    general step rather than a BAM-specific one because "find the object this
    pipeline just made" is what every downstream assertion needs.
    """
    items = _resolve(args.get("in"), ctx)
    if isinstance(items, dict):
        items = items.get("objects") or []
    if not isinstance(items, list):
        raise StepFailure(f"find: 'in' is not a list (got {type(items).__name__})")

    where = _resolve(args.get("where", {}), ctx)
    if not isinstance(where, dict) or not where:
        raise StepFailure("find requires a non-empty 'where' mapping")

    matches = [
        i for i in items
        if isinstance(i, dict) and all(i.get(k) == v for k, v in where.items())
    ]
    if not matches:
        raise StepFailure(f"find: nothing matching {where} in {len(items)} items")
    if len(matches) > 1 and not args.get("allow_many"):
        # Two BAMs where the test expects one means the flow ran twice or a
        # previous run leaked into this project; silently taking the first
        # would hide that.
        raise StepFailure(
            f"find: {len(matches)} items match {where}; expected exactly one "
            "(set allow_many: true to take the first)"
        )
    return matches[0]


def _do_assert(ctx: RunContext, args: dict) -> None:
    fact = _resolve(args.get("fact"), ctx)
    if "equals" in args:
        expected = _resolve(args.get("equals"), ctx)
        if fact != expected:
            raise StepFailure(f"assert failed: {fact!r} != {expected!r}")
    if "at_least" in args:
        # For the numbers a pipeline produces -- a mapping rate, a surviving
        # read count. `equals` cannot express these: the exact value moves
        # with tool versions, but "essentially all of them mapped" is the
        # claim worth testing, and it is the one that fails when an aligner
        # is pointed at the wrong index.
        threshold = _resolve(args.get("at_least"), ctx)
        try:
            actual, expected = float(fact), float(threshold)
        except (TypeError, ValueError) as exc:
            raise StepFailure(
                f"assert at_least needs numbers, got {fact!r} and {threshold!r}"
            ) from exc
        if actual < expected:
            raise StepFailure(f"assert failed: {actual} < {expected}")
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
