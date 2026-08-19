"""Test-database isolation for parallel (xdist) and concurrent pytest runs.

Every pytest session gets a random run token; every xdist worker within it
gets its own database, `biopipe_test_{token}_{worker}`. The token is what
keeps two *sessions* sharing one Mongo apart (two agents both running the
suite against the main stack); the worker id keeps a session's own workers
apart. conftest.py owns the Mongo I/O; this module holds the decisions so
they can be tested without a database.
"""

import os
import re
import uuid

STALE_AFTER_SECONDS = 7200
_PREFIX = "biopipe_test_"


def ensure_run_token() -> str:
    """The session's token, minted on first ask, inherited by xdist workers.

    setdefault, not assignment: workers are subprocesses of the controller
    and arrive with the controller's token already in their environment.
    """
    return os.environ.setdefault("BIOPIPE_TEST_RUN_TOKEN", uuid.uuid4().hex[:8])


def run_prefix() -> str:
    return f"{_PREFIX}{ensure_run_token()}_"


def worker_db_name() -> str:
    return run_prefix() + os.environ.get("PYTEST_XDIST_WORKER", "main")


def direct_mongo_url(base_url: str) -> str:
    """Moved verbatim from the beanie_models fixture -- see its history."""
    url = base_url
    if "://mongo:" in url:
        url = url.replace("://mongo:", "://127.0.0.1:")
    url = re.sub(r"replicaSet=[^&]*&?", "", url)
    if "directConnection=" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}directConnection=true"
    return url


def stale_test_dbs(db_names, started_at_by_db, now, active_prefix):
    """Which run databases a session start may drop.

    Never the legacy bare `biopipe_test` (no trailing underscore, so the
    prefix check excludes it), never the active run's own, and otherwise
    only those whose `_run_meta` timestamp is missing or older than
    STALE_AFTER_SECONDS -- a failed run's data stays inspectable until then.
    """
    stale = []
    for name in db_names:
        if not name.startswith(_PREFIX):
            continue
        if name.startswith(active_prefix):
            continue
        started = started_at_by_db.get(name)
        if started is None or now - started > STALE_AFTER_SECONDS:
            stale.append(name)
    return stale
