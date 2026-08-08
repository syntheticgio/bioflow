---
name: interpret-multiqc
---

# Interpret a QC Report

## When to Use

The user has a completed QC job (or a QC report object) and wants to know
whether the data is good enough to proceed — usually before deciding whether
to trim.

## Procedure

1. Get the object: `bioflow_get_object(object_id)` returns the QC report
   and the facts recorded on the reads object (look for the facts payload
   from the `run_qc` job).
2. If you need the raw job record: `bioflow_get_job(job_id)` shows the
   captured output of the QC run.
3. Explain in plain terms:
   - **Per-base quality** — is the quality high and stable across the read,
     or does it drop off toward the 3' end (that is what trimming fixes)?
   - **Adapter content** — high adapter contamination means the reads need
     `trim_reads` before alignment.
   - **GC content and duplication** — a weird GC distribution or very high
     duplication can indicate contamination or an amplification issue.
   - **Platform context** — short reads use fastp/fastqc; long reads use
     NanoPlot, so do not apply short-read per-base expectations to a
     NanoPlot report.
4. Give a concrete recommendation: proceed as-is, trim first, or flag a
   sample as unusable. Point at `trim_reads` if trimming is warranted, and
   mention that `bioflow_suggest_next` on the reads object will confirm what
   can run next.

## Pitfalls

- Do not fabricate numbers. If the report or facts are not available, say
  so and offer to run `run_qc` first.
- A low-quality result is not a bug — the recommendation is trimming or
  resequencing, not re-running QC.
