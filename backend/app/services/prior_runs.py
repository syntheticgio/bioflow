"""Which runs already did what a suggestion card offers.

A card is a specific offer -- this aligner, against this reference, on this
file -- so a prior run is one whose recorded parameters match that offer, not
merely one of the same kind. That is what lets a run launched by hand through
the Computations dialog count as a prior run of the card: the match is on the
data, never on a flag saying a card created it.

The matching is structural and happens at query time. The alternative, a
signature hashed into the run at launch, was rejected because it would show
"no prior runs" on every card until the user re-launched everything -- and
would fail silently the first time a default moved in pipeline_service.
"""

from app.models import RunInputRole, RunKind, RunStatus

# The fields that distinguish two runs of the same kind. A kind absent from
# this table matches on kind alone, which is the deliberate default: an
# unlisted kind over-matches rather than showing nothing, and over-matching is
# visible on screen while under-matching is silent.
#
# Values are field names interpreted per kind, NOT blind `params` keys --
# `_run_value` below is where each name is resolved, because the same
# conceptual parameter lives in different places on the two sides. See the
# module's tests for the two that bite.
_MATCH_FIELDS: dict[RunKind, tuple[str, ...]] = {
    RunKind.ALIGNMENT: ("aligner", "reference_id"),
    RunKind.TRIM: ("tool",),
}

# Which RunKind a card's `kind` corresponds to. Cards whose kind is absent
# have no run kind that could match them and never show prior runs.
_CARD_RUN_KINDS: dict[str, RunKind] = {
    "align": RunKind.ALIGNMENT,
    "preprocess": RunKind.TRIM,
}


def _run_value(run, field: str):
    """Read a match field off a run, from wherever that kind actually keeps it.

    Three of these do not live in `params`, which is the whole reason this
    function exists rather than a `run.params.get(field)` at the call site.
    """
    if field == "reference_id":
        # Not a parameter at all: the reference is an input with a role. A
        # comparison that walked `params` would call two alignments against
        # different genomes the same run.
        for item in run.inputs:
            if item.role is RunInputRole.REFERENCE:
                return str(item.object_id)
        return None
    if field == "tool":
        # `create_run` stores a trim's tool in the run's own `tool` field.
        return run.tool
    return run.params.get(field)


def _card_value(card: dict, field: str):
    """Read a match field off a card's launch body."""
    body = (card.get("launch") or {}).get("body") or {}
    if field == "reference_id":
        return body.get("reference_id")
    if field == "tool":
        return body.get("tool")
    # Everything else nests under `params` on this side, even where the run
    # keeps it at the top of its own.
    return (body.get("params") or {}).get(field)


def run_matches_card(run, card: dict) -> bool:
    """True when this run did what this card offers.

    Only the named fields are compared. Comparing `params` wholesale would
    match almost nothing: an alignment's params carry a `read_group` dict
    built partly from the object's own name.
    """
    expected_kind = _CARD_RUN_KINDS.get(card.get("kind"))
    if expected_kind is None or run.kind is not expected_kind:
        return False

    for field in _MATCH_FIELDS.get(expected_kind, ()):
        if _run_value(run, field) != _card_value(card, field):
            return False
    return True


def row_for_run(run, status, names: dict) -> dict:
    """One run as the frontend renders it.

    `names` maps output object id to its current name; an id missing from it
    has been deleted. The row keeps the entry either way -- the run still
    happened, and dropping it would make a real run look like it produced
    nothing.

    No file size: it was considered and cut. An output whose size changed
    unexpectedly is a real signal, but a weak one beside knowing the run
    failed, and two numbers on a row whose job is to say "this already
    happened" is one too many.
    """
    outputs = []
    for object_id in run.outputs:
        key = str(object_id)
        outputs.append(
            {
                "object_id": key,
                "name": names.get(key, "(deleted)"),
                "exists": key in names,
            }
        )

    return {
        "run_id": str(run.id),
        "finished_at": run.created_at,
        "status": status.value,
        "outputs": outputs,
    }
