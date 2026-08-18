"""Every heavy launcher must have an escape hatch (#527).

The registry-pair shape CLAUDE.md prescribes: derive the set by inspection
rather than hand-listing it, so a launcher added above the threshold without
`resource_override` fails here instead of silently enqueuing a job the
governor can never claim.

Deriving from source text rather than from the call signature is deliberate:
the declaration is a literal inside a JobResources(...) call, and nothing at
import time exposes it.
"""

import ast
import inspect
import re

from app.services import pipeline_service

# A launcher below this floor cannot be refused by a budget that lets anything
# else run, so the check there would be dead code. Read, never hardcoded.
THRESHOLD_MB = pipeline_service.MIN_DECLARED_MEM_MB


def _declared_mem_mb(func) -> int | None:
    """The flat mem_mb this launcher declares, or None if it computes one."""
    source = inspect.getsource(func)
    literals = [int(m) for m in re.findall(r"mem_mb=(\d+)", source)]
    if literals:
        return max(literals)
    # A named constant, as Task 3 introduces: resolve it off the module.
    names = re.findall(r"mem_mb=([A-Z_][A-Z0-9_]*)", source)
    values = [
        getattr(pipeline_service, n)
        for n in names
        if isinstance(getattr(pipeline_service, n, None), int)
    ]
    return max(values) if values else None


def _launchers():
    for name, obj in vars(pipeline_service).items():
        if name.startswith("launch_") and inspect.isfunction(obj):
            yield name, obj


def _accepts_override(func) -> bool:
    return "resource_override" in inspect.signature(func).parameters


def _calls_the_refusal(func) -> bool:
    tree = ast.parse(inspect.getsource(func).lstrip())
    return any(
        isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "refuse_if_over_budget"
        for node in ast.walk(tree)
    )


def test_every_heavy_launcher_accepts_an_override():
    """R8: the exhaustiveness half of the pair."""
    missing = sorted(
        name
        for name, func in _launchers()
        if (declared := _declared_mem_mb(func)) is not None
        and declared > THRESHOLD_MB
        and not _accepts_override(func)
    )
    assert not missing, (
        f"These launchers declare more than {THRESHOLD_MB} MB with no "
        f"'Launch anyway' escape, so an over-budget run waits forever: {missing}"
    )


def test_every_heavy_launcher_refuses_over_budget():
    """R8: accepting the flag means nothing if nothing checks the budget."""
    missing = sorted(
        name
        for name, func in _launchers()
        if (declared := _declared_mem_mb(func)) is not None
        and declared > THRESHOLD_MB
        and not _calls_the_refusal(func)
    )
    assert not missing, (
        f"These launchers accept an override but never call "
        f"refuse_if_over_budget, so the flag is inert: {missing}"
    )


def test_the_threshold_actually_partitions_the_launchers():
    """The guard against a vacuous pass.

    Both tests above pass trivially if the detector finds nothing. This pins
    that it finds launchers on both sides -- the failure CLAUDE.md describes,
    where a green suite means the seam broke rather than the code is right.
    """
    declared = {
        name: mb
        for name, func in _launchers()
        if (mb := _declared_mem_mb(func)) is not None
    }
    assert any(mb > THRESHOLD_MB for mb in declared.values())
    assert any(mb <= THRESHOLD_MB for mb in declared.values())
    assert len(declared) >= 20, (
        f"Only found {len(declared)} declarations; the detector regexes have "
        f"probably stopped matching how the launchers are written."
    )


def test_every_heavy_launcher_route_exposes_the_override():
    """R9: the flag is useless if the request model cannot carry it.

    Walks the route handlers rather than a hand-listed set of models, so a
    launcher wired to a new model is covered without editing this test.
    """
    from app.api.v1 import pipelines as routes

    heavy = {
        name
        for name, func in _launchers()
        if (mb := _declared_mem_mb(func)) is not None and mb > THRESHOLD_MB
    }
    source = inspect.getsource(routes)
    missing = sorted(
        name
        for name in heavy
        if f"pipeline_service.{name}(" in source
        and "resource_override=body.resource_override"
        not in source.split(f"pipeline_service.{name}(")[1][:400]
    )
    assert not missing, (
        f"These routes call a heavy launcher without forwarding "
        f"resource_override, so the card's button posts a flag the route "
        f"discards: {missing}"
    )
