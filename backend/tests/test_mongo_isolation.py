"""The naming/sweep decision logic behind parallel-safe test databases.

Pure functions only -- the Mongo I/O that uses them lives in conftest.py and
is exercised by every DB-touching test in the suite.
"""

import re

from tests import _mongo_isolation as iso


class TestRunToken:
    def test_token_is_stable_within_a_process(self, monkeypatch):
        monkeypatch.delenv("BIOPIPE_TEST_RUN_TOKEN", raising=False)
        first = iso.ensure_run_token()
        assert iso.ensure_run_token() == first

    def test_token_respects_an_existing_env_value(self, monkeypatch):
        monkeypatch.setenv("BIOPIPE_TEST_RUN_TOKEN", "cafef00d")
        assert iso.ensure_run_token() == "cafef00d"

    def test_token_is_short_hex(self, monkeypatch):
        monkeypatch.delenv("BIOPIPE_TEST_RUN_TOKEN", raising=False)
        assert re.fullmatch(r"[0-9a-f]{8}", iso.ensure_run_token())


class TestDbNaming:
    def test_worker_db_name_uses_xdist_worker_id(self, monkeypatch):
        monkeypatch.setenv("BIOPIPE_TEST_RUN_TOKEN", "cafef00d")
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
        assert iso.worker_db_name() == "biopipe_test_cafef00d_gw3"

    def test_worker_db_name_without_xdist_uses_main(self, monkeypatch):
        monkeypatch.setenv("BIOPIPE_TEST_RUN_TOKEN", "cafef00d")
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        assert iso.worker_db_name() == "biopipe_test_cafef00d_main"

    def test_legacy_name_is_not_a_prefix_match(self, monkeypatch):
        # "biopipe_test" must never look like one of our run databases.
        monkeypatch.setenv("BIOPIPE_TEST_RUN_TOKEN", "cafef00d")
        assert not "biopipe_test".startswith(iso.run_prefix())


class TestDirectMongoUrl:
    def test_rewrites_compose_hostname(self):
        assert "127.0.0.1" in iso.direct_mongo_url("mongodb://mongo:27017")

    def test_strips_replica_set_and_forces_direct(self):
        url = iso.direct_mongo_url("mongodb://mongo:27017/?replicaSet=rs0")
        assert "replicaSet" not in url
        assert "directConnection=true" in url

    def test_leaves_direct_connection_alone_if_present(self):
        url = iso.direct_mongo_url("mongodb://h:27017/?directConnection=true")
        assert url.count("directConnection") == 1


class TestStaleSweep:
    NOW = 1_000_000.0

    def test_old_db_is_stale(self):
        names = ["biopipe_test_dead0000_gw0"]
        metas = {"biopipe_test_dead0000_gw0": self.NOW - 7201}
        assert iso.stale_test_dbs(names, metas, self.NOW, "biopipe_test_live0000_") == names

    def test_young_db_is_kept(self):
        names = ["biopipe_test_dead0000_gw0"]
        metas = {"biopipe_test_dead0000_gw0": self.NOW - 60}
        assert iso.stale_test_dbs(names, metas, self.NOW, "biopipe_test_live0000_") == []

    def test_db_without_meta_is_stale(self):
        names = ["biopipe_test_dead0000_gw0"]
        assert iso.stale_test_dbs(names, {}, self.NOW, "biopipe_test_live0000_") == names

    def test_own_run_is_never_swept_even_if_old(self):
        names = ["biopipe_test_live0000_gw0"]
        metas = {"biopipe_test_live0000_gw0": self.NOW - 99999}
        assert iso.stale_test_dbs(names, metas, self.NOW, "biopipe_test_live0000_") == []

    def test_legacy_and_unrelated_dbs_are_never_swept(self):
        names = ["biopipe_test", "biopipe", "admin", "local"]
        assert iso.stale_test_dbs(names, {}, self.NOW, "biopipe_test_live0000_") == []
