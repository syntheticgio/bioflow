"""FastAPI router for the BioFlow e2e harness backend (thin shim).

Hermes imports this file as a flat module (``spec_from_file_location``, no
package), so it MUST NOT use relative imports. It locates the copied
``e2e_backend`` package alongside it and mounts the routes. Tests and
fixtures live next to the plugin (copied by install.sh).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_DASHBOARD = Path(__file__).resolve().parent
if str(_DASHBOARD) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD))

from e2e_backend import config as config_mod  # noqa: E402
from e2e_backend.config import Config  # noqa: E402
from e2e_backend.loader import discover_tests  # noqa: E402
from e2e_backend.runner import run_batch  # noqa: E402
from e2e_backend.store import ResultStore  # noqa: E402

router = APIRouter()

_PLUGIN_ROOT = _DASHBOARD.parent
_TESTS_DIR = _PLUGIN_ROOT / "tests"
_FIXTURES_DIR = _PLUGIN_ROOT / "fixtures"

_store: ResultStore | None = None
_running: dict[str, asyncio.Task] = {}


def _get_store() -> ResultStore:
    global _store
    if _store is None:
        _store = ResultStore(config_mod.data_dir())
    return _store


class RunRequest(BaseModel):
    tests: list[str] | None = None


class ConfigBody(BaseModel):
    base_url: str = "http://localhost:8000"
    profile: str = ""
    cleanup: bool = False


@router.get("/config")
async def get_config():
    return config_mod.load(config_mod.data_dir())


@router.put("/config")
async def put_config(body: ConfigBody):
    cfg = Config(base_url=body.base_url, profile=body.profile, cleanup=body.cleanup)
    config_mod.save(config_mod.data_dir(), cfg)
    return cfg


@router.get("/tests")
async def list_tests():
    tests = discover_tests(str(_TESTS_DIR))
    return [{"name": t.name, "kind": t.kind, "description": t.description} for t in tests]


@router.post("/runs")
async def start_run(body: RunRequest):
    store = _get_store()
    tests = discover_tests(str(_TESTS_DIR))
    names = body.tests or [t.name for t in tests]
    run_id = uuid.uuid4().hex
    await store.create_run(run_id, {"tests": names})
    cfg = config_mod.load(config_mod.data_dir())
    task = asyncio.create_task(_run_task(run_id, store, cfg, tests, names))
    _running[run_id] = task
    return {"run_id": run_id}


async def _run_task(run_id: str, store: ResultStore, cfg: Config, tests, names) -> None:
    try:
        await run_batch(run_id, store, cfg, _FIXTURES_DIR, tests, names)
    except Exception:  # noqa: BLE001 — never leave a run stuck in "running"
        await store.finish_run(run_id, "failed", "harness error")
    finally:
        _running.pop(run_id, None)


@router.get("/runs")
async def list_runs():
    return await _get_store().list_runs()


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = await _get_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return run


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(run_id: str):
    await _get_store().delete_run(run_id)
