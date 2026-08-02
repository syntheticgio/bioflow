"""Wiring the owner seam for the router tests that run without a database.

Several `pipelines.py` routes are exercised on a bare `FastAPI()` with no Mongo
behind it -- the report and variant-table routes touch only the filesystem, and
standing up the whole app to test a path-traversal check would be a much slower
test of the same thing.

Those routes now resolve `OwnerDep` and then look the object up through
`object_service.get_object`, and neither works without a database. So the two
seams are replaced here rather than in each fixture:

- `get_current_owner` is overridden via FastAPI's own `dependency_overrides`,
  which is the supported way to swap a dependency and keeps the route signature
  under test unchanged.
- `object_service.get_object` is monkeypatched to a stub that honours the same
  contract the real one has: return the object for a known id, raise
  `NotFoundError` for anything else. That contract is what the routes' 404s
  depend on, so a stub that always succeeded would quietly delete the
  not-found half of every test using it.

This helper deliberately does *not* test the isolation itself. Proving that
profile B cannot read profile A's rows needs two real profiles and a real
database; that lives in `tests/api/test_pipelines_profiles.py`, which runs
against the full app. What this file buys is that the fast filesystem tests
keep testing what they were written to test.
"""

from app.api.deps import get_current_owner, get_current_owner_linkable
from app.errors import NotFoundError

# The owner every stubbed lookup here answers to. Its value is arbitrary and
# never asserted on -- these tests are not about which profile is calling.
TEST_OWNER = "test-owner"


def override_owner(app, owner: str = TEST_OWNER) -> None:
    """Make `OwnerDep` and `LinkableOwnerDep` resolve without a profiles
    collection to read. Both are overridden together: a route can be moved
    between the two -- as several were, to accept `?profile=` for a plain
    link -- without a fixture silently going back to hitting a real database
    lookup for the one it forgot."""
    app.dependency_overrides[get_current_owner] = lambda: owner
    app.dependency_overrides[get_current_owner_linkable] = lambda: owner


def stub_get_object(monkeypatch, module, *, known: set[str], value=None):
    """Replace `object_service.get_object` on `module` with a stub.

    `known` is the set of object ids that resolve; everything else raises
    `NotFoundError`, exactly as the real owner-scoped lookup does for both a
    missing id and another profile's id. Keeping that branch is the point --
    the routes' 404 tests run through it.

    Returns the stub, whose `.value` can be reassigned by a test that needs the
    resolved object to look like something in particular.
    """

    async def _get(object_id, *, owner):
        if str(object_id) not in known:
            raise NotFoundError(f"Object not found: {object_id}")
        return _get.value

    _get.value = value if value is not None else object()
    monkeypatch.setattr(module.object_service, "get_object", _get)
    return _get
