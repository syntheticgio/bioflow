"""The Node document's tri-state storage fields, against a real database.

R13 is the reason this file exists and it cannot be checked by reading the
model: a node whose stored document predates these fields must load with
`storage_shared is None` -- *never probed* -- rather than defaulting to
`False`, *probed and not shared*. The difference decides whether #846 can find
those nodes to probe them, and whether #845 silently withdraws work from nodes
that have been running it correctly all along. A default written the wrong way
round type-checks, reads as correct, and is only visible on a document that
was inserted before the field existed -- so the test inserts one.
"""

import pytest

from app.models.node import Node

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def test_a_new_node_has_never_been_probed():
    """A node nobody has probed reads as unknown, not as not-shared."""
    node = Node(node_id="storage-fields-new")
    await node.insert()

    found = await Node.find_one(Node.node_id == "storage-fields-new")

    assert found.storage_shared is None
    assert found.storage_location is None
    assert found.storage_checked_at is None


async def test_a_document_written_before_these_fields_existed_reads_as_unknown():
    """R13. The migration case, reproduced rather than assumed.

    Inserts a raw document with no storage keys at all -- what every node
    enrolled before #844 actually looks like on disk -- and loads it through
    the model.
    """
    collection = Node.get_pymongo_collection()
    await collection.insert_one(
        {
            "node_id": "storage-fields-legacy",
            "hostname": "legacy.local",
            "status": "active",
            "ssh_host": "192.168.1.99",
            "ssh_port": 22,
        }
    )

    found = await Node.find_one(Node.node_id == "storage-fields-legacy")

    assert found is not None
    # Unknown, so #846 can find it. Not False, which would be a claim nobody
    # made, and not True, which would bless a node nobody checked.
    assert found.storage_shared is None
    assert found.storage_checked_at is None
    assert found.storage_location is None


async def test_a_probed_node_round_trips_all_three_fields():
    """R16 holds in the shape the probe writes them: together."""
    from datetime import UTC, datetime

    checked = datetime.now(UTC)
    node = Node(
        node_id="storage-fields-probed",
        storage_location="/mnt/shared",
        storage_shared=True,
        storage_checked_at=checked,
    )
    await node.insert()

    found = await Node.find_one(Node.node_id == "storage-fields-probed")

    assert found.storage_shared is True
    assert found.storage_location == "/mnt/shared"
    assert found.storage_checked_at is not None


async def test_not_shared_is_distinguishable_from_never_probed():
    """The distinction the tri-state exists for, once written."""
    from datetime import UTC, datetime

    node = Node(
        node_id="storage-fields-unshared",
        storage_location="/mnt/local",
        storage_shared=False,
        storage_checked_at=datetime.now(UTC),
    )
    await node.insert()

    found = await Node.find_one(Node.node_id == "storage-fields-unshared")

    assert found.storage_shared is False
    assert found.storage_shared is not None
    # A probed-and-negative node carries a timestamp; an unprobed one does not.
    assert found.storage_checked_at is not None


async def test_unknown_nodes_are_findable_as_a_group():
    """#846 needs to select exactly the never-probed nodes to drain them."""
    await Node(node_id="storage-fields-unknown-a").insert()
    await Node(node_id="storage-fields-unknown-b").insert()

    unknown = await Node.find(Node.storage_shared == None).to_list()  # noqa: E711

    ids = {n.node_id for n in unknown}
    assert {"storage-fields-unknown-a", "storage-fields-unknown-b"} <= ids
