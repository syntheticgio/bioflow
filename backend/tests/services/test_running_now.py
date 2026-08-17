"""A card knows when its own work is already in flight.

#454 greyed out a suggestion card's Launch button while the job it started
was running, keyed on the job id the launch returned. That guard lives in
React state, so a page reload lost it and the button came back enabled with
the job still running.

This is the server side of the same question: given a file, which of its
cards have work in flight right now? Answering it here rather than in the
component is what makes the guard survive a reload -- and what lets a run
launched from the Computations dialog grey out the equivalent card, which
the client-side version could never see.

The mapping is keyed on the card's own `launch.endpoint` rather than on its
`kind`. Two reasons, both learned the hard way elsewhere in this repo:
`RunKind` is a coarse display vocabulary that collapses consensus, polish
and scaffold into one member (so matching on it greys out three cards when
one runs), and a table keyed on `kind` has nothing to be checked against --
card kinds are bare strings with no enum. Endpoints are real routes, so
`test_every_endpoint_is_a_real_route` can hold the table honest.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import running_now
from beanie import PydanticObjectId


class TestEndpointJobTypes:
    """The table itself, checked against the two registries it spans."""

    def test_every_endpoint_is_a_real_route(self):
        """A renamed or removed launch route must fail here, not silently
        stop greying out its card."""
        from app.api.v1 import pipelines

        routes = {
            r.path
            for r in pipelines.router.routes
            if "POST" in getattr(r, "methods", set())
        }
        unknown = {
            ep
            for ep in running_now.ENDPOINT_JOB_TYPES
            if ep not in routes
            and ep not in running_now._ENDPOINTS_WITHOUT_ROUTES
        }
        assert unknown == set(), (
            f"endpoints with no matching POST route: {sorted(unknown)}"
        )

    def test_the_missing_route_allowlist_does_not_outlive_the_bug(self):
        """`_ENDPOINTS_WITHOUT_ROUTES` exempts endpoints from the check above,
        so it has to shrink on its own. An entry whose route now exists is a
        stale exemption quietly weakening the test -- fail, so it gets
        deleted rather than accumulating."""
        from app.api.v1 import pipelines

        routes = {
            r.path
            for r in pipelines.router.routes
            if "POST" in getattr(r, "methods", set())
        }
        resolved = running_now._ENDPOINTS_WITHOUT_ROUTES & routes
        assert resolved == set(), (
            "these endpoints have routes now -- remove them from "
            f"_ENDPOINTS_WITHOUT_ROUTES: {sorted(resolved)}"
        )

    def test_every_job_type_is_a_registered_handler(self):
        """A typo'd or renamed job type would make the guard match nothing --
        the exact silent-skip this table exists to avoid."""
        from app.queue.registry import all_handlers, load_handlers

        load_handlers()
        known = set(all_handlers())
        named = {
            t for types in running_now.ENDPOINT_JOB_TYPES.values() for t in types
        }
        assert named - known == set(), (
            f"job types with no handler: {sorted(named - known)}"
        )

    def test_align_covers_its_chunked_variant(self):
        """/align enqueues one of two job types depending on whether the
        reference had to be chunked. Listing only the single-shot one would
        leave exactly the longest alignments un-greyed."""
        assert running_now.ENDPOINT_JOB_TYPES["/pipelines/align"] == frozenset(
            {"align_reads", "align_reads_chunked"}
        )

    def test_every_endpoint_a_card_offers_is_mapped(self):
        """The direction that actually rots.

        The two tests above keep the table's own entries honest, but neither
        notices a *new card* whose endpoint nobody added here -- which is the
        exact silent skip this repo has been bitten by before (STAR and
        `_SIDECAR_ROLES`). Scraping the endpoints out of suggestion_service
        is crude, but it fails loudly the day someone adds a card and forgets
        this table, which is the whole point.
        """
        import re
        from pathlib import Path

        import app.services.suggestion_service as ss

        source = Path(ss.__file__).read_text()
        offered = set(re.findall(r'"endpoint":\s*"(/[a-z0-9/\-]+)"', source))
        missing = offered - set(running_now.ENDPOINT_JOB_TYPES)
        assert missing == set(), (
            "suggestion cards post to endpoints that ENDPOINT_JOB_TYPES does "
            f"not map, so their Launch buttons will not grey out: {sorted(missing)}"
        )


def _card(kind: str, endpoint: str | None) -> dict:
    launch = {"endpoint": endpoint, "body": {}} if endpoint else None
    return {"kind": kind, "launch": launch, "running": False}


class TestAttachRunningNow:
    async def test_a_card_with_an_active_job_is_marked_running(self):
        obj = SimpleNamespace(id=PydanticObjectId(), owner="local")
        cards = [_card("assemble", "/pipelines/assemble"), _card("align", "/pipelines/align")]

        jobs = [SimpleNamespace(type="assemble_reads")]
        with patch(
            "app.services.running_now._active_jobs_for",
            AsyncMock(return_value=jobs),
        ):
            await running_now.attach_running(cards, obj, owner="local")

        assert cards[0]["running"] is True
        assert cards[1]["running"] is False

    async def test_a_chunked_alignment_marks_the_align_card(self):
        obj = SimpleNamespace(id=PydanticObjectId(), owner="local")
        cards = [_card("align", "/pipelines/align")]

        jobs = [SimpleNamespace(type="align_reads_chunked")]
        with patch(
            "app.services.running_now._active_jobs_for",
            AsyncMock(return_value=jobs),
        ):
            await running_now.attach_running(cards, obj, owner="local")

        assert cards[0]["running"] is True

    async def test_no_active_jobs_leaves_every_card_alone(self):
        obj = SimpleNamespace(id=PydanticObjectId(), owner="local")
        cards = [_card("assemble", "/pipelines/assemble"), _card("align", "/pipelines/align")]

        with patch(
            "app.services.running_now._active_jobs_for",
            AsyncMock(return_value=[]),
        ):
            await running_now.attach_running(cards, obj, owner="local")

        assert [c["running"] for c in cards] == [False, False]

    async def test_a_card_with_no_launch_payload_is_never_running(self):
        """An unavailable card has no endpoint to key on. It must come back
        False rather than raising on the missing `launch`."""
        obj = SimpleNamespace(id=PydanticObjectId(), owner="local")
        cards = [_card("polish", None)]

        jobs = [SimpleNamespace(type="polish_assembly")]
        with patch(
            "app.services.running_now._active_jobs_for",
            AsyncMock(return_value=jobs),
        ):
            await running_now.attach_running(cards, obj, owner="local")

        assert cards[0]["running"] is False

    async def test_an_unrelated_job_does_not_grey_anything(self):
        """A summary job running on this file must not disable its pipelines."""
        obj = SimpleNamespace(id=PydanticObjectId(), owner="local")
        cards = [_card("assemble", "/pipelines/assemble"), _card("polish", "/pipelines/polish")]

        jobs = [SimpleNamespace(type="summarize_object")]
        with patch(
            "app.services.running_now._active_jobs_for",
            AsyncMock(return_value=jobs),
        ):
            await running_now.attach_running(cards, obj, owner="local")

        assert [c["running"] for c in cards] == [False, False]

    async def test_sibling_cards_sharing_a_run_kind_stay_independent(self):
        """consensus, polish and scaffold all record RunKind.REFERENCE_ASSEMBLY
        with no tool set, so a run-based match would grey out all three when
        any one runs. Keying on job type is what keeps them apart."""
        obj = SimpleNamespace(id=PydanticObjectId(), owner="local")
        cards = [
            _card("consensus", "/pipelines/consensus"),
            _card("polish", "/pipelines/polish"),
            _card("scaffold", "/pipelines/scaffold"),
        ]

        jobs = [SimpleNamespace(type="polish_assembly")]
        with patch(
            "app.services.running_now._active_jobs_for",
            AsyncMock(return_value=jobs),
        ):
            await running_now.attach_running(cards, obj, owner="local")

        assert [c["running"] for c in cards] == [False, True, False]
