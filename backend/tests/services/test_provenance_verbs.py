"""Every registered handler must be classified as narrating or not.

The failure this prevents: someone adds a pipeline handler, forgets the verb
table, and provenance reports silently omit that step forever. Nothing else
would catch it -- the walker would just render a chain with a hole in it, and
every fixture in this suite would keep passing.
"""

from app.queue import registry
from app.services.provenance_walker import _NO_NARRATIVE_STEP, _STEP_VERBS


def test_every_registered_handler_is_classified():
    """Every handler *actually registered in this process* must be
    classified. This is a subset check, not equality: `worker_run_ids_probe`
    (registered only by tests/queue/test_worker_run_ids.py, with no
    teardown) is listed in `_NO_NARRATIVE_STEP` so the exhaustiveness check
    still passes when that module has been imported first in the same
    process -- but it must not be *required* to be registered, or this test
    fails whenever it runs standalone or before that module is imported.
    """
    registry.load_handlers()
    names = set(registry.all_handlers())

    # Guards against a vacuous pass: if handler modules were never imported,
    # the registry is empty and `set() <= anything` would be trivially True.
    assert names, "registry empty -- handler modules were not imported"

    classified = set(_STEP_VERBS) | _NO_NARRATIVE_STEP
    assert names <= classified, f"unclassified handlers: {names - classified}"


def test_no_handler_is_both_narrating_and_not():
    assert not (set(_STEP_VERBS) & _NO_NARRATIVE_STEP)
