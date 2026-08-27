"""Proving whether a compute node reads the primary's `BIOINFO_HOME`.

Chunked alignment fans out one `align_reads` sub-job per reference bucket with
no `target_node` (`queue/chunked_align_handlers.py`), so any node may claim
one. That is correct only if every node reads the same storage -- and until
this module, nothing established whether it did. The failure surfaced hours
into an alignment as "Input reads not found", naming the file rather than the
cause.

**Why this cannot reuse `.biopipe/VERSION`.** The obvious probe is to compare
the existing mount sentinel on both machines. It does not work, because
`SENTINEL_CONTENT` is the fixed literal `"biopipe-home-v1\\n"`
(`storage/home.py`), written verbatim onto every home that has ever run
`initialize_home()`. Two machines that each initialised their own local disk
hold byte-identical copies, so the comparison reports *shared* for precisely
the case this module exists to catch. Confirmed on real hardware 2026-08-27:
the deployment's one enrolled node holds an identical `VERSION` and cannot
read a single byte of the primary's storage. `.biopipe/VERSION` keeps its job
-- it is a mount check, and a correct one. It is not an identity check.

So the probe writes a **per-probe nonce** the other side cannot produce
independently, and reads it back by path. Path and content must both coincide.

**Returning `False` and being unable to answer are different outcomes**, and
conflating them is the mistake this module is shaped to prevent. A node that
answers "no such file" is not shared. A probe that times out, or a primary
that cannot write its own sentinel, has learned nothing -- recording `False`
there would turn an infrastructure fault into a verified negative and exclude
a perfectly good node from work forever. The first returns a `ProbeResult`;
the second raises `StorageProbeError`.
"""

import asyncio
import secrets
from dataclasses import dataclass

from app.config import settings
from app.logging import get_logger
from app.services import node_ssh

log = get_logger(__name__)

SENTINEL_PREFIX = "probe-"

# A whole probe is a write, one `cat` over an already-open connection, and an
# unlink. Ten seconds is generous for that and still fails long before the
# provisioning flow's own timeouts.
PROBE_TIMEOUT_SECONDS = 10

# The probe reads once, deliberately -- there is no retry loop below. Whether
# an SMB client can lag behind the server's write is a real question, left open
# on purpose: there is no CIFS mount in this deployment to measure against, and
# the design forbids settling it by reasoning about the protocol. #848 creates
# the first real mount and owns re-testing it. If it lags, the bounded retry
# belongs in this function, with its own constant, not bolted onto the caller.


class StorageProbeError(Exception):
    """The probe could not be carried out -- so the answer is unknown.

    Distinct from a `ProbeResult` with `shared=False`, which is a real answer.
    """


@dataclass
class ProbeResult:
    """The outcome of a probe that actually ran."""

    shared: bool
    detail: str


def _sentinel_body(token: str) -> str:
    """The sentinel's contents: the nonce, plus an explanation for a human.

    Someone will find one of these after a crash, on a shared volume, with no
    idea what wrote it. Tell them there rather than making them grep for it.
    """
    return (
        f"{token}\n"
        "\n"
        "BioFlow shared-storage probe sentinel.\n"
        "Written to check whether a compute node reads this same directory,\n"
        "and normally deleted within seconds. If you are reading this, a probe\n"
        "was interrupted. The file is inert and safe to delete.\n"
    )


async def probe_shared_storage(conn, storage_location: str) -> ProbeResult:
    """Write a nonce here, read it there. Round trip or nothing.

    `conn` is an already-open `asyncssh` connection to the node --
    provisioning has one in hand, and so does the re-probe endpoint.
    `storage_location` is the node's `BIOINFO_HOME`, as the node sees it.

    Raises `StorageProbeError` if the probe could not be carried out.
    """
    token = secrets.token_hex(16)
    local_path = settings.meta_dir / f"{SENTINEL_PREFIX}{token}"
    remote_path = f"{storage_location}/.biopipe/{SENTINEL_PREFIX}{token}"

    try:
        local_path.write_text(_sentinel_body(token))
    except OSError as e:
        # The primary cannot write its own storage. That says nothing about
        # the node, so it is not a `False` -- it is a failed probe.
        raise StorageProbeError(
            f"Cannot write the storage probe sentinel to {local_path.parent}: "
            f"{e.strerror}. This is the primary's own BIOINFO_HOME, so this is "
            "a problem with the primary rather than with the node."
        ) from e

    try:
        # Quote it: `storage_location` has no validator on the request model
        # and is interpolated straight into a remote command.
        command = f"cat {node_ssh.quote_for_shell(remote_path)}"
        try:
            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except TimeoutError as e:
            raise StorageProbeError(
                f"Timed out after {PROBE_TIMEOUT_SECONDS}s reading the storage "
                f"probe sentinel at {remote_path}. The node did not answer, so "
                "whether it shares this storage is unknown."
            ) from e
        except OSError as e:
            raise StorageProbeError(
                f"Could not run the storage probe on the node: {e}. Whether it "
                "shares this storage is unknown."
            ) from e

        # Detection is by exit status and stdout, never through
        # `_command_output`: that helper is stderr-first *for display*, so a
        # missing sentinel would come back as cat's error message -- a
        # non-empty string that reads as success. Verified against a real node
        # 2026-08-27: a missing file gives exit 1, empty stdout, message on
        # stderr.
        stdout = result.stdout or ""
        if result.exit_status != 0:
            return ProbeResult(
                shared=False,
                detail=(
                    f"{remote_path} could not be read on the node "
                    f"({(result.stderr or '').strip() or 'no output'})."
                ),
            )

        # Substring on the whole file, not equality: the body carries the
        # human-readable explanation alongside the nonce.
        if token in stdout:
            return ProbeResult(
                shared=True,
                detail=f"The node read the sentinel written at {local_path}.",
            )

        return ProbeResult(
            shared=False,
            detail=(
                f"{remote_path} exists on the node but does not contain this "
                "probe's token, so it is a different file of the same name -- "
                "the node has its own storage, not the primary's."
            ),
        )
    finally:
        # A cleanup failure must not change the answer (R6). Log and continue.
        try:
            local_path.unlink(missing_ok=True)
        except OSError as e:
            log.warning(
                "node_storage_probe_cleanup_failed",
                path=str(local_path),
                error=e.strerror,
            )
