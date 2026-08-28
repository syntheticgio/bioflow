"""The Host allowlist that closes DNS rebinding (#871).

`is_local_host` carries the whole policy and is pure, so most of this is a
table. The two app-level tests exist because the policy being right is not the
same as it being *reached*: the guard has to run before any route, and it has
to not break the ordinary local request every other test in this suite makes.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.host_guard import _split_host, host_guard, is_local_host


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:5173",
        "127.0.0.1",
        "127.0.0.1:8000",
        "[::1]:8000",
        "::1",
        "",  # HTTP/1.0 sends no Host; refusing it would break curl --http1.0
        "192.168.1.40:8000",  # a LAN browser
        "10.1.2.3",  # a compute node
        "172.20.0.5:8000",  # the docker bridge, which is how `web` proxies
        "bioflow-box:8000",  # single-label LAN hostname
        "mac-studio.local:8000",  # mDNS
        "nas.home.arpa",
    ],
)
def test_local_and_lan_hosts_are_accepted(host):
    assert is_local_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "evil.example.com",  # the rebinding attacker's own name
        "evil.example.com:8000",
        "rebind.attacker.net",
        "8.8.8.8",  # a public IP literal
        "bioflow.example.org",
    ],
)
def test_public_names_are_refused(host):
    assert is_local_host(host) is False


def test_a_configured_name_is_accepted():
    """The escape hatch: a user reaching BioFlow under a real name on purpose."""
    assert is_local_host("bioflow.example.org") is False
    assert is_local_host("bioflow.example.org", frozenset({"bioflow.example.org"})) is True
    # The port is stripped before the allowlist is consulted, so a user does
    # not have to guess whether to include one.
    assert is_local_host("bioflow.example.org:8000", frozenset({"bioflow.example.org"})) is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("LocalHost:5173", "localhost"),
        ("[::1]:8000", "::1"),
        ("::1", "::1"),  # bare IPv6: several colons, no port to strip
        ("192.168.1.1:8000", "192.168.1.1"),
        ("  example.com  ", "example.com"),
    ],
)
def test_split_host(raw, expected):
    assert _split_host(raw) == expected


def _guarded_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(host_guard)

    @app.get("/probe")
    def probe():
        return {"ok": True}

    return app


def test_guard_lets_a_local_request_through():
    r = TestClient(_guarded_app()).get("/probe", headers={"host": "localhost:5173"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_guard_refuses_a_rebound_request_before_the_route():
    """421, and the handler never ran -- a rebinding request must not reach a
    route that would have had a side effect."""
    r = TestClient(_guarded_app()).get("/probe", headers={"host": "evil.example.com"})
    assert r.status_code == 421
    assert "BIOFLOW_ALLOWED_HOSTS" in r.json()["detail"]


def test_guard_runs_outside_the_other_middleware():
    """Registration order matters, and it is the reverse of the intuitive one.

    Starlette applies http middleware in reverse registration order, so the
    guard has to be registered *after* everything it must run outside of.
    `create_app` registers it last for exactly this reason; this test fails if
    that call is ever moved up next to the other middleware.
    """
    seen: list[str] = []

    app = FastAPI()

    @app.middleware("http")
    async def inner(request, call_next):
        seen.append("inner")
        return await call_next(request)

    app.middleware("http")(host_guard)

    @app.get("/probe")
    def probe():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/probe", headers={"host": "evil.example.com"}).status_code == 421
    assert seen == []
    assert client.get("/probe", headers={"host": "localhost"}).status_code == 200
    assert seen == ["inner"]


def test_the_real_app_registers_the_guard_outermost():
    """The policy being right is worth nothing if create_app stops wiring it.

    Asserted on the built middleware stack rather than by issuing a request,
    so this needs no database: `create_app` builds the stack eagerly, and the
    guard being the *first* entry is what "outermost" means at this layer.
    """
    from app.main import create_app

    stack = create_app().user_middleware
    dispatchers = [m.kwargs.get("dispatch") for m in stack]
    assert host_guard in dispatchers, "create_app no longer registers the host guard"
    assert dispatchers[0] is host_guard, (
        "the host guard is no longer outermost -- it must be registered last in create_app"
    )
