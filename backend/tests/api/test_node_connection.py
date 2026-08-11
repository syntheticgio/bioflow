"""Tests for node connection details endpoint."""

import pytest
from app.api.v1.nodes import _rewrite_host


class TestRewriteHost:
    def test_passes_through_already_routable_url(self):
        assert (
            _rewrite_host("mongodb://192.168.1.50:27017/db", "10.0.0.1")
            == "mongodb://192.168.1.50:27017/db"
        )

    def test_rewrites_mongo_service_name(self):
        rewritten = _rewrite_host(
            "mongodb://mongo:27017/biopipe?replicaSet=rs0&directConnection=true",
            "192.168.1.50",
        )
        # The host part was "mongo", now "192.168.1.50".
        # The scheme "mongodb" still contains the letters m-o-n-g-o — don't
        # confuse that with the hostname.
        assert "mongodb://192.168.1.50:27017" in rewritten
        # The standalone host "mongo" is gone.
        assert "://mongo:" not in rewritten

    def test_rewrites_redis_service_name(self):
        rewritten = _rewrite_host("redis://redis:6379/0", "192.168.1.50")
        assert rewritten == "redis://192.168.1.50:6379/0"

    def test_rewrites_redis_with_user_info(self):
        # "redis://:password@redis:6379/0" — the host is `redis` after `@`.
        rewritten = _rewrite_host("redis://:password@redis:6379/0", "10.0.0.1")
        assert rewritten == "redis://:password@10.0.0.1:6379/0"

    def test_empty_url_passes_through(self):
        assert _rewrite_host("", "10.0.0.1") == ""

    def test_does_not_rewrite_hostname_inside_path_or_query(self):
        url = "mongodb://mongo:27017/db?appName=redis-app"
        rewritten = _rewrite_host(url, "192.168.1.50")
        assert "redis-app" in rewritten
        assert "192.168.1.50" in rewritten
        # The hostname in the query string should NOT be rewritten.
        assert "appName=redis-app" in rewritten
