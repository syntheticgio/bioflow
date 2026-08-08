---
name: debug-failed-job
---

# Debug a Failed or Stuck Job

## When to Use

The user says a pipeline failed, a job is stuck, or asks why something did
not produce its expected output.

## Procedure

1. Find the job: `bioflow_get_job(job_id)` if the id is known, otherwise
   `bioflow_list_jobs(limit)` and look for the failed/failed-outcome entry.
   Jobs are asynchronous — a job may also still be running, so check its
   status before declaring failure.
2. Read the job record's error and captured output. Distinguish:
   - **Input problems** — missing or corrupt objects, wrong payload. The
     fix is on the data side (re-upload, re-download, correct params).
   - **Tool failures** — tool not installed (`needs_install`), resource
     limits (memory/OOM, disk), or a tool version mismatch. The fix is
     install/pin the tool, retry with fewer threads, or free disk.
3. Suggest the concrete fix and offer to retry. If the job is stuck rather
   than failed, `bioflow_cancel_job(job_id)` may be the right move before
   relaunching.
4. For a freshly failed run, check `bioflow_suggest_next` on the input
   object — it will say whether the input is actually runnable, which
   distinguishes "the tool is broken" from "this object was never runnable".

## Pitfalls

- A `run_qc` job does not produce a new object — "failed to produce output"
  is expected for QC; check the job outcome, not a missing file.
- Do not guess tool names or job kinds from memory: `bioflow_list_tools()`
  and the `bioflow://jobs/types` resource are the ground truth.
- Timeouts on long pipelines are normal; a job that has run for hours is
  not necessarily stuck — check its progress in `bioflow_get_job` before
  recommending a cancel.
