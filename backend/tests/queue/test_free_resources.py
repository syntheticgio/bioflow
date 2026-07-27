"""Admission headroom.

`claim.lua` reserves each job's declared cpu/mem into `bp:conc:*` and `release`
gives them back, but the worker used to compute headroom from a *count* of
running jobs and never read those counters. With every handler declaring cpu=1
the two agreed exactly, which is why the discrepancy stayed invisible;
`trim_reads` and `align_reads` declare the user's thread count, so they diverge.

The leak case has its own tests because it is the reason the original code
derived from a job count: counters can drift if a release is ever missed, and a
drifting counter that permanently shrinks capacity is a worse failure than the
one being fixed.
"""

import pytest

from app.queue.worker import IO_HEAVY_LIMIT, _as_int, compute_free_resources


def free(**kwargs):
    base = {
        "cpu_budget": 16,
        "mem_mb": 8192,
        "reserved_cpu": 0,
        "reserved_io_heavy": 0,
        "in_flight": 0,
    }
    return compute_free_resources(**{**base, **kwargs})


class TestCpuHeadroom:
    def test_an_idle_worker_offers_the_whole_budget(self):
        assert free()["cpu"] == 16

    def test_reservations_are_subtracted_by_weight_not_by_count(self):
        """The actual bug. One 12-thread alignment must leave 4 CPUs of
        headroom, not 15 -- which is what subtracting a job count gave."""
        assert free(reserved_cpu=12, in_flight=1)["cpu"] == 4

    def test_many_small_jobs_and_one_large_job_differ(self):
        """Four single-CPU jobs and one four-CPU job reserve the same total, so
        they must produce the same headroom. Under the old count-based maths
        these were 12 and 15."""
        four_small = free(reserved_cpu=4, in_flight=4)["cpu"]
        one_large = free(reserved_cpu=4, in_flight=1)["cpu"]
        assert four_small == one_large == 12

    def test_never_offers_less_than_one_cpu(self):
        """A fully-reserved queue must still drain. Offering zero would let the
        queue deadlock against its own bookkeeping."""
        assert free(reserved_cpu=999, in_flight=3)["cpu"] == 1


class TestIoHeavyCap:
    def test_light_jobs_do_not_consume_heavy_capacity(self):
        """The second recorded bug: `io_heavy` counted *every* running job, so
        four trivial light jobs drove heavy capacity to zero and starved a trim
        or alignment entirely."""
        assert free(in_flight=4, reserved_io_heavy=0)["io_heavy"] == IO_HEAVY_LIMIT

    def test_heavy_jobs_consume_it(self):
        assert free(in_flight=1, reserved_io_heavy=1)["io_heavy"] == 1

    def test_the_cap_holds(self):
        assert free(in_flight=2, reserved_io_heavy=2)["io_heavy"] == 0

    def test_never_negative(self):
        assert free(in_flight=5, reserved_io_heavy=9)["io_heavy"] == 0


class TestLeakedReservations:
    """A crashed worker's reservations must not permanently shrink capacity."""

    def test_an_idle_worker_ignores_leaked_counters(self):
        """The self-healing property. A worker running nothing cannot still owe
        any reservation, so a counter left high by a crash is disregarded
        rather than blocking this worker forever."""
        assert free(reserved_cpu=16, reserved_io_heavy=2, in_flight=0) == {
            "cpu": 16,
            "mem_mb": 8192,
            "io_heavy": IO_HEAVY_LIMIT,
        }

    def test_a_busy_worker_still_respects_counters(self):
        """The clamp must not become a way to ignore real reservations: a
        worker with jobs in flight honours them."""
        assert free(reserved_cpu=10, in_flight=2)["cpu"] == 6

    def test_recovery_does_not_require_the_counter_to_be_corrected(self):
        """Draining is enough. Once the jobs finish, full headroom returns even
        if the counter is still wrong -- so a leak costs throughput while busy
        and nothing at all once idle."""
        while_busy = free(reserved_cpu=99, in_flight=1)["cpu"]
        once_idle = free(reserved_cpu=99, in_flight=0)["cpu"]
        assert while_busy == 1
        assert once_idle == 16


class TestCounterParsing:
    @pytest.mark.parametrize("raw,expected", [("4", 4), (b"4", 4), (7, 7)])
    def test_reads_redis_scalars(self, raw, expected):
        assert _as_int(raw) == expected

    def test_missing_counter_reads_as_zero(self):
        """An unset key means nothing has been reserved yet, not an error."""
        assert _as_int(None) == 0

    def test_garbage_reads_as_zero(self):
        assert _as_int("not-a-number") == 0

    def test_a_negative_counter_is_clamped(self):
        """A double release drives a counter below zero. Left negative it would
        read as *extra* free capacity and cause over-admission -- turning a
        bookkeeping slip into an overloaded machine."""
        assert _as_int(-5) == 0
