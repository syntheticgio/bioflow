---
name: drive-pipelines
---

# Drive a Pipeline in BioFlow

## When to Use

You need to run a pipeline step in BioFlow — QC, trimming, alignment, variant
calling, assembly, or any stage — and you want the general pattern that works
for all of them.

## Procedure

1. Check what can run next: call `bioflow_suggest_next(object_id)` on the
   relevant object (reads, a genome, an alignment). It returns a list of
   launchable jobs with their kinds and pre-built payloads — prefer these
   payloads over constructing one from scratch.
2. Read the guide for the stage if you need context:
   `bioflow_get_guide(topic)` where topic is one of `getting-started`,
   `acquiring-data`, `read-qc-and-trimming`, `alignment-and-variants`,
   `de-novo-assembly`, `rna-quantification`.
3. Run the pipeline: `bioflow_run_pipeline(kind, object_id, params)` using
   the kind and params from `bioflow_suggest_next` or from the guide.
4. Poll the job: `bioflow_get_job(job_id)` until the status is `completed`
   or `failed`. Jobs are asynchronous — never report success without
   checking.
5. Verify the output: check that the expected object was created (look for it
   with `bioflow_get_object(object_id)` on the output reference from the job
   record, or `bioflow_list_objects` on the project).
6. If the job failed, see the `debug-failed-job` skill.

## Pitfalls

- Job kinds are ground truth from `bioflow://jobs/types`; `run_qc`,
  `trim_reads`, `build_index`, `align_reads`, `index_bam`, `call_variants`,
  `run_vcf_stats` are examples, not an exhaustive list.
- `bioflow_run_pipeline` returns immediately; the job runs in the
  background. Never report success without polling the job.
- QC (`run_qc`) does not produce a new object — "no output file" is
  expected there; check the job outcome instead.
