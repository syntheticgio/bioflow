"""What the project panel needs to say about a project's MultiQC report.

One read, one shape, seven states. The frontend renders a state; it does not
assemble one from four separate lookups, because the interesting cases here
are *combinations* -- a failed run that still has an older report to offer
(`failed` + `report`) reads differently from a failed run with nothing
(`failed` alone), and deciding that in the component would put the
distinction somewhere no test covers.

Deliberately not a field on `ProjectDetail`. The report is a filesystem
artifact whose state changes without the project document being touched, so
folding it into the project read would either serve stale values or make
every project fetch stat the filesystem.
"""

from dataclasses import dataclass

from beanie import PydanticObjectId

from app.models.job import Job, JobState
from app.models.object import DataObject
from app.queue import multiqc_handlers


@dataclass
class MultiqcStatus:
    """Everything the Project QC panel renders, in one read.

    The flags are deliberately independent rather than one enum: a run can
    fail while an older report is still on disk, and that combination is a
    distinct thing to say (`s7` in the design) rather than a fallback to
    either single state.
    """

    # How many of the project's files carry QC output MultiQC could parse.
    # Drives both "no report yet" (with a count to explain the offer) and
    # "not available" (fewer than two).
    summarizable: int = 0
    # Unix timestamp of the report on disk, or None when there is none.
    generated_at: float | None = None
    # How many files the *current* report covers. None when there is no
    # report. Taken from the last succeeded job rather than recomputed, so
    # it describes what was summarized rather than what would be now.
    covered: int | None = None
    # True when QC output is newer than the report -- see
    # `multiqc_handlers.newest_qc_output_at` for what staleness means here.
    stale: bool = False
    # Set while a run is queued or running, so the panel can show progress
    # rather than a stale "no report".
    running: bool = False
    running_since: float | None = None
    # Set when the most recent run failed. Independent of `generated_at`:
    # both together mean a regeneration failed over a report that survives.
    failed: bool = False
    failed_at: float | None = None

    def as_dict(self) -> dict:
        return {
            "summarizable": self.summarizable,
            "generated_at": self.generated_at,
            "covered": self.covered,
            "stale": self.stale,
            "running": self.running,
            "running_since": self.running_since,
            "failed": self.failed,
            "failed_at": self.failed_at,
        }


_ACTIVE_STATES = (JobState.PENDING, JobState.QUEUED, JobState.RUNNING)


async def status_for(project_id: PydanticObjectId, *, owner: str) -> MultiqcStatus:
    """Assemble the project's MultiQC state.

    Callers must have already authorized `project_id` against `owner`; the
    `owner` here scopes the queries rather than granting access.
    """
    objects = await DataObject.find(
        {"project_id": project_id, "owner": owner}
    ).to_list()

    generated_at = multiqc_handlers.report_generated_at(project_id)

    status = MultiqcStatus(
        summarizable=multiqc_handlers.count_summarizable(objects),
        generated_at=generated_at,
    )

    if generated_at is not None:
        newest = multiqc_handlers.newest_qc_output_at(objects)
        status.stale = newest is not None and newest > generated_at

    active = await Job.find(
        {
            "project_id": project_id,
            "type": "multiqc_report",
            "state": {"$in": [s.value for s in _ACTIVE_STATES]},
        }
    ).to_list()
    if active:
        status.running = True
        started = [j.timing.started_at or j.created_at for j in active]
        chosen = min(started)
        status.running_since = chosen.timestamp() if chosen else None

    # The last finished run, whichever way it went. A failure is only worth
    # reporting when it is the most recent outcome -- a failure followed by
    # a success is history, not state.
    last = (
        await Job.find({"project_id": project_id, "type": "multiqc_report"})
        .sort("-created_at")
        .limit(5)
        .to_list()
    )
    for job in last:
        if job.state is JobState.FAILED:
            status.failed = True
            finished = job.timing.finished_at
            status.failed_at = finished.timestamp() if finished else None
            break
        if job.state is JobState.SUCCEEDED:
            status.covered = (job.result or {}).get("objects_summarized")
            break

    return status
