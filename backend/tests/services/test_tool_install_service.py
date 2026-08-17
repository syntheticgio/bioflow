"""Install/uninstall eligibility and dedup for ON_DEMAND_IMAGE tools.

Mirrors tests/queue/test_queue_owner.py's Mongo-only shape: `enqueue` writes
Mongo first and only then pushes to Redis, and it is the Mongo write that
decides whether a job exists, so Redis is stubbed rather than provided.
"""

import uuid

import pytest

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import Job, JobState
from app.pipelines import tools
from app.queue import queue
from app.services import tool_install_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    async def _skip(*args, **kwargs):
        return None

    monkeypatch.setattr(queue, "_push_to_redis", _skip)
    monkeypatch.setattr(queue, "publish_event", _skip)


@pytest.fixture(autouse=True)
def _installed_probe(monkeypatch):
    """`uninstall` checks the live probe before refusing or proceeding.
    Defaults to INSTALLED so tests that only care about the eligibility and
    dedup logic do not also have to fake a Docker daemon; tests of the
    not-installed refusal override this per-test."""
    monkeypatch.setattr(
        tools,
        "deepvariant",
        lambda: tools.Tool(
            name="deepvariant",
            path="/usr/bin/docker",
            version="1.9.0",
            install_state=tools.InstallState.INSTALLED,
        ),
    )


def _owner() -> str:
    return f"tool-install-{uuid.uuid4().hex}"


class TestInstallEligibility:
    async def test_refuses_a_bundled_tool(self):
        with pytest.raises(ValidationError):
            await tool_install_service.install(tool_name="fastp", owner=_owner())

    async def test_refuses_an_unknown_tool(self):
        with pytest.raises(NotFoundError):
            await tool_install_service.install(tool_name="not-a-real-tool", owner=_owner())

    async def test_queues_an_on_demand_tool(self):
        job = await tool_install_service.install(tool_name="deepvariant", owner=_owner())

        assert job.type == "install_tool"
        assert job.payload["tool"] == "deepvariant"
        assert job.state == JobState.PENDING


class TestInstallDedup:
    async def test_a_second_install_returns_the_first_jobs_id(self):
        """The regression this task's plan flags by name: two clicks of
        Install must not start two pulls. `enqueue`'s dedup_key stops the
        second Mongo insert; this is what makes the caller still get *a*
        job back instead of a bare None."""
        owner = _owner()
        first = await tool_install_service.install(tool_name="deepvariant", owner=owner)
        second = await tool_install_service.install(tool_name="deepvariant", owner=owner)

        assert second.id == first.id

    async def test_different_owners_do_not_collide(self):
        """dedup_key folds the owner in via `enqueue` itself (the same
        reasoning `test_queue_owner.py` exists to pin) -- two profiles
        pressing Install on the same tool each get their own job, not one
        profile silently riding the other's."""
        first = await tool_install_service.install(tool_name="deepvariant", owner=_owner())
        second = await tool_install_service.install(tool_name="deepvariant", owner=_owner())

        assert second.id != first.id

    async def test_an_uninstall_in_flight_blocks_a_new_install(self):
        """The dedup key is shared between install_tool and uninstall_tool
        for the same tool on purpose -- pulling and removing the same image
        at once is not a race either side should win silently."""
        owner = _owner()
        removal = await tool_install_service.uninstall(tool_name="deepvariant", owner=owner)

        reused = await tool_install_service.install(tool_name="deepvariant", owner=owner)

        assert reused.id == removal.id
        assert reused.type == "uninstall_tool"


class TestUninstallEligibility:
    async def test_refuses_a_bundled_tool(self):
        with pytest.raises(ValidationError):
            await tool_install_service.uninstall(tool_name="fastp", owner=_owner())

    async def test_refuses_an_unknown_tool(self):
        with pytest.raises(NotFoundError):
            await tool_install_service.uninstall(tool_name="not-a-real-tool", owner=_owner())

    async def test_refuses_when_not_installed(self, monkeypatch):
        """The symmetry rule from the design doc: uninstall is offered
        exactly when install was, so a tool that has never been pulled has
        nothing to remove."""
        monkeypatch.setattr(
            tools,
            "deepvariant",
            lambda: tools.Tool(
                name="deepvariant",
                path="/usr/bin/docker",
                version=None,
                install_state=tools.InstallState.NOT_INSTALLED,
            ),
        )

        with pytest.raises(ValidationError, match="not installed"):
            await tool_install_service.uninstall(tool_name="deepvariant", owner=_owner())

    async def test_queues_removal_when_installed(self):
        job = await tool_install_service.uninstall(tool_name="deepvariant", owner=_owner())

        assert job.type == "uninstall_tool"
        assert job.payload["tool"] == "deepvariant"


class TestUninstallRefusesWhileRunning:
    """`_running_job_using_query` is deliberately *not* owner-scoped -- the
    image DeepVariant runs from is one shared sibling container, so a job
    started by any profile is a real reason to refuse every profile's
    uninstall. That means a RUNNING `call_variants` job with `caller:
    "deepvariant"` inserted directly (bypassing `queue.enqueue`, which never
    lets a job reach RUNNING in these tests) is visible to every other test
    in this module's shared, module-scoped database -- so each test that
    creates one must also retire it, or a later test asserting "does not
    block" fails against a fixture leftover rather than its own setup. Real
    production jobs retire naturally by finishing; these are faked directly
    into RUNNING and so must be faked back out just as directly.
    """

    async def test_refuses_while_a_running_job_uses_the_tool(self):
        """The guard the design doc's symmetry rule does not cover on its
        own: uninstall must not pull the image out from under a variant
        call that is using it right now."""
        owner = _owner()
        running = Job(
            type="call_variants",
            owner=owner,
            state=JobState.RUNNING,
            payload={"caller": "deepvariant"},
        )
        await running.insert()
        try:
            with pytest.raises(ConflictError):
                await tool_install_service.uninstall(tool_name="deepvariant", owner=owner)
        finally:
            running.state = JobState.SUCCEEDED
            await running.save()

    async def test_a_queued_but_not_running_job_does_not_block(self):
        """A job that has not started has not touched the image yet -- see
        _running_job_using_query's docstring for why only RUNNING counts."""
        owner = _owner()
        queued = Job(
            type="call_variants",
            owner=owner,
            state=JobState.QUEUED,
            payload={"caller": "deepvariant"},
        )
        await queued.insert()

        job = await tool_install_service.uninstall(tool_name="deepvariant", owner=owner)
        assert job.type == "uninstall_tool"

    async def test_a_running_job_for_a_different_tool_does_not_block(self):
        owner = _owner()
        running = Job(
            type="call_variants",
            owner=owner,
            state=JobState.RUNNING,
            payload={"caller": "bcftools"},
        )
        await running.insert()
        try:
            job = await tool_install_service.uninstall(tool_name="deepvariant", owner=owner)
            assert job.type == "uninstall_tool"
        finally:
            running.state = JobState.SUCCEEDED
            await running.save()
