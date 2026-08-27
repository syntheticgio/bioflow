"""Peak CPU and memory for one job, sampled from its process subtree.

Polling rather than `resource.getrusage(RUSAGE_CHILDREN)`, which gives an
exact kernel high-water mark but is cumulative across every child of the
worker -- and the governor admits several jobs at once, so a getrusage number
cannot be attributed to one of them. Polling a subtree keeps concurrent jobs
separable, which is the property that matters more than exactness here.

The numbers replace hand-tuned coefficients in `pipelines/resource_estimator.py`
that came from published tool documentation rather than measurement, so
"within a few percent" is comfortably good enough.
"""

import psutil

from app.logging import get_logger

log = get_logger(__name__)


class ResourceSampler:
    """Accumulates peak and mean resource use across repeated observations.

    Holds no timer of its own: the caller decides when to `observe()`. That
    keeps the arithmetic testable without spawning processes or sleeping.

    Tracks job-specific subprocess PIDs rather than the worker PID, so
    concurrent jobs record only their own process tree's resource usage.
    """

    def __init__(self, pid: int):
        # Fallback worker PID used when no job-specific subprocesses have
        # been registered yet (early sampling window before the first
        # run_subprocess call).
        self.pid = pid
        # Job-specific subprocess PIDs to sample from. When non-empty, these
        # replace `self.pid` as the roots to walk -- each PID and its
        # descendants are summed, keeping concurrent jobs separable.
        self._subprocess_pids: set[int] = set()
        self.peak_rss_bytes: int | None = None
        self.peak_cpu_percent: float | None = None
        # The most recent reading, alongside the peak. A peak alone cannot
        # answer "what is this job doing right now" once the job has passed
        # its high point -- which is exactly the question live progress asks.
        self.last_rss_bytes: int | None = None
        self.last_cpu_percent: float | None = None
        self.sample_count = 0
        self._cpu_total = 0.0
        # `psutil.Process.children()` returns brand-new Process objects on
        # every call, and `cpu_percent(interval=None)` reports the delta
        # *since the previous call on that exact instance* -- unconditionally
        # 0.0 on an instance's first call. Without persisting one Process per
        # child pid across polls, every child's cpu_percent would read 0.0 on
        # every single sample, forever, which is exactly where the real
        # bioinformatics tool (bwa, minimap2, fastp, ...) runs: as a child of
        # the worker, not as the root.
        # Keyed by (root_pid, child_pid) tuples to support multiple roots.
        self._child_procs: dict[tuple[int, int], object] = {}

    def track_subprocess(self, pid: int) -> None:
        """Register a subprocess PID belonging to this job.

        Once any subprocess PIDs are registered, observe() walks from each
        one instead of from the worker PID, keeping concurrent jobs' resource
        usage separable.
        """
        self._subprocess_pids.add(pid)

    @property
    def mean_cpu_percent(self) -> float | None:
        if self.sample_count == 0:
            return None
        return self._cpu_total / self.sample_count

    def _sample_root(self, root_pid: int) -> tuple[int, float] | None:
        """Sample one root PID and its descendants. Returns (rss, cpu) or None."""
        try:
            root_proc = psutil.Process(root_pid)
            rss, cpu = self._read(root_proc)
        except Exception as e:  # noqa: BLE001
            log.debug("resource_sample_root_gone", pid=root_pid, error=str(e))
            return None

        try:
            seen_pids = set()
            for child in root_proc.children(recursive=True):
                child_pid = child.pid
                seen_pids.add(child_pid)
                key = (root_pid, child_pid)
                tracked = self._child_procs.get(key, child)
                try:
                    child_rss, child_cpu = self._read(tracked)
                except Exception as e:  # noqa: BLE001 - this child exited mid-walk
                    log.debug("resource_sample_child_gone", pid=root_pid, error=str(e))
                    self._child_procs.pop(key, None)
                    continue
                self._child_procs[key] = tracked
                rss += child_rss
                cpu += child_cpu
            # Drop persisted entries for pids that no longer appear under this
            # root -- they exited (or were reaped) between polls.
            for stale_key in set(self._child_procs) - {(root_pid, cp) for cp in seen_pids}:
                if stale_key[0] == root_pid:
                    self._child_procs.pop(stale_key, None)
        except Exception as e:  # noqa: BLE001 - the root exited during the walk
            log.debug("resource_sample_walk_interrupted", pid=root_pid, error=str(e))

        return rss, cpu

    def observe(self, proc=None) -> None:
        """Take one reading of the subtree. Never raises.

        When job-specific subprocess PIDs are registered (via track_subprocess),
        samples from each one and its descendants. Falls back to the worker PID
        if no subprocess PIDs have been registered yet.

        When a `proc` argument is passed (as the sampler's own persistent
        psutil.Process instance, constructed once in the executor), it is
        used as the sole root instead of the PID-based lookup. This preserves
        backward compatibility with tests and the cpu_percent persistence
        across polls.

        A process disappearing mid-walk is the normal case, not an error: a
        pipeline spawns and reaps children constantly. Whatever was readable
        at this instant is a valid sample.
        """
        if proc is not None:
            # Legacy path: caller supplied a persistent psutil.Process.
            # Used by the executor's _sample_resources loop, which constructs
            # one Process instance per job and reuses it across polls so that
            # cpu_percent(interval=None) works correctly.
            try:
                rss, cpu = self._read(proc)
            except Exception as e:  # noqa: BLE001
                log.debug("resource_sample_root_gone", pid=self.pid, error=str(e))
                return

            try:
                seen_pids = set()
                for child in proc.children(recursive=True):
                    child_pid = child.pid
                    seen_pids.add(child_pid)
                    key = (self.pid, child_pid)
                    tracked = self._child_procs.get(key, child)
                    try:
                        child_rss, child_cpu = self._read(tracked)
                    except Exception as e:  # noqa: BLE001
                        log.debug("resource_sample_child_gone", pid=self.pid, error=str(e))
                        self._child_procs.pop(key, None)
                        continue
                    self._child_procs[key] = tracked
                    rss += child_rss
                    cpu += child_cpu
                for stale_key in set(self._child_procs) - {(self.pid, cp) for cp in seen_pids}:
                    if stale_key[0] == self.pid:
                        self._child_procs.pop(stale_key, None)
            except Exception as e:  # noqa: BLE001
                log.debug("resource_sample_walk_interrupted", pid=self.pid, error=str(e))

            self.sample_count += 1
            self._cpu_total += cpu
            self.last_rss_bytes = rss
            self.last_cpu_percent = cpu
            if self.peak_rss_bytes is None or rss > self.peak_rss_bytes:
                self.peak_rss_bytes = rss
            if self.peak_cpu_percent is None or cpu > self.peak_cpu_percent:
                self.peak_cpu_percent = cpu
            return

        # New path: sample from registered subprocess PIDs, or fall back to
        # the worker PID. Used when the sampler runs independently of the
        # executor's persistent Process (not currently reached in production
        # but available for future use).
        roots = list(self._subprocess_pids) if self._subprocess_pids else [self.pid]

        total_rss = 0
        total_cpu = 0.0
        any_succeeded = False

        for root_pid in roots:
            result = self._sample_root(root_pid)
            if result is not None:
                total_rss += result[0]
                total_cpu += result[1]
                any_succeeded = True

        if not any_succeeded:
            log.debug("resource_sample_all_roots_gone", pids=roots)
            return

        self.sample_count += 1
        self._cpu_total += total_cpu
        self.last_rss_bytes = total_rss
        self.last_cpu_percent = total_cpu
        if self.peak_rss_bytes is None or total_rss > self.peak_rss_bytes:
            self.peak_rss_bytes = total_rss
        if self.peak_cpu_percent is None or total_cpu > self.peak_cpu_percent:
            self.peak_cpu_percent = total_cpu

    @staticmethod
    def _read(proc) -> tuple[int, float]:
        return proc.memory_info().rss, proc.cpu_percent(interval=None)
