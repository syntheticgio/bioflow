"""Round-trip coverage for NodeUpdateTask against a real database.

Guards against the exact bug found while adding this model:
NodeProvisionTask originally shipped a bare-string Settings.indexes entry,
which crashes db.index_reconcile.reconcile_indexes() with
`AttributeError: 'str' object has no attribute 'document'` the moment the
model is registered and its indexes are reconciled. NodeUpdateTask avoids
that by relying on field-level `Indexed(...)` annotations only (task_id
unique, node_id non-unique) and carrying no Settings.indexes at all. This
test proves the model actually initializes and both indexed fields are
usable for lookups, not just that the class is importable.
"""

import pytest

from app.models import NodeUpdateTask

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


async def test_insert_and_query_by_task_id():
    task = NodeUpdateTask(node_id="node-a", host="node-a.local")
    await task.insert()

    found = await NodeUpdateTask.find_one(NodeUpdateTask.task_id == task.task_id)
    assert found is not None
    assert found.node_id == "node-a"
    assert found.status == "updating"
    assert found.drain is True

    await found.delete()
    assert await NodeUpdateTask.find_one(NodeUpdateTask.task_id == task.task_id) is None


async def test_query_by_node_id_returns_update_history():
    node_id = "node-history"
    await NodeUpdateTask(node_id=node_id, status="success").insert()
    await NodeUpdateTask(node_id=node_id, status="failed").insert()
    await NodeUpdateTask(node_id="node-other", status="success").insert()

    results = await NodeUpdateTask.find(NodeUpdateTask.node_id == node_id).to_list()
    assert len(results) == 2
    assert {r.status for r in results} == {"success", "failed"}


async def test_task_id_uniqueness_is_enforced():
    from pymongo.errors import DuplicateKeyError

    task = NodeUpdateTask(node_id="node-dup")
    await task.insert()

    dup = NodeUpdateTask(task_id=task.task_id, node_id="node-dup")
    with pytest.raises(DuplicateKeyError):
        await dup.insert()
