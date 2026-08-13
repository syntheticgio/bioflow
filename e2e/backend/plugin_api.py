"""FastAPI router for the BioFlow e2e harness backend.

Mounted by Hermes at ``/api/plugins/bioflow-e2e/``. Exports ``router``.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import config as config_mod
from .config import Config
from .loader import discover_tests
from .runner import run_batch
from .store import ResultStore

router = APIRouter()

# Harness root = e2e/ (this file is e2e/backend/plugin_api.py, resolved through
# any install symlink).
_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _ROOT / "tests"
_FIXTURES_DIR = _ROOT / "fixtures"

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
