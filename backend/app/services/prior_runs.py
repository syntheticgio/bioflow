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

from app.models import (
    DataObject,
    PipelineRun,
    RunInputRole,
    RunKind,
    RunStatus,
)
from app.services import run_service

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
    "phase": RunKind.PHASE_VARIANTS,
    "haplotag": RunKind.HAPLOTAG,
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

    `names` maps output object id to `{"name": str, "sidecar_of": id | None}`;
    an id missing from it has been deleted. The row keeps a deleted entry
    rather than dropping it -- the run still happened, and dropping it would
    make a real run look like it produced nothing.

    Sidecars are dropped rather than kept-and-marked, unlike deleted outputs.
    `record_outputs` attaches a shared `build_index` job's sidecar files
    (.fai, .amb, ...) to every alignment run that reused that index, because
    `RunJob` links a shared job to many runs rather than copying it -- so
    `run.outputs` genuinely contains scaffolding this run did not produce.
    Confirmed against a real project: an align card's one prior run listed
    the BAM alongside six index files before this filter existed. `sidecar_of`
    is the field that already marks exactly this distinction.

    No file size: it was considered and cut. An output whose size changed
    unexpectedly is a real signal, but a weak one beside knowing the run
    failed, and two numbers on a row whose job is to say "this already
    happened" is one too many.
    """
    outputs = []
    for object_id in run.outputs:
        key = str(object_id)
        info = names.get(key)
        if info is not None and info["sidecar_of"] is not None:
            continue
        outputs.append(
            {
                "object_id": key,
                "name": info["name"] if info else "(deleted)",
                "exists": info is not None,
            }
        )

    return {
        "run_id": str(run.id),
        "finished_at": run.created_at,
        "status": status.value,
        "outputs": outputs,
    }


# Runs still in flight. The card is a record of what has happened, and the
# Activity view already owns work in progress -- a card claiming a prior run
# that has not produced anything yet would link to a file that is not there.
_IN_FLIGHT = frozenset({RunStatus.WAITING, RunStatus.RUNNING})

# Three, per the design. Enough to see a pattern, few enough that the card
# stays a card.
_MAX_ROWS = 3


async def _runs_touching(obj) -> list:
    """Every run in this project that took this object as an input."""
    return await PipelineRun.find(
        {
            "owner": obj.owner,
            "project_id": obj.project_id,
            "inputs.object_id": obj.id,
        }
    ).to_list()


async def _output_names(object_ids: list, *, owner: str) -> dict:
    """Current name and sidecar status for each output id.

    A missing id has been deleted. `sidecar_of` rides along so `row_for_run`
    can drop index scaffolding without a second query -- see its docstring
    for why `run.outputs` contains sidecars in the first place.
    """
    if not object_ids:
        return {}
    objects = await DataObject.find(
        {"owner": owner, "_id": {"$in": object_ids}}
    ).to_list()
    return {
        str(o.id): {"name": o.name, "sidecar_of": o.sidecar_of}
        for o in objects
    }


async def attach_prior_runs(cards: list[dict], obj, *, owner: str) -> None:
    """Give every card the runs that already did its work.

    Mutates the cards in place -- `suggestions_for` has already converted them
    to dicts by the time this runs, and returning a parallel list the caller
    had to zip back up would be a second thing to keep in order.

    Two queries plus one status derivation for the whole list, not per card:
    the cost of this feature must not scale with how many cards a file has.
    """
    for card in cards:
        card["prior_runs"] = []

    runs = await _runs_touching(obj)
    if not runs:
        return

    # `status_for_many` is already owner-scoped and already two queries rather
    # than 2N -- the reason this does not derive status itself.
    statuses = await run_service.status_for_many(
        [run.id for run in runs], owner=owner
    )

    finished = [
        run
        for run in runs
        if statuses.get(run.id) is not None
        and statuses[run.id] not in _IN_FLIGHT
    ]
    if not finished:
        return

    names = await _output_names(
        [oid for run in finished for oid in run.outputs], owner=owner
    )

    finished.sort(key=lambda r: r.created_at, reverse=True)

    for card in cards:
        card["prior_runs"] = [
            row_for_run(run, statuses[run.id], names)
            for run in finished
            if run_matches_card(run, card)
        ][:_MAX_ROWS]
