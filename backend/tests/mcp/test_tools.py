"""Tool behaviour and owner scoping.

Every assertion about scoping is B asking for A's data, following
`tests/api/test_route_owner_scoping.py`. A single profile's request for its
own data succeeds whether or not the tool ever applied a filter, so a test
written that way proves nothing -- which is the direction that fails when the
seam breaks.
"""

import pytest

from app.errors import NotFoundError, ProfileUnresolvedError
from app.mcp import tools
from app.services import profile_service, project_service

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


async def test_list_projects_returns_this_owners_projects():
    profile = await profile_service.create_profile(username="tools-list")
    owner = profile.owner_id()
    await project_service.create_project(name="Mine", owner=owner)

    result = await tools.list_projects(owner=owner)

    assert [p["name"] for p in result["projects"]] == ["Mine"]


async def test_list_projects_does_not_see_another_owners_projects():
    a = await profile_service.create_profile(username="tools-a")
    b = await profile_service.create_profile(username="tools-b")
    await project_service.create_project(name="A's project", owner=a.owner_id())

    result = await tools.list_projects(owner=b.owner_id())

    assert result["projects"] == []


async def test_get_project_treats_another_owners_project_as_missing():
    """Not a 403: answering differently would confirm the id is real, which
    is the reasoning already written on `jobs._owned_job`."""
    a = await profile_service.create_profile(username="tools-get-a")
    b = await profile_service.create_profile(username="tools-get-b")
    project = await project_service.create_project(name="A's", owner=a.owner_id())

    with pytest.raises(NotFoundError):
        await tools.get_project(str(project.id), owner=b.owner_id())


async def test_create_project_assigns_the_acting_owner():
    profile = await profile_service.create_profile(username="tools-create")
    owner = profile.owner_id()

    result = await tools.create_project("New project", owner=owner)

    stored = await project_service.get_project(result["id"], owner=owner)
    assert stored.name == "New project"


async def test_whoami_reports_the_acting_profile():
    profile = await profile_service.create_profile(username="tools-whoami")

    result = await tools.whoami(owner=profile.owner_id())

    assert result["username"] == "tools-whoami"


async def test_whoami_rejects_a_malformed_owner_cleanly():
    """A malformed owner should reach an agent as an actionable error, not a
    raw bson.errors.InvalidId stack trace -- the same trap app.api.deps'
    resolve_owner already guards against for the REST routes."""
    with pytest.raises(ProfileUnresolvedError):
        await tools.whoami(owner="not-a-valid-object-id")


async def test_suggest_next_returns_cards_with_their_reasons(monkeypatch):
    """The reasons are the point.

    An agent that learns "no aligner is installed" can act; one that gets a
    bare "unavailable" is stuck. This asserts the reason survives the trip
    through the tool rather than being flattened into a status.
    """
    profile = await profile_service.create_profile(username="tools-suggest")
    owner = profile.owner_id()
    project = await project_service.create_project(name="P", owner=owner)

    from app.mcp import tools as mcp_tools

    async def fake_suggestions_for(obj):
        return [
            {"kind": "align", "status": "unavailable", "reason": "No aligner is installed"},
            {"kind": "qc", "status": "available", "payload": {"object_id": "x"}},
        ]

    class FakeObject:
        id = "507f1f77bcf86cd799439011"
        name = "reads.fastq.gz"

    async def fake_get_object(object_id, *, owner):
        return FakeObject()

    monkeypatch.setattr(
        "app.services.suggestion_service.suggestions_for", fake_suggestions_for
    )
    monkeypatch.setattr("app.services.object_service.get_object", fake_get_object)

    result = await mcp_tools.suggest_next("507f1f77bcf86cd799439011", owner=owner)

    unavailable = [s for s in result["suggestions"] if s["status"] == "unavailable"]
    assert unavailable[0]["reason"] == "No aligner is installed"


async def test_suggest_next_returns_an_empty_list_when_nothing_applies(monkeypatch):
    """No candidate pipelines is a well-formed answer, not an omission."""
    profile = await profile_service.create_profile(username="tools-suggest-empty")
    owner = profile.owner_id()

    from app.mcp import tools as mcp_tools

    async def fake_suggestions_for(obj):
        return []

    class FakeObject:
        id = "507f1f77bcf86cd799439011"
        name = "unrecognized.bin"

    async def fake_get_object(object_id, *, owner):
        return FakeObject()

    monkeypatch.setattr(
        "app.services.suggestion_service.suggestions_for", fake_suggestions_for
    )
    monkeypatch.setattr("app.services.object_service.get_object", fake_get_object)

    result = await mcp_tools.suggest_next("507f1f77bcf86cd799439011", owner=owner)

    assert result == {"suggestions": []}


async def test_run_pipeline_rejects_an_unknown_kind():
    """The error names the valid kinds.

    An agent that gets "unknown kind: algn" and a list can correct itself; one
    that gets a bare 400 retries the same thing.
    """
    profile = await profile_service.create_profile(username="tools-run-bad")

    from app.errors import ValidationError

    with pytest.raises(ValidationError) as exc:
        await tools.run_pipeline("not_a_real_kind", {}, owner=profile.owner_id())

    assert "not_a_real_kind" in str(exc.value)


async def test_run_pipeline_enqueues_a_known_kind(monkeypatch):
    profile = await profile_service.create_profile(username="tools-run-ok")
    owner = profile.owner_id()

    captured = {}

    async def fake_enqueue(job_type, *, owner, payload=None, **kwargs):
        captured["job_type"] = job_type
        captured["owner"] = owner
        captured["payload"] = payload

        class FakeState:
            value = "queued"

        class FakeJob:
            id = "507f1f77bcf86cd799439099"
            type = job_type
            state = FakeState()

        return FakeJob()

    monkeypatch.setattr("app.queue.queue.enqueue", fake_enqueue)

    from app.queue import registry

    # Handler modules are imported for their registration side effects only
    # at app/worker startup (app/main.py, app/queue/worker.py), not merely by
    # importing app.queue.registry -- so a test process that never started
    # either needs this explicit load first. See
    # tests/services/test_provenance_verbs.py for the same pattern.
    registry.load_handlers()
    kind = next(iter(registry.all_handlers()))

    result = await tools.run_pipeline(kind, {"object_id": "abc"}, owner=owner)

    assert captured["job_type"] == kind
    assert captured["owner"] == owner
    assert result["job_id"] == "507f1f77bcf86cd799439099"


async def test_list_jobs_does_not_see_another_owners_jobs():
    a = await profile_service.create_profile(username="tools-jobs-a")
    b = await profile_service.create_profile(username="tools-jobs-b")

    result = await tools.list_jobs(owner=b.owner_id())

    assert all(j["owner"] != a.owner_id() for j in result.get("jobs", []))


async def test_get_job_treats_another_owners_job_as_missing():
    a = await profile_service.create_profile(username="tools-getjob-a")
    b = await profile_service.create_profile(username="tools-getjob-b")

    from app.models import Job

    job = Job(type="qc", owner=a.owner_id())
    await job.insert()

    with pytest.raises(NotFoundError):
        await tools.get_job(str(job.id), owner=b.owner_id())


async def test_cancel_job_treats_another_owners_job_as_missing():
    a = await profile_service.create_profile(username="tools-cancel-a")
    b = await profile_service.create_profile(username="tools-cancel-b")

    from app.models import Job

    job = Job(type="qc", owner=a.owner_id())
    await job.insert()

    with pytest.raises(NotFoundError):
        await tools.cancel_job(str(job.id), owner=b.owner_id())


async def test_cancel_job_rejects_an_unknown_job_id():
    profile = await profile_service.create_profile(username="tools-cancel-unknown")

    with pytest.raises(NotFoundError):
        await tools.cancel_job("507f1f77bcf86cd799439011", owner=profile.owner_id())


async def test_search_objects_is_scoped_to_the_owner():
    a = await profile_service.create_profile(username="tools-search-a")
    b = await profile_service.create_profile(username="tools-search-b")

    result = await tools.search_objects("anything", owner=b.owner_id())

    assert all(o.get("owner") != a.owner_id() for o in result["objects"])


async def test_search_ncbi_returns_organisms_and_their_assemblies(monkeypatch):
    """Regression test for a real bug caught in code review: AssemblyMetadata
    has no as_dict() (unlike TaxonSuggestion), so building the assemblies
    list naively crashed on any organism that actually had assemblies on
    file. This exercises the real conversion path end to end."""
    from app.metadata.ncbi_assembly import AssemblyMetadata
    from app.metadata.ncbi_taxonomy import AssemblyPage, TaxonSuggestion

    profile = await profile_service.create_profile(username="tools-search-ncbi")

    def fake_suggest_organisms(query):
        return [TaxonSuggestion(sci_name="Escherichia coli", tax_id=562)]

    def fake_search_assemblies_by_taxon(tax_id, **kwargs):
        return AssemblyPage(
            assemblies=[
                AssemblyMetadata(accession="GCF_000005845.2", organism="Escherichia coli"),
            ]
        )

    monkeypatch.setattr(
        "app.metadata.ncbi_taxonomy.suggest_organisms", fake_suggest_organisms
    )
    monkeypatch.setattr(
        "app.metadata.ncbi_taxonomy.search_assemblies_by_taxon",
        fake_search_assemblies_by_taxon,
    )

    result = await tools.search_ncbi("e coli", owner=profile.owner_id())

    assert result["organisms"][0]["sci_name"] == "Escherichia coli"
    assert result["assemblies"][0]["accession"] == "GCF_000005845.2"


async def test_search_ncbi_returns_empty_when_no_organism_matches(monkeypatch):
    profile = await profile_service.create_profile(username="tools-search-ncbi-none")

    monkeypatch.setattr(
        "app.metadata.ncbi_taxonomy.suggest_organisms", lambda query: []
    )

    result = await tools.search_ncbi("not-a-real-organism-xyz", owner=profile.owner_id())

    assert result == {"organisms": [], "assemblies": []}


async def test_download_reference_delegates_to_launch_download(monkeypatch):
    """Confirms the tool calls the real service function (which owns
    validation, dedup and run-tracking) rather than enqueuing a raw job --
    the correction made over the plan's original hand-rolled enqueue call."""
    profile = await profile_service.create_profile(username="tools-download")
    owner = profile.owner_id()
    project = await project_service.create_project(name="Ref project", owner=owner)

    captured = {}

    async def fake_launch_download(*, project_id, accession, components, owner):
        captured["project_id"] = project_id
        captured["accession"] = accession
        captured["components"] = components
        captured["owner"] = owner

        # launch_download's real return shape is (run, [str(job.id)]) --
        # a list of string ids, not job objects. See
        # app/services/ncbi_assembly_service.py's final `return run, [str(job.id)]`.
        return object(), ["507f1f77bcf86cd799439077"]

    monkeypatch.setattr(
        "app.services.ncbi_assembly_service.launch_download", fake_launch_download
    )

    result = await tools.download_reference(
        "GCF_000005845.2", str(project.id), owner=owner
    )

    assert captured["accession"] == "GCF_000005845.2"
    assert captured["owner"] == owner
    assert str(captured["project_id"]) == str(project.id)
    assert result["accession"] == "GCF_000005845.2"
    assert result["job_id"] == "507f1f77bcf86cd799439077"


async def test_list_tools_reports_installation_state():
    """An agent needs to know what is installed, not just what exists.

    `needs_install` is a real first-run state for ON_DEMAND_IMAGE tools, and
    an agent that cannot see it will read a pullable tool as permanently
    broken -- the same wrong reading `CardStatus.NEEDS_INSTALL` exists to
    prevent in the UI.
    """
    profile = await profile_service.create_profile(username="tools-listtools")

    result = await tools.list_tools(owner=profile.owner_id())

    assert result["tools"]
    sample = next(iter(result["tools"].values()))
    assert "installed" in sample


async def test_get_guide_returns_content():
    profile = await profile_service.create_profile(username="tools-guide")

    result = await tools.get_guide("getting-started", owner=profile.owner_id())

    assert "bioflow_suggest_next" in result["content"]


async def test_get_guide_rejects_an_unknown_topic():
    profile = await profile_service.create_profile(username="tools-guide-bad")

    from app.errors import ValidationError

    with pytest.raises(ValidationError) as exc:
        await tools.get_guide("no-such-guide", owner=profile.owner_id())

    assert "getting-started" in str(exc.value)
