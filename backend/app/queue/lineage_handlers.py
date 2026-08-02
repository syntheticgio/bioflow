"""Downloading a compleasm lineage dataset.

Modelled on the NCBI download handlers rather than on a pipeline handler: this
fetches reference data shared across every project, not something derived
from one object. `assess_completeness` depends on this rather than fetching
inline for the reason Clair3's baked-in models exist -- a completeness job
must not depend on the network, so what it reads must already be present.

There is no applier: a successful run leaves files under `settings.
lineages_dir` and nothing else changes state. `assess_completeness` checks
for their presence directly rather than through a DataObject, since a
lineage dataset is not a project artifact and has no owner.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import completeness_runner, tools
from app.queue import download_failures
from app.queue.executor import run_subprocess
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)


def lineage_present(library_path: Path, lineage: str, odb: str) -> bool:
    """Whether this lineage's dataset has already been downloaded.

    compleasm's own download_lineage writes `<lineage>_<odb>.done` as its
    completion marker (verified against a real download on 2026-08-02) --
    checking for that file rather than the lineage directory itself, since a
    download that died mid-extraction leaves a partial directory with no
    marker, and this must report that as absent rather than present.
    """
    return (library_path / f"{lineage}_{odb}.done").is_file()


@handler(
    "download_lineage",
    mode=HandlerMode.SUBPROCESS,
    # USER_INTERACTIVE: someone chose a lineage and is waiting on the
    # completeness card to become launchable, the same reasoning
    # download_assembly gives for its own tier.
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
    # Matches download_assembly: a failed download is usually the network,
    # and a third attempt often succeeds.
    max_attempts=3,
)
def download_lineage(ctx: JobContext) -> dict:
    """Fetch one lineage's dataset into the shared library.

    Idempotent by construction: compleasm's own download_lineage checks for
    the `.done` marker before re-fetching, so re-running this job against an
    already-present lineage is a fast no-op rather than a duplicate
    download.

    Also fetches ~100MB of placement files on first use, unconditionally --
    verified against a real download on 2026-08-02, this is compleasm's own
    `Downloader.__init__` behaviour and not something this handler's argv can
    suppress. Paid once per `library_path`, not once per lineage.
    """
    tool = tools.require(tools.compleasm())

    lineage = (ctx.payload.get("lineage") or "").strip()
    if not lineage:
        raise PermanentError("download_lineage requires a 'lineage'")
    odb = (ctx.payload.get("odb") or "").strip()
    if not odb:
        raise PermanentError("download_lineage requires an 'odb'")

    library_path = settings.lineages_dir
    library_path.mkdir(parents=True, exist_ok=True)

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ctx.progress(phase="downloading", pct=None, message=f"downloading {lineage}_{odb}")
    # Lineage downloads for a large dataset can run minutes; the placement
    # files add more on a cold library_path. Generous rather than measured,
    # the same posture download_assembly's own extend_lease takes.
    ctx.extend_lease(1800)

    cmd = completeness_runner.build_download_command(
        compleasm_path=tool.path,
        lineage=lineage,
        odb=odb,
        library_path=library_path,
    )

    log.info("lineage_download_started", job_id=ctx.job_id, lineage=lineage, odb=odb)

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise download_failures.classify_failure(
            code, log_path, f"{lineage}_{odb}", tool="compleasm"
        )

    if not lineage_present(library_path, lineage, odb):
        raise PermanentError(
            f"compleasm exited 0 but {lineage}_{odb} is not marked done -- "
            f"check {lineage!r} is a real lineage name "
            f"(`compleasm list --remote --odb {odb}`)."
        )

    ctx.progress(phase="done", pct=1.0, message=f"{lineage}_{odb} ready")
    log.info("lineage_download_finished", job_id=ctx.job_id, lineage=lineage, odb=odb)

    return {"lineage": lineage, "odb": odb, "library_path": str(library_path)}
