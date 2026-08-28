"""Sweeping every enrolled node's shared-storage status in one pass.

#844 made a single node's storage status checkable. This module applies that
probe across the fleet, which is what a deployment enrolled before the field
existed actually needs: every one of its nodes reads `storage_shared = None`,
and #845 is about to treat `None` as not-shared and withdraw work those nodes
have been doing correctly for months.

**The answer is unrecorded, not unknown** -- that distinction is the whole
reason this can be fixed by a sweep rather than by re-provisioning. A probe
can still establish it.

**Nothing here infers a node is shared.** The tempting shortcut -- this node
has been completing `align_reads` for months, so it must read the primary's
storage -- is the same failure #843 exists to remove, arrived at from the
other direction. A node whose history happens to hold only self-fetching jobs
(`download_sra_run`, `download_assembly`) would be blessed on evidence that
says nothing about the question, and #845 would then trust that `True` right
up until a chunked alignment fails hours in. There is exactly one way a node
gets `True`: #844's round trip returned a match.

**Five outcomes, and only two of them write anything.** `shared` and
`not_shared` are answers the probe gave. `unreachable`, `not_probeable` and
`no_recorded_path` are the probe not having run -- so they leave
`storage_shared` and `storage_checked_at` exactly as they were, including
when what was there is already `True`. Recording `False` for a node that was
merely powered off would be the same lie as the shortcut above, and under
#845 it buys nothing anyway: `None` and `False` are already treated alike.

**Stateless, and therefore idempotent.** No cursor, no "already migrated"
marker: every run probes every node it can reach and overwrites that node's
fields with what it just observed. Running it twice gives the same records as
running it once; running it after a share is unmounted gives the *new*
answer, which is why this is not scoped to nodes whose value is `None`. That
is also what lets the same entry point serve the periodic re-verification
this epic wants later, rather than needing a second one.

See `docs/superpowers/specs/2026-08-25-node-storage-migration-design.md`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import asyncssh

from app.logging import get_logger
from app.models.node import Node
from app.services import node_storage_probe
from app.services.ai import crypto
from app.services.node_ssh import connect_with_tofu

log = get_logger(__name__)

# The same timeout the update fan-out uses (`node_update_service.py`). An
# offline node costs this much and the sweep moves on.
_CONNECT_TIMEOUT_SECONDS = 20

# The five outcomes. `shared` and `not_shared` are answers; the rest are the
# probe not having run, and are never written to the node.
SHARED = "shared"
NOT_SHARED = "not_shared"
UNREACHABLE = "unreachable"
NOT_PROBEABLE = "not_probeable"
NO_RECORDED_PATH = "no_recorded_path"

_WROTE_A_RESULT = (SHARED, NOT_SHARED)


@dataclass
class NodeStorageOutcome:
    """One node's result within a sweep.

    `detail` is written for a person reading the report: for `not_shared` it
    carries the remedy naming *that node's* storage location, and for
    `no_recorded_path` it is the ask, since the path is a fact the system
    genuinely does not hold and cannot infer.
    """

    node_id: str
    outcome: str
    storage_shared: bool | None
    storage_location: str | None
    detail: str


def _remedy(node: Node, probe_detail: str) -> str:
    """What the operator does about a node the probe says is not shared."""
    return (
        f"{probe_detail} Mount the primary's BIOINFO_HOME at "
        f"{node.storage_location} on {node.hostname or node.node_id}, then "
        "re-run this check."
    )


async def check_node_storage(
    node: Node, storage_locations: dict[str, str] | None = None
) -> NodeStorageOutcome:
    """Probe one node, classify it, and write the result only if it is one.

    Split out from the sweep so that one node's failure is structurally
    incapable of ending it: everything that can raise lives in here, behind
    the sweep's per-node `except`.
    """
    supplied = (storage_locations or {}).get(node.node_id)

    # Both preconditions are classifications, not errors. #844's endpoint
    # raises 409 for each because a single-node caller asked about a specific
    # node and deserves to be told why; a sweep needs to keep going and
    # report them alongside the answers.
    if not node.ssh_key_enc or not node.ssh_host or not node.ssh_username:
        # `enumerate_nodes` already calls this `updatable` (`nodes.py:174`).
        # Same fact, and it must stay distinguishable from merely-offline:
        # the remedies differ. An offline node needs powering on; this one
        # can never be probed this way at all.
        return NodeStorageOutcome(
            node_id=node.node_id,
            outcome=NOT_PROBEABLE,
            storage_shared=node.storage_shared,
            storage_location=node.storage_location,
            detail=(
                "Cannot check -- this node enrolled itself and BioFlow holds "
                "no SSH key for it. Provision it from here to make it "
                "checkable."
            ),
        )

    private_pem = crypto.decrypt(node.ssh_key_enc)
    if not private_pem:
        # A key that is present but will not decrypt is a *different* problem
        # from having no key, and the remedy differs: the encryption key file
        # was replaced or lost, so re-provisioning the node is what restores
        # it. Reporting this as "the node enrolled itself" -- which is what
        # collapsing the two cases does -- sends the operator looking at the
        # wrong machine entirely.
        return NodeStorageOutcome(
            node_id=node.node_id,
            outcome=NOT_PROBEABLE,
            storage_shared=node.storage_shared,
            storage_location=node.storage_location,
            detail=(
                "Cannot check -- BioFlow holds an SSH key for this node but "
                "cannot decrypt it, so the encryption key it was stored under "
                "is gone. Provision the node again to store a new one."
            ),
        )

    if supplied:
        # Written before the probe so the probe finds it. The operator
        # supplying it is the only way this fact enters the system: see below.
        node.storage_location = supplied
        await node.save()

    if not node.storage_location:
        # The common case on a pre-#844 deployment, not a corner: #844 writes
        # `storage_location` only at provision time, so every node enrolled
        # before it has a null path and there is nothing to probe against.
        #
        # `ProvisionRequest.storage_location` defaults to "/data/scratch" and
        # it is tempting to fall back to it. Don't. It is a *form* default,
        # not a record of what any particular node was given -- a node
        # provisioned elsewhere would be probed at the wrong directory, find
        # nothing, and be recorded `False`: a confident wrong answer, which is
        # strictly worse than an honest "cannot check".
        return NodeStorageOutcome(
            node_id=node.node_id,
            outcome=NO_RECORDED_PATH,
            storage_shared=node.storage_shared,
            storage_location=None,
            detail=(
                "Cannot check -- BioFlow has no record of where this node's "
                "storage is. It was enrolled before that was recorded. Supply "
                "the path it uses and re-run, or provision it again."
            ),
        )

    conn = None
    try:
        conn, _ = await connect_with_tofu(
            node.ssh_host,
            node.ssh_port,
            node.ssh_username,
            private_pem,
            stored_host_key=node.host_key,
            timeout_seconds=_CONNECT_TIMEOUT_SECONDS,
        )
    except (TimeoutError, asyncssh.Error, ValueError) as e:
        # Unreachable is not an answer, so nothing is written. See the module
        # docstring: an unattended sweep during an overnight reboot must not
        # mark a whole cluster not-shared.
        return NodeStorageOutcome(
            node_id=node.node_id,
            outcome=UNREACHABLE,
            storage_shared=node.storage_shared,
            storage_location=node.storage_location,
            detail=(
                f"Cannot check -- {node.ssh_host} did not answer ({e}). The "
                "machine may be off, or its update key may have been removed. "
                "Its storage status is unchanged."
            ),
        )

    try:
        probe = await node_storage_probe.probe_shared_storage(
            conn, node.storage_location
        )
    except node_storage_probe.StorageProbeError as e:
        # The probe could not be carried out -- distinct from a `False`, and
        # treated like unreachable for exactly that reason.
        return NodeStorageOutcome(
            node_id=node.node_id,
            outcome=UNREACHABLE,
            storage_shared=node.storage_shared,
            storage_location=node.storage_location,
            detail=f"Cannot check -- {e} Its storage status is unchanged.",
        )
    finally:
        # Per node, not per sweep: a `finally` at sweep scope would hold every
        # node's connection open until the last one finished. Not awaited
        # (#788) -- `asyncssh`'s close is synchronous and awaiting the
        # MagicMock in tests is what made that bug hard to see.
        conn.close()

    # The three fields move together, so `storage_checked_at` is null if and
    # only if `storage_shared` is None.
    node.storage_shared = probe.shared
    node.storage_checked_at = datetime.now(UTC)
    await node.save()

    return NodeStorageOutcome(
        node_id=node.node_id,
        outcome=SHARED if probe.shared else NOT_SHARED,
        storage_shared=probe.shared,
        storage_location=node.storage_location,
        detail=probe.detail if probe.shared else _remedy(node, probe.detail),
    )


async def sweep_node_storage(
    storage_locations: dict[str, str] | None = None,
) -> list[NodeStorageOutcome]:
    """Probe every enrolled node and report what each one turned out to be.

    Sequential rather than concurrent: this tool targets single-digit node
    counts (the maintainer's deployment has one), so N connects at a 20s
    worst case stays inside an ordinary request. If a deployment outgrows
    that, the fallback is `update_node`'s task-and-poll shape -- not a partial
    sweep that returns early.

    `storage_locations` maps node id to the path the operator says that node
    uses, for nodes that have none recorded. The first run on a pre-#844
    deployment is *expected* to migrate nothing and report every node as
    `no_recorded_path`; the second, carrying the paths the first asked for,
    is the one that migrates. Two runs is the honest cost of a fact the
    system does not hold.
    """
    outcomes: list[NodeStorageOutcome] = []

    async for node in Node.find_all():
        try:
            outcomes.append(await check_node_storage(node, storage_locations))
        except Exception as e:
            # One node's unexpected failure must not end the sweep -- the
            # whole point is getting an answer for every node that can give
            # one. Reported as unreachable because that is what it is from
            # here: nothing was established, so nothing is written.
            log.warning(
                "node_storage_sweep_node_failed",
                node_id=node.node_id,
                error=str(e),
            )
            outcomes.append(
                NodeStorageOutcome(
                    node_id=node.node_id,
                    outcome=UNREACHABLE,
                    storage_shared=node.storage_shared,
                    storage_location=node.storage_location,
                    detail=(
                        f"Cannot check -- the check failed unexpectedly ({e}). "
                        "Its storage status is unchanged."
                    ),
                )
            )

    log.info(
        "node_storage_sweep_complete",
        checked=sum(1 for o in outcomes if o.outcome in _WROTE_A_RESULT),
        total=len(outcomes),
    )
    return outcomes
