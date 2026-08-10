"""Tests for per-node Redis key helpers."""

from app.queue.keys import conc_key, node_conc_keys, ready_key


class TestReadyKey:
    def test_global_pool_when_none(self):
        assert ready_key(None) == "bp:q:ready"

    def test_global_pool_when_empty(self):
        assert ready_key("") == "bp:q:ready"

    def test_node_specific_key(self):
        assert ready_key("gpu-node-1") == "bp:q:ready:gpu-node-1"


class TestConcKey:
    def test_global_when_no_node(self):
        assert conc_key("cpu") == "bp:conc:cpu"
        assert conc_key("mem_mb") == "bp:conc:mem_mb"
        assert conc_key("io_heavy") == "bp:conc:io_heavy"

    def test_per_node_when_node_id(self):
        assert conc_key("cpu", "node2") == "bp:conc:cpu:node2"
        assert conc_key("mem_mb", "node2") == "bp:conc:mem_mb:node2"
        assert conc_key("io_heavy", "node2") == "bp:conc:io_heavy:node2"


class TestNodeConcKeys:
    def test_three_keys_in_order(self):
        keys = node_conc_keys("node2")
        assert keys == [
            "bp:conc:cpu:node2",
            "bp:conc:mem_mb:node2",
            "bp:conc:io_heavy:node2",
        ]

    def test_global_when_no_node(self):
        keys = node_conc_keys("")
        assert keys == ["bp:conc:cpu", "bp:conc:mem_mb", "bp:conc:io_heavy"]
