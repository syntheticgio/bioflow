"""The role-string to enum mapping the index appliers depend on.

Its own module rather than appended to test_results_owner.py, whose
`pytestmark` applies an asyncio mark to everything in the file -- these
are synchronous and would be marked for no reason.
"""

from app.models import SidecarRole
from app.queue import results


class TestSidecarRoleMapping:
    """`_SIDECAR_ROLES` turns a handler's role string back into an enum member.

    It was a hand-maintained allowlist, and STAR -- the first role added after
    it was written -- was not in it. The consequence was not a crash: every
    one of the eight index files was skipped as an unknown role while
    `build_index` still reported success, so the reference ended up with no
    index and the failure surfaced later as the *aligner* complaining its
    genome directory did not exist.
    """

    def test_every_sidecar_role_is_accepted(self):
        """The property that makes the drift impossible rather than merely
        fixed: a role the enum knows about must be storable."""
        for role in SidecarRole:
            assert results._SIDECAR_ROLES.get(role.value) is role

    def test_an_unknown_role_is_still_rejected(self):
        """Deriving the mapping must not turn it into a passthrough -- a role
        string no enum member matches is a handler bug, and silently coercing
        it would store a sidecar nothing knows how to find again."""
        assert results._SIDECAR_ROLES.get("not-a-real-index") is None

    def test_meryl_db_is_a_known_sidecar_role(self):
        """A meryl database built from a read set is scaffolding, not something
        a person opens -- the same category as a BWA index. Absent from
        _SIDECAR_ROLES it would be silently skipped at ingest, which is how
        STAR's index lost all eight of its files while the suite stayed green.
        """
        assert SidecarRole.MERYL_DB.value in results._SIDECAR_ROLES
