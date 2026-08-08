import { useState } from "react";
import type { RunMemberJob } from "../../api/types";
import { ROLE_LABELS } from "../../lib/runFormat";
import { JobLogView } from "../JobLogView";
import { FailureExplanationExpander } from "./FailureExplanationExpander";

/**
 * Why a run failed, inside the ledger row's existing expansion.
 *
 * The data is already on the page -- `run_service.status_for` returns each
 * member's error, and ActivityView fetches every run's detail for its job
 * counts. Until this existed, the ledger threw it away, and a job that
 * belonged to a run was the one kind of job whose error and log were
 * unreachable: the loose-job list below the columns renders both, but
 * deliberately excludes anything grouped into a run.
 */
export function RunFailureBlock({ jobs }: { jobs: RunMemberJob[] }) {
  // One log open at a time, matching the ledger's own single-open-row rule.
  const [openLog, setOpenLog] = useState<string | null>(null);

  const failed = jobs.filter((j) => j.error != null);
  const succeeded = jobs.filter((j) => j.state === "succeeded").length;

  return (
    <div className="ledger-failure">
      {/* Where in the run it died. Failing at the last step after doing all
          the work and failing at the first are different diagnoses, and this
          is one line rather than a list of rows nobody needs. */}
      {jobs.length > 0 && (
        <div className="ledger-failure-shape">
          {succeeded} of {jobs.length} {jobs.length === 1 ? "job" : "jobs"} succeeded
        </div>
      )}

      {failed.length === 0 ? (
        // A cancelled sibling leaves no error, and a pruned job comes back
        // with null state. Without this line the block would render empty,
        // which reads as broken rather than as merely unhelpful.
        <div className="ledger-failure-none">
          No job reported an error; the run may have been cancelled or a job may
          have expired.
        </div>
      ) : (
        failed.map((job) => (
          <div key={job.job_id} className="ledger-failure-job">
            <div className="ledger-failure-head">
              <span className="ledger-failure-role">
                {ROLE_LABELS[job.role] ?? job.role}
              </span>
              <button
                type="button"
                className="btn-text"
                onClick={() =>
                  setOpenLog((o) => (o === job.job_id ? null : job.job_id))
                }
              >
                {openLog === job.job_id ? "Hide log" : "Show log"}
              </button>
            </div>

            <div className="ledger-failure-error">
              {job.error!.code}: {job.error!.message}
              <FailureExplanationExpander
                code={job.error!.code}
                message={job.error!.message}
              />
            </div>

            {/* Never live: a run in this ledger has finished by definition. */}
            {openLog === job.job_id && (
              <JobLogView jobId={job.job_id} live={false} />
            )}
          </div>
        ))
      )}
    </div>
  );
}
