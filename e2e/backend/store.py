"""SQLite-backed result store.

All operations are async and run SQLite work on a worker thread so the event
loop is never blocked (the plan's concurrency gate). A single connection is
guarded by a threading.Lock.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL,
  error TEXT,
  request_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  test_name TEXT NOT NULL,
  step_index INTEGER NOT NULL,
  verb TEXT NOT NULL,
  status TEXT NOT NULL,
  elapsed_ms INTEGER,
  log TEXT,
  error TEXT,
  result_json TEXT,
  PRIMARY KEY (run_id, test_name, step_index)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResultStore:
    def __init__(self, data_dir: str):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(Path(data_dir) / "results.db"), check_same_thread=False
        )
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_DDL)
        self._conn.commit()
        self._lock = threading.Lock()

    # ---- sync helpers (run on a worker thread) ----

    def _create_run(self, run_id: str, request_json: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (run_id, started_at, status, request_json) VALUES (?, ?, ?, ?)",
                (run_id, _now(), "running", json.dumps(request_json)),
            )
            self._conn.commit()

    def _start_step(self, run_id, test_name, step_index, verb) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO steps (run_id, test_name, step_index, verb, status)"
                " VALUES (?, ?, ?, ?, ?)",
                (run_id, test_name, step_index, verb, "running"),
            )
            self._conn.commit()

    def _finish_step(self, run_id, test_name, step_index, status, elapsed_ms, log, error, result_json) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE steps SET status=?, elapsed_ms=?, log=?, error=?, result_json=?"
                " WHERE run_id=? AND test_name=? AND step_index=?",
                (status, elapsed_ms, log, error, result_json, run_id, test_name, step_index),
            )
            self._conn.commit()

    def _finish_run(self, run_id, status, error=None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status=?, ended_at=?, error=? WHERE run_id=?",
                (status, _now(), error, run_id),
            )
            self._conn.commit()

    def _get_run(self, run_id):
        with self._lock:
            run = self._conn.execute(
                "SELECT run_id, started_at, ended_at, status, error, request_json FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                return None
            steps = self._conn.execute(
                "SELECT test_name, step_index, verb, status, elapsed_ms, log, error, result_json"
                " FROM steps WHERE run_id=? ORDER BY step_index",
                (run_id,),
            ).fetchall()
        tests: dict[str, dict] = {}
        for tn, si, verb, status, elapsed, log, error, result in steps:
            tests.setdefault(tn, {"name": tn, "status": "running", "steps": []})
            tests[tn]["steps"].append({
                "index": si, "verb": verb, "status": status,
                "elapsed_ms": elapsed, "log": log, "error": error,
                "result": json.loads(result) if result else None,
            })
        for t in tests.values():
            if any(s["status"] == "failed" for s in t["steps"]):
                t["status"] = "failed"
            elif all(s["status"] == "passed" for s in t["steps"]):
                t["status"] = "passed"
        return {
            "run_id": run[0], "started_at": run[1], "ended_at": run[2],
            "status": run[3], "error": run[4], "request": json.loads(run[5]),
            "tests": list(tests.values()),
        }

    def _list_runs(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, started_at, ended_at, status FROM runs ORDER BY started_at DESC"
            ).fetchall()
        return [{"run_id": r[0], "started_at": r[1], "ended_at": r[2], "status": r[3]} for r in rows]

    def _delete_run(self, run_id):
        with self._lock:
            self._conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
            self._conn.commit()

    # ---- async API ----

    async def create_run(self, run_id, request_json):
        return await asyncio.to_thread(self._create_run, run_id, request_json)

    async def start_step(self, run_id, test_name, step_index, verb):
        return await asyncio.to_thread(self._start_step, run_id, test_name, step_index, verb)

    async def finish_step(self, run_id, test_name, step_index, status, elapsed_ms, log, error, result_json):
        return await asyncio.to_thread(
            self._finish_step, run_id, test_name, step_index, status, elapsed_ms, log, error, result_json
        )

    async def finish_run(self, run_id, status, error=None):
        return await asyncio.to_thread(self._finish_run, run_id, status, error)

    async def get_run(self, run_id):
        return await asyncio.to_thread(self._get_run, run_id)

    async def list_runs(self):
        return await asyncio.to_thread(self._list_runs)

    async def delete_run(self, run_id):
        return await asyncio.to_thread(self._delete_run, run_id)
