"""Background prose about a species, generated once and cached.

Read-through cache: a hit is a single indexed document read, and a miss calls
the model and stores the result. Unlike the file summary this does not go
through the job queue, and the reason is the shape of the request rather than
laziness -- the blurb is page decoration that the detail panel wants *now*, it
takes one short generation, and a queued job would mean the panel renders
without it and then pops it in seconds later. A synchronous call the UI can
show a placeholder for is the honest presentation of that.

Every failure yields None. The blurb is colour; a species the model does not
know, or a server that is not running, simply means the panel shows a name and
no paragraph, exactly as it did before this existed.
"""

import importlib

from app.logging import get_logger
from app.models import OrganismBlurb, normalize_organism
from app.services import summary_prompt
from app.services.ai import router as ai_router
from app.services.ai.adapters import Completion

# NOT `from app.services.ai import complete as ai_complete`, and NOT
# `import app.services.ai.complete as ai_complete` either: app/services/ai/
# __init__.py does `from app.services.ai.complete import complete`, which
# rebinds the *package attribute* `complete` to the function it re-exports,
# shadowing the submodule of the same name. Both of those import forms
# resolve through that attribute and would silently bind the function, not
# the module -- so this goes through `sys.modules` via `importlib` instead,
# which is the only form immune to the shadow, and gives tests a module to
# monkeypatch `.complete` on.
ai_complete = importlib.import_module("app.services.ai.complete")

log = get_logger(__name__)

# Long enough that a genus-only entry like "Escherichia" is allowed through, but
# short enough to reject the junk that lands in a free-text metadata field.
_MIN_ORGANISM_LENGTH = 3
# Nothing this long is a species name, and it is the shape a pasted description
# or an accidental paste of a whole record would take.
_MAX_ORGANISM_LENGTH = 120

# Placeholder values that appear in real metadata and are not organisms. Checked
# against the normalized key.
_NON_ORGANISMS = frozenset(
    {
        "unknown",
        "n/a",
        "na",
        "none",
        "null",
        "unspecified",
        "not applicable",
        "not collected",
        "not provided",
        "missing",
        "other",
        "unclassified",
        "uncultured",
        "synthetic construct",
        "metagenome",
        "mixed",
    }
)


def is_summarizable(organism: str | None) -> bool:
    """Whether a metadata value is worth asking a model about.

    Guards the cache as much as the model: without this, every junk value in a
    free-text field becomes its own permanent row with its own confabulated
    paragraph attached.
    """
    if not organism:
        return False
    key = normalize_organism(organism)
    if not (_MIN_ORGANISM_LENGTH <= len(key) <= _MAX_ORGANISM_LENGTH):
        return False
    if key in _NON_ORGANISMS:
        return False
    # Must contain letters. Catches bare tax IDs and punctuation-only values,
    # which are real things that end up in this field.
    return any(c.isalpha() for c in key)


async def get_cached(organism: str) -> OrganismBlurb | None:
    """The stored blurb for a species, if one has been written."""
    return await OrganismBlurb.find_one(
        OrganismBlurb.organism_key == normalize_organism(organism)
    )


async def get_or_generate(organism: str, *, force: bool = False) -> OrganismBlurb | None:
    """The blurb for a species, generating and caching it on a miss.

    `force` regenerates over an existing entry, which is what a manual refresh
    wants -- the species has not changed, but the user has asked for another
    take, perhaps against a different model.
    """
    from app.config import settings

    if not settings.llm_summaries_enabled or not is_summarizable(organism):
        return None

    key = normalize_organism(organism)
    if not force:
        cached = await OrganismBlurb.find_one(OrganismBlurb.organism_key == key)
        if cached is not None:
            return cached

    from app.models.ai import TaskSlot

    provider = await ai_router.resolve(TaskSlot.ORGANISM_BLURB)
    if provider is None:
        return None

    result = await ai_complete.complete(
        provider,
        system=summary_prompt.ORGANISM_SYSTEM_PROMPT,
        user=summary_prompt.build_organism_prompt(organism.strip()),
        # Shorter than a file summary: this is two or three sentences, and the
        # cap is what stops a chatty model from writing an essay.
        max_tokens=250,
    )
    if not isinstance(result, Completion):
        return None

    text, model = result.text, result.model
    log.info("organism_blurb_generated", organism=key, model=model, chars=len(text))

    # Upsert rather than insert: two files of the same organism can reach here
    # concurrently, and the unique index would turn the loser's insert into an
    # error over a paragraph that is already correct.
    await OrganismBlurb.find_one(OrganismBlurb.organism_key == key).upsert(
        {
            "$set": {
                "organism": organism.strip(),
                "text": text,
                "model": model,
                "generated_at": _now(),
                "updated_at": _now(),
            }
        },
        on_insert=OrganismBlurb(
            organism_key=key,
            organism=organism.strip(),
            text=text,
            model=model,
        ),
    )

    return await OrganismBlurb.find_one(OrganismBlurb.organism_key == key)


def _now():
    from app.models.base import utcnow

    return utcnow()
