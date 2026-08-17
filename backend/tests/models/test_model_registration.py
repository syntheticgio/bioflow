"""Every Document subclass must be registered with init_beanie.

A model missing from ALL_MODELS raises CollectionWasNotInitialized on its
first query -- at runtime, in the one code path that uses it, with nothing
failing at import or startup. NodeProvisionTask shipped that way.
"""

import importlib
import pkgutil

import app.models
from app.models import ALL_MODELS
from beanie import Document


def _all_document_subclasses() -> set[type]:
    """Every leaf Document subclass defined under app.models.

    A class with subclasses of its own (e.g. TimestampedDocument) is a shared
    base, not a collection -- it is never passed to init_beanie itself, only
    its concrete subclasses are. Excluding non-leaves is derivable rather
    than a hand-maintained list: in this codebase every actual collection
    model is a leaf, and TimestampedDocument is the only non-leaf.
    """
    for mod in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"app.models.{mod.name}")

    found: set[type] = set()
    stack = [Document]
    while stack:
        for sub in stack.pop().__subclasses__():
            if sub in found:
                continue
            # Only models defined in this package -- not beanie internals.
            if sub.__module__.startswith("app.models"):
                found.add(sub)
            stack.append(sub)

    non_leaves = {cls.__base__ for cls in found if cls.__base__ in found}
    return found - non_leaves


def test_every_document_model_is_registered():
    registered = set(ALL_MODELS)
    missing = sorted(m.__name__ for m in _all_document_subclasses() - registered)
    assert not missing, (
        f"Document models missing from ALL_MODELS: {missing}. "
        "Unregistered models raise CollectionWasNotInitialized on first query."
    )
