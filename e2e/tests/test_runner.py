import asyncio

from e2e.backend import runner
from e2e.backend.config import Config
from e2e.backend.model import Step, Test
from e2e.backend.store import ResultStore


class FakeMcp:
    def __init__(self, base_url, profile=""):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        pass

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "create_project":
            return {"id": "proj1", "name": arguments["name"]}
        if name == "whoami":
            return {"owner": "p1"}
        if name == "get_job":
            return {"job_id": arguments.get("job_id"), "state": "succeeded"}
        return {}


def _run(monkeypatch, tmp_path, tests, names):
    monkeypatch.setattr(runner, "McpClient", FakeMcp)
    store = ResultStore(str(tmp_path))

    async def go():
        await store.create_run("r1", {"tests": names})
        await runner.run_batch("r1", store, Config(), tmp_path / "fixtures", tests, names)
        return await store.get_run("r1")

    return asyncio.run(go())


def test_passing_yaml_test_with_ref_resolution(monkeypatch, tmp_path):
    tests = [Test(name="t1", kind="yaml", steps=[
        Step("create_project", {"name": "x"}),
        Step("mcp", {"tool": "whoami", "args": {}}),
        Step("assert", {"fact": "$.project_id", "equals": "proj1"}),
    ])]
    run = _run(monkeypatch, tmp_path, tests, ["t1"])
    assert run["status"] == "passed"
    assert run["tests"][0]["status"] == "passed"
    assert len(run["tests"][0]["steps"]) == 3


def test_failing_step_stops_test(monkeypatch, tmp_path):
    tests = [Test(name="t1", kind="yaml", steps=[
        Step("create_project", {"name": "x"}),
        Step("assert", {"fact": "$.project_id", "equals": "WRONG"}),
        Step("mcp", {"tool": "whoami", "args": {}}),  # must not run
    ])]
    run = _run(monkeypatch, tmp_path, tests, ["t1"])
    assert run["status"] == "failed"
    assert len(run["tests"][0]["steps"]) == 2  # third step never started


def test_continue_across_tests(monkeypatch, tmp_path):
    tests = [
        Test(name="fail", kind="yaml", steps=[Step("assert", {"fact": "x", "equals": "y"})]),
        Test(name="pass", kind="yaml", steps=[Step("mcp", {"tool": "whoami", "args": {}})]),
    ]
    run = _run(monkeypatch, tmp_path, tests, ["fail", "pass"])
    assert run["status"] == "failed"
    by_name = {t["name"]: t for t in run["tests"]}
    assert by_name["fail"]["status"] == "failed"
    assert by_name["pass"]["status"] == "passed"


def test_unresolved_ref_fails_step(monkeypatch, tmp_path):
    tests = [Test(name="t1", kind="yaml", steps=[
        Step("mcp", {"tool": "whoami", "args": {"x": "$.missing"}}),
    ])]
    run = _run(monkeypatch, tmp_path, tests, ["t1"])
    assert run["status"] == "failed"
    assert "unresolved reference" in run["tests"][0]["steps"][0]["error"]


# ---- launch / find steps (the reads path) ----


def _card(kind, status="available", endpoint="/pipelines/x", body=None, reason=None):
    return {
        "kind": kind,
        "status": status,
        "reason": reason,
        "launch": (
            {"endpoint": endpoint, "body": body if body is not None else {"o": 1}}
            if status != "unavailable"
            else None
        ),
    }


def _patch_launch(monkeypatch, sink):
    async def fake_launch(base_url, profile, endpoint, body):
        sink.append((endpoint, body))
        return {"id": "job1", "state": "queued"}

    monkeypatch.setattr(runner, "launch_pipeline", fake_launch)


def test_launch_posts_the_card_body_verbatim(monkeypatch, tmp_path):
    """The body must be the card's, not one the test rebuilt."""
    sent = []
    _patch_launch(monkeypatch, sent)
    suggestions = {"suggestions": [_card("align", endpoint="/pipelines/align",
                                         body={"object_id": "o1", "reference_id": "r1"})]}
    tests = [Test(name="t1", kind="yaml", steps=[
        Step("mcp", {"tool": "suggest_next", "as": "suggest", "args": {}}),
        Step("launch", {"kind": "align", "from": "$.suggest", "as": "al"}),
        Step("assert", {"fact": "$.al.id", "equals": "job1"}),
    ])]

    class Mcp(FakeMcp):
        async def call_tool(self, name, arguments):
            if name == "suggest_next":
                return suggestions
            return await super().call_tool(name, arguments)

    monkeypatch.setattr(runner, "McpClient", Mcp)
    store = ResultStore(str(tmp_path))

    async def go():
        await store.create_run("r1", {"tests": ["t1"]})
        await runner.run_batch("r1", store, Config(), tmp_path, tests, ["t1"])
        return await store.get_run("r1")

    run = asyncio.run(go())
    assert run["status"] == "passed", run["tests"][0]["steps"]
    assert sent == [("/pipelines/align", {"object_id": "o1", "reference_id": "r1"})]


def test_launch_reports_an_unavailable_cards_reason(monkeypatch, tmp_path):
    """The card's own reason is the failure message worth surfacing."""
    _patch_launch(monkeypatch, [])
    suggestions = {"suggestions": [
        _card("align", status="unavailable", reason="No reference is available.")
    ]}
    tests = [Test(name="t1", kind="yaml", steps=[
        Step("mcp", {"tool": "suggest_next", "as": "suggest", "args": {}}),
        Step("launch", {"kind": "align", "from": "$.suggest"}),
    ])]

    class Mcp(FakeMcp):
        async def call_tool(self, name, arguments):
            if name == "suggest_next":
                return suggestions
            return await super().call_tool(name, arguments)

    monkeypatch.setattr(runner, "McpClient", Mcp)
    store = ResultStore(str(tmp_path))

    async def go():
        await store.create_run("r1", {"tests": ["t1"]})
        await runner.run_batch("r1", store, Config(), tmp_path, tests, ["t1"])
        return await store.get_run("r1")

    run = asyncio.run(go())
    assert run["status"] == "failed"
    assert "No reference is available." in run["tests"][0]["steps"][1]["error"]


def test_launch_missing_card_names_what_was_there(monkeypatch, tmp_path):
    _patch_launch(monkeypatch, [])
    suggestions = {"suggestions": [_card("preprocess")]}
    tests = [Test(name="t1", kind="yaml", steps=[
        Step("mcp", {"tool": "suggest_next", "as": "suggest", "args": {}}),
        Step("launch", {"kind": "align", "from": "$.suggest"}),
    ])]

    class Mcp(FakeMcp):
        async def call_tool(self, name, arguments):
            if name == "suggest_next":
                return suggestions
            return await super().call_tool(name, arguments)

    monkeypatch.setattr(runner, "McpClient", Mcp)
    store = ResultStore(str(tmp_path))

    async def go():
        await store.create_run("r1", {"tests": ["t1"]})
        await runner.run_batch("r1", store, Config(), tmp_path, tests, ["t1"])
        return await store.get_run("r1")

    run = asyncio.run(go())
    assert run["status"] == "failed"
    err = run["tests"][0]["steps"][1]["error"]
    assert "no 'align' card" in err and "preprocess" in err


def test_launch_explicit_endpoint_form(monkeypatch, tmp_path):
    """QC has no card, so the endpoint/body form must work on its own."""
    sent = []
    _patch_launch(monkeypatch, sent)
    tests = [Test(name="t1", kind="yaml", steps=[
        Step("create_project", {"name": "x"}),
        Step("launch", {"endpoint": "/pipelines/qc",
                        "body": {"object_id": "$.project_id"}, "as": "qc"}),
        Step("assert", {"fact": "$.qc.id", "equals": "job1"}),
    ])]
    run = _run(monkeypatch, tmp_path, tests, ["t1"])
    assert run["status"] == "passed"
    assert sent == [("/pipelines/qc", {"object_id": "proj1"})]


def test_find_selects_one_object_by_field(monkeypatch, tmp_path):
    tests = [Test(name="t1", kind="yaml", steps=[
        Step("mcp", {"tool": "list_objects", "as": "objs", "args": {}}),
        Step("find", {"in": "$.objs.objects", "where": {"format": "bam"}, "as": "bam"}),
        Step("assert", {"fact": "$.bam.id", "equals": "b1"}),
    ])]

    class Mcp(FakeMcp):
        async def call_tool(self, name, arguments):
            if name == "list_objects":
                return {"objects": [
                    {"id": "f1", "format": "fastq"},
                    {"id": "b1", "format": "bam"},
                ]}
            return await super().call_tool(name, arguments)

    monkeypatch.setattr(runner, "McpClient", Mcp)
    store = ResultStore(str(tmp_path))

    async def go():
        await store.create_run("r1", {"tests": ["t1"]})
        await runner.run_batch("r1", store, Config(), tmp_path, tests, ["t1"])
        return await store.get_run("r1")

    run = asyncio.run(go())
    assert run["status"] == "passed", run["tests"][0]["steps"]


def test_find_refuses_an_ambiguous_match(monkeypatch, tmp_path):
    """Two BAMs where one is expected means the flow ran twice -- don't hide it."""
    tests = [Test(name="t1", kind="yaml", steps=[
        Step("mcp", {"tool": "list_objects", "as": "objs", "args": {}}),
        Step("find", {"in": "$.objs.objects", "where": {"format": "bam"}}),
    ])]

    class Mcp(FakeMcp):
        async def call_tool(self, name, arguments):
            if name == "list_objects":
                return {"objects": [
                    {"id": "b1", "format": "bam"},
                    {"id": "b2", "format": "bam"},
                ]}
            return await super().call_tool(name, arguments)

    monkeypatch.setattr(runner, "McpClient", Mcp)
    store = ResultStore(str(tmp_path))

    async def go():
        await store.create_run("r1", {"tests": ["t1"]})
        await runner.run_batch("r1", store, Config(), tmp_path, tests, ["t1"])
        return await store.get_run("r1")

    run = asyncio.run(go())
    assert run["status"] == "failed"
    assert "2 items match" in run["tests"][0]["steps"][1]["error"]


def test_assert_at_least_compares_numbers(monkeypatch, tmp_path):
    passing = [Test(name="t1", kind="yaml", steps=[
        Step("mcp", {"tool": "get_object", "as": "o", "args": {}}),
        Step("assert", {"fact": "$.o.metadata.mapped_pct", "at_least": 90}),
    ])]
    failing = [Test(name="t2", kind="yaml", steps=[
        Step("mcp", {"tool": "get_object", "as": "o", "args": {}}),
        Step("assert", {"fact": "$.o.metadata.mapped_pct", "at_least": 99.9}),
    ])]

    class Mcp(FakeMcp):
        async def call_tool(self, name, arguments):
            if name == "get_object":
                return {"metadata": {"mapped_pct": 97.5}}
            return await super().call_tool(name, arguments)

    monkeypatch.setattr(runner, "McpClient", Mcp)

    def go(tests, names):
        store = ResultStore(str(tmp_path / names[0]))

        async def inner():
            await store.create_run("r", {"tests": names})
            await runner.run_batch("r", store, Config(), tmp_path, tests, names)
            return await store.get_run("r")

        return asyncio.run(inner())

    assert go(passing, ["t1"])["status"] == "passed"
    failed = go(failing, ["t2"])
    assert failed["status"] == "failed"
    assert "97.5 < 99.9" in failed["tests"][0]["steps"][1]["error"]


def test_wait_can_poll_a_non_state_field(monkeypatch, tmp_path):
    """Objects are awaited on `status: ready`, not a job's terminal `state`."""
    seen = {"n": 0}

    class Mcp(FakeMcp):
        async def call_tool(self, name, arguments):
            if name == "get_object":
                seen["n"] += 1
                # ingesting first, ready on the second poll
                return {"status": "ready" if seen["n"] > 1 else "ingesting"}
            return await super().call_tool(name, arguments)

    tests = [Test(name="t1", kind="yaml", steps=[
        Step("wait", {"tool": "get_object", "args": {}, "field": "status",
                      "until": "ready", "timeout": 30, "poll": 0}),
    ])]
    monkeypatch.setattr(runner, "McpClient", Mcp)
    store = ResultStore(str(tmp_path))

    async def go():
        await store.create_run("r1", {"tests": ["t1"]})
        await runner.run_batch("r1", store, Config(), tmp_path, tests, ["t1"])
        return await store.get_run("r1")

    run = asyncio.run(go())
    assert run["status"] == "passed"
    assert seen["n"] == 2, "should have polled past the ingesting state"


def test_wait_on_status_fails_fast_on_error(monkeypatch, tmp_path):
    """A file that failed to ingest will never become ready; don't wait it out."""

    class Mcp(FakeMcp):
        async def call_tool(self, name, arguments):
            if name == "get_object":
                return {"status": "error"}
            return await super().call_tool(name, arguments)

    tests = [Test(name="t1", kind="yaml", steps=[
        Step("wait", {"tool": "get_object", "args": {}, "field": "status",
                      "until": "ready", "timeout": 30, "poll": 0}),
    ])]
    monkeypatch.setattr(runner, "McpClient", Mcp)
    store = ResultStore(str(tmp_path))

    async def go():
        await store.create_run("r1", {"tests": ["t1"]})
        await runner.run_batch("r1", store, Config(), tmp_path, tests, ["t1"])
        return await store.get_run("r1")

    run = asyncio.run(go())
    assert run["status"] == "failed"
    assert "status=error" in run["tests"][0]["steps"][0]["error"]


def test_get_object_awaits_a_late_fact(monkeypatch, tmp_path):
    """`mapped_pct` is written by index_bam, after the align job succeeds."""
    calls = {"n": 0}

    async def fake_get(base_url, profile, object_id):
        calls["n"] += 1
        facts = {"mapped_pct": 99.1} if calls["n"] > 2 else {}
        return {"id": object_id, "facts": facts}

    monkeypatch.setattr(runner, "get_object", fake_get)
    tests = [Test(name="t1", kind="yaml", steps=[
        Step("get_object", {"object_id": "o1", "as": "bam",
                            "await_fact": "mapped_pct", "timeout": 30, "poll": 0}),
        Step("assert", {"fact": "$.bam.facts.mapped_pct", "at_least": 90}),
    ])]
    run = _run(monkeypatch, tmp_path, tests, ["t1"])
    assert run["status"] == "passed"
    assert calls["n"] == 3, "should have polled until the fact appeared"


def test_get_object_reports_which_fact_never_arrived(monkeypatch, tmp_path):
    async def fake_get(base_url, profile, object_id):
        return {"id": object_id, "facts": {"has_index": True}}

    monkeypatch.setattr(runner, "get_object", fake_get)
    tests = [Test(name="t1", kind="yaml", steps=[
        Step("get_object", {"object_id": "o1", "await_fact": "mapped_pct",
                            "timeout": 0, "poll": 0}),
    ])]
    run = _run(monkeypatch, tmp_path, tests, ["t1"])
    assert run["status"] == "failed"
    err = run["tests"][0]["steps"][0]["error"]
    assert "mapped_pct" in err and "has_index" in err


def test_patch_sends_the_body(monkeypatch, tmp_path):
    sent = []

    async def fake_patch(base_url, profile, object_id, body):
        sent.append((object_id, body))
        return {"id": object_id, "role": body.get("role")}

    monkeypatch.setattr(runner, "patch_object", fake_patch)
    tests = [Test(name="t1", kind="yaml", steps=[
        Step("patch", {"object_id": "o1", "body": {"role": "reference"}}),
        Step("assert", {"fact": "$.patched.role", "equals": "reference"}),
    ])]
    run = _run(monkeypatch, tmp_path, tests, ["t1"])
    assert run["status"] == "passed"
    assert sent == [("o1", {"role": "reference"})]
