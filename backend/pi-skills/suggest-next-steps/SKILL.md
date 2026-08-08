---
name: suggest-next-steps
---

# Suggest Next Steps

## When to Use

The user asks "what should I do next?", is deciding what to run, or has just
added a new object to a project and wants to know what it enables.

## Procedure

1. Find the object in question: `bioflow_get_object(object_id)`, or
   `bioflow_list_objects(project_id)` to see everything in the project.
2. Ask BioFlow itself: `bioflow_suggest_next(object_id)` returns every
   candidate pipeline with a status — `available`, `unavailable`, or
   `needs_install` — a ready-made launch payload, and the honest reason
   anything cannot run. This is the authoritative answer; do not reason
   from file format alone.
3. For each candidate, explain in plain terms: what it runs, what input it
   needs, and what the output will be.
4. If the user wants to launch one, use the payload from `suggest_next`
   with `bioflow_run_pipeline(kind, params)` and poll `bioflow_get_job`.
5. If a candidate is `needs_install`, say which tool is missing rather than
   suggesting the run anyway.

## Pitfalls

- `bioflow_suggest_next` takes an **object id**, not a project id.
- "Next" is relative to the object — a reference needs `build_index` before
  `align_reads`; reads need `run_qc`/`trim_reads` first. The tool accounts
  for this; your explanation should reflect it.
- If the project has no objects yet, the honest answer is to add data first
  (upload via the UI, `bioflow_download_reference`, or `download_sra_run`).
