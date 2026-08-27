"""Reject requests whose Host header is not a local or LAN name.

This closes DNS rebinding. There is no CORS middleware, which is deliberate --
a cross-origin page cannot *read* our responses -- and the ``X-BioFlow-Profile``
requirement on mutating endpoints forces a preflight nothing answers, so
ordinary cross-site CSRF is already blocked. Rebinding dissolves both defences
at once: the attacker's page re-resolves its own hostname to 127.0.0.1 and is
then *same-origin* with this API, reaching every state-changing endpoint,
including ``/nodes/provision`` and ``/settings/ai/providers``.

What survives rebinding is the ``Host`` header. The browser keeps sending the
attacker's public name (``evil.example.com``) no matter what that name resolves
to, so refusing public DNS names is exactly the discriminator the attack cannot
route around -- while every legitimate caller keeps working:

- The browser on this machine sends ``localhost`` or ``127.0.0.1``.
- A browser or compute node elsewhere on the LAN sends a private IP literal
  (``192.168.x.x``, ``10.x.x.x``, ``172.16-31.x.x``), an mDNS ``.local`` name,
  or a bare single-label hostname -- none of which is resolvable on the public
  internet, so none of which an attacker page can be served from.
- Anything else the user has deliberately pointed at this API is named in
  ``BIOFLOW_ALLOWED_HOSTS``.

`TrustedHostMiddleware` was not used because its allowlist is a fixed list of
patterns, and the set of names a LAN node legitimately arrives under is not
knowable at configure time -- it is a *property* of the name (private, or not),
which is what `is_local_host` decides.
"""

import ipaddress

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import settings

# Names that always mean "this machine", whatever they resolve to.
_ALWAYS_LOCAL = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", ""})

# mDNS. Not resolvable off the local link, so it cannot be rebound.
_LOCAL_SUFFIXES = (".local", ".localhost", ".home.arpa", ".internal")


def _split_host(raw: str) -> str:
    """The hostname from a Host header, with the port and any brackets gone.

    IPv6 literals arrive bracketed (``[::1]:8000``), so a naive rsplit on ":"
    would return ``[``-prefixed garbage for exactly the address that matters.
    """
    host = raw.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            return host[1 : end]
        return host
    # A bare IPv6 literal has several colons and no port; only strip a port
    # when there is exactly one colon.
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host


def is_local_host(raw: str, allowed: frozenset[str] = frozenset()) -> bool:
    """Whether a Host header names this machine, the LAN, or a configured name.

    Pure, so the whole policy is testable without an app or a socket.
    """
    host = _split_host(raw)

    if host in _ALWAYS_LOCAL or host in allowed:
        return True
    if host.endswith(_LOCAL_SUFFIXES):
        return True

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Not an IP. A single-label name ("bioflow-box") has no public DNS
        # zone to be served from, so it cannot be an attacker's origin; a
        # dotted name ("evil.example.com") can be, and is refused.
        return "." not in host

    return addr.is_private or addr.is_loopback or addr.is_link_local


def allowed_hosts() -> frozenset[str]:
    """The extra names from BIOFLOW_ALLOWED_HOSTS, comma-separated."""
    return frozenset(
        h.strip().lower() for h in settings.bioflow_allowed_hosts.split(",") if h.strip()
    )


async def host_guard(request: Request, call_next):
    if not is_local_host(request.headers.get("host", ""), allowed_hosts()):
        return JSONResponse(
            status_code=421,
            content={
                "detail": (
                    "Host header is not a local or LAN name. If you reach BioFlow "
                    "under this name deliberately, add it to BIOFLOW_ALLOWED_HOSTS."
                )
            },
        )
    return await call_next(request)
