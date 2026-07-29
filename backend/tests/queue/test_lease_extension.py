"""Lease extension: a handler declaring it needs longer than the default.

The heartbeat renews every in-flight job on a fixed interval, which covers a
merely *slow* job. It does not cover a paused VM or a stalled loop -- the
laptop-lid case reap_expired.lua exists for. A handler that asked for an hour
and silently got the 30s default is one lid-close away from being reaped and
double-run, which is why these callers are not decorative.
"""

import pytest

from app.queue.registry import JobContext


class TestExtendLease:
    def test_defaults_to_no_override(self):
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        assert ctx.lease_override_seconds is None

    def test_records_the_requested_seconds(self):
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        ctx.extend_lease(3600)
        assert ctx.lease_override_seconds == 3600

    def test_keeps_the_longest_request(self):
        """A handler with several long phases must not shorten its own lease by
        asking for less on a later phase than it did on an earlier one."""
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        ctx.extend_lease(3600)
        ctx.extend_lease(60)
        assert ctx.lease_override_seconds == 3600

    def test_ignores_a_nonpositive_request(self):
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        ctx.extend_lease(0)
        ctx.extend_lease(-5)
        assert ctx.lease_override_seconds is None

    def test_still_invokes_the_callback_when_one_is_set(self):
        """The callback stays supported so the worker can react immediately
        rather than waiting for the next heartbeat tick."""
        seen = []
        ctx = JobContext(job_id="j1", payload={}, epoch=0, attempts=0)
        ctx._extend_cb = seen.append
        ctx.extend_lease(120)
        assert seen == [120]
