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
import socket
import uuid

STALE_AFTER_SECONDS = 7200
_PREFIX = "biopipe_test_"


def _compose_host_resolves(hostname: str = "mongo") -> bool:
    """Whether the compose service name resolves, i.e. are we in the network?

    Inside the `api` container `mongo` is a real DNS name; on the host it is
    not. That is the whole difference between the two ways this suite is run.
    """
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except OSError:
        return False


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


def direct_mongo_url(base_url: str, *, host_reachable: bool | None = None) -> str:
    """A single-node, direct connection to whichever Mongo this run can see.

    `replicaSet=` is stripped and `directConnection=true` forced because the
    set advertises its members under names only the compose network knows;
    a replica-set-aware driver would follow those and hang.

    The `mongo:` -> `127.0.0.1:` rewrite is the part that depends on where
    pytest is running. From the host, `mongo` does not resolve and the
    published port on localhost is the way in. From inside the `api`
    container the opposite holds: `mongo` resolves and nothing listens on
    the container's own localhost. Rewriting unconditionally -- as this did
    when it was inlined in `beanie_models` -- makes every database test in
    an in-container run fail server selection, which is what it silently
    did until #679 (6 errors in tests/storage alone, on main).

    `host_reachable` overrides the probe, for tests.
    """
    url = base_url
    if host_reachable is None:
        host_reachable = _compose_host_resolves()
    if "://mongo:" in url and not host_reachable:
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
    only those whose `_run_meta` timestamp is older than
    STALE_AFTER_SECONDS -- a failed run's data stays inspectable until then.

    A database with *no* marker is left alone, which is the opposite of the
    obvious rule and the reason this function has a test for it. An
    unstamped database is far more likely being born than abandoned: a
    concurrent run creates it, and only then writes its marker. Sweeping on
    a missing marker turns that gap into a race the other run loses --
    measured, before this rule changed, as `init_beanie` dying with
    "OperationFailure: Operation not permitted" when its index build landed
    on a database this sweep had just dropped underneath it. The cost of
    being wrong the other way is one leaked database until someone drops it
    by hand, which is the cheaper mistake.
    """
    stale = []
    for name in db_names:
        if not name.startswith(_PREFIX):
            continue
        if name.startswith(active_prefix):
            continue
        started = started_at_by_db.get(name)
        if started is not None and now - started > STALE_AFTER_SECONDS:
            stale.append(name)
    return stale
