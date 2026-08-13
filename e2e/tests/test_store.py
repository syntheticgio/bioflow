import asyncio

from e2e.backend.store import ResultStore


def test_run_round_trip(tmp_path):
    async def go():
        s = ResultStore(str(tmp_path))
        await s.create_run("r1", {"tests": ["a"]})
        await s.start_step("r1", "a", 0, "create_project")
        await s.finish_step("r1", "a", 0, "passed", 10, "", None, '{"id": "p1"}')
        await s.start_step("r1", "a", 1, "assert")
        await s.finish_step("r1", "a", 1, "failed", 5, "", "boom", None)
        await s.finish_run("r1", "failed", "err")
        return await s.get_run("r1")

    run = asyncio.run(go())
    assert run["status"] == "failed"
    assert run["error"] == "err"
    assert run["tests"][0]["name"] == "a"
    assert run["tests"][0]["status"] == "failed"
    assert len(run["tests"][0]["steps"]) == 2
    assert run["tests"][0]["steps"][0]["result"] == {"id": "p1"}
    assert run["tests"][0]["steps"][1]["error"] == "boom"


def test_list_and_delete_cascades(tmp_path):
    async def go():
        s = ResultStore(str(tmp_path))
        await s.create_run("r1", {"tests": []})
        await s.start_step("r1", "a", 0, "mcp")
        await s.finish_step("r1", "a", 0, "passed", 1, "", None, None)
        await s.finish_run("r1", "passed")
        runs = await s.list_runs()
        await s.delete_run("r1")
        return runs, await s.get_run("r1"), await s.list_runs()

    runs, after, remaining = asyncio.run(go())
    assert len(runs) == 1
    assert after is None
    assert remaining == []
