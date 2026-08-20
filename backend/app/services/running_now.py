"""Which suggestion cards have work in flight right now.

The companion to `prior_runs`, which answers "what has this card already
done?". This one answers "is it doing it at the moment?", so the Actions tab
can grey out a card's Launch button for the life of the job it started.

Why this is a server question rather than a client one (#454): the first fix
kept the launched job's id in React state, which is lost on a page reload --
the button came back enabled with the job still running. Answering here also
picks up a run the *Computations dialog* started, which the client-side
version could never see because no card launched it.

Keyed on the card's own `launch.endpoint`, not on its `kind` or on `RunKind`:

- `RunKind` is a coarse display vocabulary. Consensus, polish and scaffold
  all record `REFERENCE_ASSEMBLY` with no `tool` set, so nothing on the run
  distinguishes them -- matching on it would grey out all three cards when
  any one of them runs.
- A table keyed on card `kind` has nothing to check itself against: card
  kinds are bare strings with no enum, which is precisely the shape that
  silently skips whichever card someone adds next.

Endpoints are real registered routes and job types are real registered
handlers, so both sides of this table are checkable -- and the tests in
`tests/services/test_running_now.py` check them. That is what makes this a
"genuinely derivable" registry in the sense CLAUDE.md means, rather than a
hand-maintained one that rots.
"""

from app.logging import get_logger
from app.models import ACTIVE_STATES, Job

log = get_logger(__name__)

# Every launch endpoint a suggestion card can post to, and the job types that
# posting it can produce. A set rather than a single value because two of
# these fan out: `/align` picks between the single-shot and chunked aligners
# depending on whether the reference fits in memory, and `/bamstats` indexes
# the BAM first when it has no index.
#
# Endpoints with no card (`/qc`, `/summary`, `/index`, ...) are deliberately
# absent: this table exists to answer a question about cards, and an entry
# nothing can ask about is dead weight. `test_every_endpoint_is_a_real_route`
# constrains it in the direction that matters -- every key must be a route
# that exists.
ENDPOINT_JOB_TYPES: dict[str, frozenset[str]] = {
    "/pipelines/trim": frozenset({"trim_reads"}),
    "/pipelines/align": frozenset({"align_reads", "align_reads_chunked"}),
    "/pipelines/variants": frozenset({"call_variants"}),
    "/pipelines/annotate": frozenset({"annotate_variants"}),
    "/pipelines/annotate-genome": frozenset({"annotate_genome"}),
    "/pipelines/transfer-annotation": frozenset({"transfer_annotation"}),
    "/pipelines/classify-reads": frozenset({"classify_reads"}),
    "/pipelines/assemble": frozenset({"assemble_reads"}),
    "/pipelines/completeness": frozenset({"assess_completeness"}),
    "/pipelines/gc-tracks": frozenset({"analyze_gc_tracks"}),
    "/pipelines/consensus": frozenset({"consensus_from_alignment"}),
    "/pipelines/polish": frozenset({"polish_assembly"}),
    "/pipelines/polish-long": frozenset({"polish_long_assembly"}),
    "/pipelines/scaffold": frozenset({"scaffold_assembly"}),
    "/pipelines/misassemblies": frozenset({"assess_misassemblies"}),
    "/pipelines/multiqc": frozenset({"multiqc_report"}),
    "/pipelines/synteny": frozenset({"analyze_synteny"}),
    "/pipelines/assembly-errors": frozenset({"assess_assembly_errors"}),
    "/pipelines/assembly-qv": frozenset({"assess_assembly_qv"}),
    "/pipelines/assembly-continuity": frozenset({"assess_assembly_continuity"}),
    "/pipelines/quantify": frozenset({"quantify"}),
    "/pipelines/feature-coverage": frozenset({"feature_coverage"}),
    "/pipelines/coverage": frozenset({"coverage"}),
    "/pipelines/salmon-quantify": frozenset({"salmon_quantify"}),

    "/pipelines/transcript-assembly": frozenset({"transcript_assembly"}),
    "/pipelines/structural_variants": frozenset({"call_structural_variants"}),
    "/pipelines/merge_structural_variants": frozenset({"merge_structural_variants"}),
    "/pipelines/phase-variants": frozenset({"phase_variants"}),
    # Offered by the kmer_spectra and repeat_density cards. One route serves
    # both, since the handler runs both analyses in a single job.
    "/pipelines/meryl-analysis": frozenset({"analyze_meryl_tracks"}),
}

# Endpoints in the table above that no route serves yet. Every entry here is a
# known bug in something *else*, not a gap in this mapping -- keeping the list
# explicit is what lets `test_every_endpoint_is_a_real_route` stay strict about
# every other key. An entry should be deleted the moment its route lands, and
# the test fails if one is added here that does resolve, so this cannot quietly
# become a dumping ground.
#
# Empty since #495 landed `/pipelines/meryl-analysis`, its only ever entry.
_ENDPOINTS_WITHOUT_ROUTES: frozenset[str] = frozenset()


async def _active_jobs_for(obj, *, owner: str) -> list[Job]:
    """Every non-terminal job attributed to this file.

    `ACTIVE_STATES` is the queue's own definition of "in flight", so a state
    added later is covered without this module knowing about it. Terminal is
    terminal: a job that failed is not running, and its card must go back to
    being launchable, since retrying is what the user wants next.
    """
    return await Job.find(
        {
            "owner": owner,
            "object_id": obj.id,
            "state": {"$in": [s.value for s in ACTIVE_STATES]},
        }
    ).to_list()


async def attach_running(cards: list[dict], obj, *, owner: str) -> None:
    """Mark every card whose own work is currently in flight.

    Mutates in place for the same reason `attach_prior_runs` does: the caller
    has already converted the cards to dicts, and a parallel list it had to
    zip back up would be a second ordering to keep right.

    One query for the whole grid, not one per card.
    """
    for card in cards:
        card["running"] = False

    jobs = await _active_jobs_for(obj, owner=owner)
    if not jobs:
        return

    active_types = {job.type for job in jobs}

    for card in cards:
        endpoint = (card.get("launch") or {}).get("endpoint")
        if endpoint is None:
            # An unavailable card has no launch payload and so no endpoint to
            # key on. It is already un-pressable; leaving it False keeps the
            # reason it shows ("no assembler installed") rather than
            # overwriting it with "running".
            continue
        wanted = ENDPOINT_JOB_TYPES.get(endpoint)
        if wanted is None:
            # A card pointing at an endpoint this table does not know. Not an
            # error -- the guard simply cannot speak to it -- but worth a log
            # line, since the exhaustiveness tests cover the table's keys and
            # not which endpoints cards actually use.
            log.debug("running_now_unmapped_endpoint", endpoint=endpoint)
            continue
        if active_types & wanted:
            card["running"] = True
