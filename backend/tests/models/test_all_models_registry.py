"""ALL_MODELS is hand-maintained; a model missing from it is silently uninitialized.

`init_beanie` only registers what ALL_MODELS lists, and beanie raises
`CollectionWasNotInitialized` at the first query against an unregistered
Document -- at runtime, in whatever endpoint happens to touch it first, with
nothing at import or startup saying so. That is how `NodeProvisionTask` shipped
uninitialized: node provisioning failed on its first write, while every test
that never queried the collection stayed green.

This walks the package rather than naming models, so a new module is covered
the day it lands.
"""

import importlib
import inspect
import pkgutil

from beanie import Document
from pymongo import IndexModel

import app.models
from app.models import ALL_MODELS

# Bases that exist to be subclassed, not stored. These have no collection of
# their own and must not be registered.
ABSTRACT_BASES = {"TimestampedDocument"}


def _discovered_documents() -> dict[str, type[Document]]:
    found: dict[str, type[Document]] = {}
    for module_info in pkgutil.iter_modules(app.models.__path__):
        module = importlib.import_module(f"app.models.{module_info.name}")
        for name, obj in vars(module).items():
            if (
                inspect.isclass(obj)
                and issubclass(obj, Document)
                and obj is not Document
                and obj.__module__ == module.__name__
                and name not in ABSTRACT_BASES
            ):
                found[name] = obj
    return found


def test_every_document_subclass_is_registered():
    discovered = _discovered_documents()
    assert discovered, "walked app.models and found no Document subclasses at all"

    missing = sorted(set(discovered) - {model.__name__ for model in ALL_MODELS})
    assert not missing, (
        f"Document subclasses not in ALL_MODELS: {missing}. "
        "init_beanie will not create their collections or indexes, and the "
        "first query against one raises CollectionWasNotInitialized at runtime."
    )


def test_all_models_has_no_duplicates():
    names = [model.__name__ for model in ALL_MODELS]
    assert len(names) == len(set(names)), f"duplicate entries in ALL_MODELS: {names}"


def test_declared_indexes_are_index_models():
    """`db.index_reconcile` reads `.document` off every declared index.

    Beanie itself accepts a bare field-name string in `Settings.indexes`, but
    reconcile_indexes does not -- it raises AttributeError at startup, for the
    whole collection. NodeProvisionTask shipped with `indexes = ["task_id"]`
    and took out 828 tests the moment it was registered.
    """
    offenders = []
    for model in ALL_MODELS:
        settings = getattr(model, "Settings", None)
        for entry in getattr(settings, "indexes", None) or []:
            if not isinstance(entry, IndexModel):
                offenders.append(f"{model.__name__}: {entry!r}")

    assert not offenders, (
        f"Settings.indexes entries that are not IndexModel: {offenders}. "
        "Use IndexModel(...), or an Indexed(...) annotation on the field."
    )


def test_all_models_are_exported():
    """A registered model that isn't in __all__ can't be imported from app.models."""
    missing = sorted({model.__name__ for model in ALL_MODELS} - set(app.models.__all__))
    assert not missing, f"in ALL_MODELS but absent from __all__: {missing}"
