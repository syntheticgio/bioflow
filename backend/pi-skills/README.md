# pi-skills

Skills that teach the BioFlow agent how to do bioinformatics workflows in
BioFlow.

## How to read these

- All tools are reached through the **mcp proxy tool** (pi-mcp-adapter):
  `mcp({ tool: "bioflow_...", args: { ... } })`. The exact proxy syntax is
  covered by the agent's system prompt; these skills name the real tool and
  its arguments.
- Tool names and arguments here are the real MCP server tools
  (`backend/app/mcp/server.py`), not invented ones. If a name looks
  surprising, check the server before "fixing" it.
- `bioflow_suggest_next(object_id)` is the most useful call in the whole
  server: it asks BioFlow itself what can run against an object right now,
  accounting for installed tools, existing indexes, and what has already
  run. Prefer it over reasoning from format names.
- Job kinds (`run_qc`, `trim_reads`, `align_reads`, `build_index`,
  `index_bam`, `call_variants`, `download_sra_run`, ...) are read from the
  `bioflow://jobs/types` resource or taken straight from a
  `bioflow_suggest_next` launch payload — never invented.

## Skills

- `run-qc` — assess raw read quality before anything else
- `interpret-multiqc` — explain a QC report in plain terms
- `suggest-next-steps` — what should the user run next
- `debug-failed-job` — diagnose a failed or stuck job
