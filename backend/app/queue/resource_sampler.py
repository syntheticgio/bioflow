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
    """

    def __init__(self, pid: int):
        self.pid = pid
        self.peak_rss_bytes: int | None = None
        self.peak_cpu_percent: float | None = None
        self.sample_count = 0
        self._cpu_total = 0.0

    @property
    def mean_cpu_percent(self) -> float | None:
        if self.sample_count == 0:
            return None
        return self._cpu_total / self.sample_count

    def observe(self, proc=None) -> None:
        """Take one reading of the subtree. Never raises.

        A process disappearing mid-walk is the normal case, not an error: a
        pipeline spawns and reaps children constantly. Whatever was readable
        at this instant is a valid sample.
        """
        try:
            proc = proc if proc is not None else psutil.Process(self.pid)
            rss, cpu = self._read(proc)
        except Exception as e:  # noqa: BLE001 - the root is gone; no sample exists
            log.debug("resource_sample_root_gone", pid=self.pid, error=str(e))
            return

        try:
            for child in proc.children(recursive=True):
                try:
                    child_rss, child_cpu = self._read(child)
                except Exception as e:  # noqa: BLE001 - this child exited mid-walk
                    log.debug("resource_sample_child_gone", pid=self.pid, error=str(e))
                    continue
                rss += child_rss
                cpu += child_cpu
        except Exception as e:  # noqa: BLE001 - the root exited during the walk
            log.debug("resource_sample_walk_interrupted", pid=self.pid, error=str(e))

        self.sample_count += 1
        self._cpu_total += cpu
        if self.peak_rss_bytes is None or rss > self.peak_rss_bytes:
            self.peak_rss_bytes = rss
        if self.peak_cpu_percent is None or cpu > self.peak_cpu_percent:
            self.peak_cpu_percent = cpu

    @staticmethod
    def _read(proc) -> tuple[int, float]:
        return proc.memory_info().rss, proc.cpu_percent(interval=None)
