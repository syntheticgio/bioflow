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
