# Acquiring data

Data reaches a BioFlow project one of three ways. All three end the same
way: an object in the project, with its format and role detected.

## Uploading a local file

If the human already has the file on disk, they upload it through the
BioFlow UI (`POST /api/v1/uploads` and its chunk endpoints) -- there is no
MCP tool for this, since the bytes have to move from the human's machine,
not from here. This is still a job, like everything else: completing the
upload returns a job id for `assemble_upload`, and once that succeeds it
enqueues a second job, `ingest_headers`, to detect the format and pull out
header facts. Poll `bioflow_get_job` for both before treating the object as
ready -- there is no fire-and-forget path here.

## Downloading an NCBI assembly

`bioflow_search_ncbi(term)` resolves an organism name to its NCBI taxon and
lists the assemblies on file for it. Pick an `accession` from the results
and hand it to `bioflow_download_reference(accession, project_id)`.

Like every pipeline, the download is a job (`download_assembly` under the
hood) -- the tool call returns a job id immediately, and the file is not in
the project until you poll `bioflow_get_job` and see it finish.

## Downloading an SRA run

An SRA accession (an `SRR...` run) is fetched with `bioflow_run_pipeline`
using the `download_sra_run` job type directly -- there is no dedicated
convenience tool for it the way there is for assemblies. Same rule applies:
it is a job, poll `bioflow_get_job` for it to land.

An SRA run always yields FASTQ (a single file, or an R1/R2 pair), so once it
lands the natural next step is `read-qc-and-trimming`.

## After acquisition

Whatever the source, once an object exists, `bioflow_suggest_next(object_id)`
is the right way to find out what can be run against it -- rather than
guessing from the format.
