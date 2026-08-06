"""Every registered handler must be classified as narrating or not.

The failure this prevents: someone adds a pipeline handler, forgets the verb
table, and provenance reports silently omit that step forever. Nothing else
would catch it -- the walker would just render a chain with a hole in it, and
every fixture in this suite would keep passing.
"""

from app.queue import registry
from app.services.provenance_walker import _NO_NARRATIVE_STEP, _STEP_VERBS


def test_every_registered_handler_is_classified():
    registry.load_handlers()
    names = set(registry.all_handlers())

    # Guards against a vacuous pass: if handler modules were never imported,
    # the registry is empty and `set() == set() | set()` would be True.
    assert names, "registry empty -- handler modules were not imported"

    assert names == set(_STEP_VERBS) | _NO_NARRATIVE_STEP


def test_no_handler_is_both_narrating_and_not():
    assert not (set(_STEP_VERBS) & _NO_NARRATIVE_STEP)
