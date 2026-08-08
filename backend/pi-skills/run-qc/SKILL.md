---
name: run-qc
---

# Run QC on Raw Reads

## When to Use

The user has raw sequencing reads (FASTQ objects) in a project and wants to
assess quality before running further analysis. QC always comes first — it
is cheap and it decides whether trimming is warranted.

## Procedure

1. Find the reads: `bioflow_list_objects(project_id)` lists everything in
   the project, or `bioflow_search_objects(query)` searches across the
   library for e.g. "fastq".
2. Confirm what can run: `bioflow_suggest_next(object_id)` on the reads
   object (the R1 of a paired set if there is one). It returns `available`
   candidates with a ready-made launch payload for `bioflow_run_pipeline` —
   take the payload from there rather than constructing it by hand.
3. Launch QC: `bioflow_run_pipeline(kind="run_qc", params=<payload>)`. The
   tool picks the right tool by platform automatically — fastp + fastqc for
   short reads, NanoPlot for Nanopore/PacBio long reads.
4. Poll `bioflow_get_job(job_id)` until it finishes. QC produces a report
   and facts on the object; it does not create a new file.
5. Offer to interpret the report (see the `interpret-multiqc` skill) and
   note that trimming (`trim_reads`) is the next step if the numbers warrant
   it — `bioflow_suggest_next` on the reads object will confirm.

## Pitfalls

- `run_qc` is the job kind. Do not invent variants like `qc` or
  `read-qc-and-trimming` (the latter is a guide topic, not a job kind).
- Reads must be ingested before they can be QC'd — if the object is not
  there yet, tell the user to upload through the BioFlow UI; there is no
  MCP upload tool.
- QC measures; it never modifies the input.
