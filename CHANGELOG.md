# Changelog

All notable changes to BioFlow, generated from Conventional Commits by
[git-cliff](https://git-cliff.org). One entry per commit: the subject plus
the first paragraph of the body where one exists; only `feat` and `fix`
reach the notes. See AGENTS.md "Release notes" for the contract.

## [0.2.6] - 2026-08-07




### 🚀 Features

- *(frontend)* resource limits settings page (#22)
Says the limit governs what BioFlow plans to start, not what it may use --
per the spec, a mispredicted job will go over, and promising otherwise would
make the first overrun read as a bug.

- *(queue)* admission respects the stored resource limit (#22)
_free_resources resolves the stored budget against the machine's own and
computes headroom from the lower of the two. No new enforcement: claim.lua
already refuses any candidate whose declared mem_mb …

- *(api)* GET/PUT settings/resources for the admission budget (#22)
Reports the machine's own budget alongside the stored one so the UI can render
a range and say what 'no limit' resolves to. A zero limit is refused at the
edge rather than silently reinterpreted -- it…

- *(services)* resolve a stored memory limit against the host budget (#22)
A stored limit only ever lowers the budget -- typing 64 GB on a 16 GB machine
cannot conjure headroom. Zero and negatives are 'no opinion' rather than a
literal ceiling of nothing, which would admit n…

- *(models)* ResourceLimits singleton for the admission budget (#22)
Upsert-on-read singleton following the AiRouting precedent. None on any field
means 'use the machine's own budget' -- a real state rather than a null, so
the UI's 'No limit' writes None instead of a s…

- *(version)* make release targets (#53)

- *(version)* show the running version on the About page (#53)

- *(version)* GET /api/v1/version (#53)

- *(version)* release script with preflight refusals (#53)
Wraps ops/lib/bump_version.py in the release ceremony: preflight checks
(semver, clean tree, on main, tag not already taken, version actually
increases), then bump + commit + tag + push as one atomic …

- *(version)* bump library for both release lines (#53)

- *(version)* VERSION file and generated version module (#53)
Also mounts VERSION into run-worktree-tests.sh's throwaway container --
it previously mounted only backend/app and backend/tests, so the new
consistency test's repo-root read resolved against an empty…

- render variant summary above the variant results charts

- render DE summary above the DE results plots

- add frontend types and API client functions for DE/variant summaries

- add DE and variant summary API endpoints

- chain DE and variant summary launches into their producing jobs

- collect severe variants during vcf_stats for the summary prompt
run_vcf_stats now builds consequence_counts and severe_variants in the
same per-record pass that already feeds the density accumulator and
FILTER tally, via a new ConsequenceAccumulator in vcf_stats_r…

- add launch_de_summary and launch_variant_summary
Also classifies the two new handlers (summarize_de_results,
summarize_variant_results) in provenance_walker._NO_NARRATIVE_STEP -- they
write a field rather than producing an object, same shape as summ…

- add variant summary queue handler

- add DE summary queue handler

- add variant summary prompt builder

- add DE summary prompt builder

- add DE_SUMMARY and VARIANT_SUMMARY task slots

- render on-demand failure explanation in ActivityView

- add failureExplanation API client function

- add GET /pipelines/failure-explanation endpoint
Mirrors /pipelines/organism/{organism}: returns null rather than 404
when there is nothing to say (no provider configured, or a model that
produces nothing). Not owner-scoped -- the explanation depend…

- add failure explanation service
Read-through cache mirroring organism_service.py: resolves the
FAILURE_EXPLANATION task slot, calls the model synchronously (no queue),
and upserts into FailureExplanation keyed by normalize_failure(c…

- add failure explanation prompt

- add FailureExplanation model and normalize_failure
Mirrors OrganismBlurb/normalize_organism, but keys the cache on a hash
of (code, message) instead of a literal normalized string, since error
messages are unbounded in length and character content.

- add FAILURE_EXPLANATION task slot

- *(frontend)* assembly continuity facts block (#65)

- *(pipelines)* GCI launch path with chemistry routing, route, and card (#65)
launch_continuity_qc auto-pairs a HiFi and/or ONT BAM against a draft
assembly, splitting alignments_against's long_ bucket further by chemistry
via the new gci_slot_for_chemistry (and its shared _gci…

- *(queue)* assess_assembly_continuity handler and applier (#65)
Adds GCI's handler, modeled on assess_assembly_errors (CRAQ): links
the assembly and available hifi/nano BAMs under fixed names, requires
each BAM's .bai beside it via the existing _link_bam_index hel…

- *(pipelines)* GCI command builder and .gci parser (#65)

- *(pipelines)* install GCI and register the tool (#65)
Pure-Python assembly-continuity tool, pinned to commit
543cd4136187ff3ddd3ba4d1585626dbcdef6af6 (re-verified 2026-08-07: main
had not moved). No build step -- GCI never invokes an aligner, it
consumes…

- *(frontend)* QV facts block and spectra-cn plots (#64)
Renders Merqury k-mer QV facts (assembly_qv, error rate, completeness,
k, tool/meryl versions) on the assembly's Quality tab, following the
existing hasAssemblyErrors block pattern. The read set the Q…

- *(pipelines)* QV launch path, route, and Actions card (#64)
Task 5 of the Merqury k-mer QV plan. launch_qv_qc resolves the assembly
and read set (auto-picking only when exactly one candidate exists),
materializes a cached MERYL_DB sidecar group back into a dir…

- *(queue)* assess_assembly_qv handler and meryl-db caching applier (#64)
Task 4 of the Merqury k-mer QV plan. Adds the assess_assembly_qv SUBPROCESS
handler (meryl count + merqury.sh, fixed-name input links so an object's own
name never reaches the command line) and _apply…

- *(models)* add MERYL_DB sidecar role for cached k-mer databases (#64)
_SIDECAR_ROLES in queue/results.py is already the derived kind
({role.value: role for role in SidecarRole}), so adding the enum
member alone makes it recognized -- no registry entry needed, unlike
STA…

- *(pipelines)* merqury command builders and output parsers (#64)

- *(pipelines)* install meryl 1.4.2 and Merqury, reject Debian's meryl (#64)
meryl's arm64 release binary links against OpenSSL 1.1, which Debian
trixie no longer carries on either architecture. Vendor libssl.so.1.1/
libcrypto.so.1.1 from a debian:bullseye-slim build stage int…

- *(assembly)* pass the run's stage order to the progress parser (#55)

- *(assembly)* report phase_index/phase_total from the stage order (#55)
Indexes on the raw Flye stage name rather than the display label, which
is not injective. An undeclared stage reports a null index, so the UI
falls back to the phase name alone exactly as it does toda…

- *(assembly)* derive Flye's stage order from params (#55)
Flye builds its full job list at launch, so the stage sequence is known
before the run starts: seven stages, or six when --iterations 0 drops
polishing. Also drops the trestle label -- JobTrestle is c…

- *(frontend)* render open-vocabulary fields as a combo (#66)

- *(frontend)* type open_vocabulary on MetadataField (#66)

- *(metadata)* mark the eight externally-owned vocabularies open (#66)

- *(metadata)* add FieldDef.open_vocabulary flag (#66)

- *(mcp)* connection panel for the MCP settings tab
Adds SettingsMcp, a paste-ready mcpServers config block keyed to the
selected profile's id, plus the generalized 3-item SettingsNav and the
/settings/mcp route.

- *(mcp)* mount the MCP server at /api/v1/mcp (#31)
Uses mcp.server.mcpserver.MCPServer and Context-parameter injection --
the plan's original draft assumed mcp.server.fastmcp.FastMCP and
mcp.get_context(), neither of which exist in the installed mcp==…

- *(mcp)* search, acquisition and reference tools (#31)

- *(mcp)* run, poll and cancel pipeline jobs (#31)
cancel_job models the real POST /jobs/{id}/cancel route: it replicates
the route's inline ownership check (Job.owner == owner) before calling
queue.request_cancel, and returns request_cancel's actual …

- *(mcp)* suggest_next, so an agent can ask what to run (#31)

- *(mcp)* orientation and data tools (#31)

- *(mcp)* derived resources for tools, job types and sources (#31)

- *(mcp)* guide topics with exhaustiveness tests (#31)

- *(mcp)* resolve the acting profile from ?profile= (#31)

- *(frontend)* CRAQ assembly error facts block (#63)
Renders assembly_error_* facts (Task 5's craq_runner/assembly_qc_handlers
output) in AssemblyFacts.tsx, mirroring the existing QUAST misassembly
block's markup and inline styles. AQI/S-AQI/CSE are onl…

- *(queue)* ingest CRAQ's corrected FASTA as a new object (#63)
Handler's return dict now carries ngs_bam_object_id/sms_bam_object_id
(read from ctx.payload, set there by launch_assembly_error_qc) so the
applier can attribute the corrected FASTA to the BAM(s) CRAQ…

- *(pipelines)* launch path, route and Actions card for CRAQ (#63)
Task 4 of the CRAQ assembly error detection plan: adds
pipeline_service.alignments_against (splits a project's BAMs aligned
to an assembly by read chemistry, refusing UNKNOWN rather than
defaulting), …

- *(queue)* assess_assembly_errors handler for CRAQ (#63)
Wires Task 2's craq_runner (command builder + report/bed parsers) into a
SUBPROCESS handler, following assess_misassemblies's shape: fixed-name
links for every input (CRAQ shells out via system(), so …

- *(pipelines)* CRAQ command builder and output parsers (#63)

- *(pipelines)* install CRAQ and register the tool (#63)
Adds backend/scripts/install-craq.sh (git-clone install, pinned via
CRAQ_COMMIT, whole tree kept since bin/craq resolves src/ siblings
relative to its own path), wires it into the Dockerfile after
ins…

- *(align)* add SamPlatform enum from the SAM specification (#61)

- *(launcher)* sign macOS bundles with Developer ID (#39)
Adds the macOS signing path: a tauri.macos.conf.json overlay (auto-merged
the same way tauri.linux.conf.json already is), a hardened-runtime
entitlements file, and build-macos.sh wrapping the build wi…





### 🐛 Bug Fixes

- *(docker)* provide flye-samtools shim for Flye consensus stage (#67)
Debian's flye 2.9.5+dfsg-1 unbundles upstream's vendored samtools but
patched only polishing/alignment.py, leaving utils/sam_parser.py calling
the absent `flye-samtools`. Every assembly died at the co…

- *(frontend)* reject non-numeric memory limit input (#22)
parseFloat("abc") is NaN, and NaN <= 0 is false in JavaScript, so the
existing validation let non-numeric input through. On save, Math.round(NaN)
produced NaN, which JSON.stringify silently serializes…

- *(queue)* subtract the memory reservation from admission headroom (#68)
claim.lua INCRBYs bp:conc:mem_mb and release.lua DECRBYs it, but
_read_reservations never read the counter and compute_free_resources had no
parameter for it. The ledger was maintained correctly by bo…

- *(version)* mount VERSION into the api container's dev override (#53)
Task 1 mounted VERSION into the throwaway worktree test container but
not the standing dev stack's api service, so
tests/test_version_consistency.py's two repo-root-reading tests passed
in every isola…

- *(version)* release job's notes heredoc never terminated inside the YAML block (#53)
The `cat > "$notes_file" <<EOF` heredoc lived inside an indented `run: |`
block scalar, which indents every line including the closing `EOF`. Bash
only recognizes an unindented terminator at column 0,…

- *(version)* make the partial-bump regression test actually discriminate the bug (#53)
The fake bump_version.py used by TestBumpFailurePartway printed a
tracked filename but never changed its content, so `git commit`
failed with "nothing to commit" regardless of whether release.sh's
`se…

- *(version)* release.sh must not silently ignore a failed bump (#53)
`while read; done < <(bump_version.py ...)` hid a nonzero exit from
set -e, because process substitution only reports the loop's own exit
status. bump_version.py is all-or-nothing today so this never …

- exclude DE/variant summary fact keys from summary_fingerprint
summary_fingerprint() only excluded ai_summary_* keys, but
launch_de_summary and launch_variant_summary reuse it for their own
dedup checks against ai_de_summary_fingerprint and
ai_variant_summary_fin…

- *(pipelines)* correct GCI parser against real Zenodo test data (#65)
Verified parse_gci against a real GCI v1.0 run on upstream's own Zenodo
example dataset (MH63, record 12748594). The real .gci file has a leading
chemistry label line ("HiFi:") and a trailing dash sep…

- *(queue)* GCI handler consistency and test coverage (#65)
Corrects three review findings on assess_assembly_continuity: the
GCI_PLOT_MAX_CONTIGS comment claimed a gate the handler never enforced
(now documents that the launch path is responsible), bracket ac…

- *(pipelines)* GCI parser selects Genome row and guards invariants (#65)

- *(pipelines)* add ARG GCI_COMMIT to Dockerfile for build-time override (#65)

- *(frontend)* correct guessed spectra-cn plot filenames (#64)
Task 6's plot filenames (qv.spectra-cn.{fl,ln,st}.png, qv.qv.png) were a
documentation-based guess, flagged at the time as needing verification
against a real run. Verified 2026-08-07 against a real S…

- *(pipelines)* patch Merqury's k-detection for meryl 1.4.2's verbose print (#64)
Real run discovery: eval/spectra-cn.sh detects k via
`meryl print $read | head -n 2 | tail -n 1 | awk '{print length($1)}'`,
which assumes line 2 of `meryl print`'s output is the first k-mer row.
True…

- *(pipelines)* correct QV resource figure, tool version, and read extension (#64)
Task 7 real-data verification (S. cerevisiae R64 + DRR1066343, 2026-08-07)
found three issues in the first real run:

- *(pipelines)* reject a read set from another project on explicit id (#64)
Code review on Task 5 found launch_qv_qc's explicit-read_object_id branch
never checked the read set belongs to the same project as the assembly --
object_service.get_object only scopes by owner. A ca…

- *(pipelines)* reject a partially-cached meryl database on read (#64)
Spec review on Task 5 traced a real gap spanning Task 4 and 5: when the
applier only partially ingests a meryl database (meryl_db_partially_applied),
it never recorded how many files the database was …

- *(queue)* loudly log partial meryl-db sidecar ingest (#64)
Code review on Task 4 caught that _apply_assess_assembly_qv had no signal
for a partial or total cache-ingest failure -- it logged the count that
succeeded but never compared it against how many files…

- *(pipelines)* meryl probe now matches Celera's real --version output (#64)
Code review on Task 1 caught that the Celera-rejection regex matched the
dpkg *package* version string (0~20150903+r2013-9+b1), which the meryl
*binary* never actually prints. Verified against the rea…

- *(a11y)* ARIA combobox semantics for NCBI organism search (#34)
The organism-search suggestion dropdown in NcbiDownloadDialog rendered
plain input + ul/button markup with no ARIA combobox semantics, so a
screen reader had no way to know a suggestion list was open,…

- *(metadata)* stop warning on values from open vocabularies (#66)

- *(mcp)* acquiring-data guide understated upload as fire-and-forget (#31)
From code review of Task 14: the guide said uploads have "nothing further
to launch or poll" once complete, but POST /uploads/{id}/complete returns
202 with a job id for assemble_upload, which itself …

- *(mcp)* match the settings panel to existing page conventions (#31)
From code review of Task 13: the panel used classNames that don't exist
anywhere in styles.css (settings-view, settings-section, hint), so it
rendered functionally correct but visually unstyled -- no …

- *(mcp)* dedup response shape, shared job lookup, owner-scoping tests (#31)
From code review of Task 9:
- search_ncbi crashed on any organism with real assemblies on file --
  AssemblyMetadata has no as_dict() (unlike TaxonSuggestion, which does),
  so the naive [a.as_dict() …

- *(mcp)* dedup response shape, shared job lookup, owner-scoping tests (#31)
From code review of Task 8:
- run_pipeline's dedup response no longer sets job_id: None -- an agent
  that reads result["job_id"] without checking `deduplicated` now gets
  an immediate KeyError inste…

- *(mcp)* catch malformed owner in whoami, drop dead null guard (#31)
From code review of Task 6:
- whoami is the one tool that re-derives an owner from the string rather
  than forwarding it, so it's the one place a malformed value could reach
  a raw bson.errors.Inval…

- *(mcp)* assert derived-resource values, not just their keys (#31)
From code review of Task 5: the original tests only checked that
installed_tools()/job_types() returned the right names, which would pass
even if the conversion silently dropped or scrambled values fo…

- *(mcp)* source test_endpoint_paths_are_real from openapi, not app.routes (#31)
Code review of Task 4 found the test was vacuously passing: app.routes only
yields framework paths (/docs, /openapi.json, ...) because
app.include_router composes sub-routers as _IncludedRouter object…

- *(mcp)* clarify no-profile message, pin empty-string fallback (#31)
From code review of Task 2: the "no profiles exist" error now says the
query parameter becomes unnecessary once a profile is created, and a new
test locks in that owner_for("") falls back like an abse…

- *(services)* register assess_assembly_errors in the provenance verb table
test_every_registered_handler_is_classified failed after merging #63:
provenance_walker.py's _STEP_VERBS dict landed on main (fact-grounded
pipeline provenance narratives, #16) after the CRAQ branch w…

- *(pipelines)* install bc, a real CRAQ runtime dependency (#63)
A real end-to-end run against yeast data (project 6a6a2fe0...) showed
runSR.sh calling bc 3 times and failing each call: "bc: command not
found" followed by "[: -eq: unary operator expected" -- the im…

- *(pipelines)* correct CRAQ report filename and row key against a real run (#63)
Every prior task passed its own unit tests and two independent code
reviews, but the design doc's source read of CRAQ's report format was
wrong in two ways that only surfaced by actually running CRAQ …

- *(queue)* link CRAQ's corrected FASTA to its run, matching sibling appliers (#63)
The chimera-break branch of _apply_assess_assembly_errors ingested the
corrected FASTA but never called run_service.record_outputs, unlike
every other applier that ingests a new object from a job's ou…

- *(pipelines)* refuse ambiguous BAM sets in the CRAQ Actions card, matching the launch path (#63)

- *(pipelines)* match CRAQ launch path's BAI payload keys to what the handler reads (#63)
launch_assembly_error_qc's BAM loop wrote ngs_bam_bai_sha256/sms_bam_bai_sha256,
but assess_assembly_errors' _link_bam_index reads ngs_bai_sha256/sms_bai_sha256.
The keys never matched, so every BioFl…

- *(queue)* resolve CRAQ BAM indexes as sidecar objects, fail loudly when absent (#63)
Code review on Task 3 found _link_bam_index's path-guessing (.bai beside
the raw BAM's own path) can't succeed for a BioFlow-produced BAM: storage
is content-addressed, so a BAM and its .bai are separ…

- *(ui)* do not require a platform to launch an alignment (#61)
PL is optional now that unknown platforms are omitted per the SAM spec;
gating submit on it left the Align button dead with no explanation.

- *(align)* omit @RG PL when the platform is unknown (#61)
All three emission shapes (as_sam_header, as_rg_args, as_star_rg_fields)
drop the field rather than rendering PL:None, and from_dict no longer
requires a platform -- without that, an unrecognized inst…

- *(align)* return None for platforms outside the SAM vocabulary (#61)
OTHER is not a SAM PL value, though sam_platform's docstring claimed it
was. The spec's remedy for an unrecognized technology is to omit the
field, so None now means omit. The ILLUMINA-when-empty defa…

- *(align)* emit DNBSEQ, not the invalid BGI, in @RG PL (#61)
The SAM spec's name for this platform has been DNBSEQ since April 2020;
BGI was never a valid PL value. Every BGI/MGI file aligned through this
codebase carried an @RG PL that downstream tools do not …

- *(launcher)* notarize the .dmg itself, not just the .app (#39)
The first real end-to-end signed+notarized build exposed a gap: Tauri's
build-time notarization step notarizes and staples the .app, but the .dmg is
assembled afterward, wrapping the already-notarized…

















## [0.1.0] - 2026-08-06




### 🚀 Features

- *(frontend)* project Q&A chat drawer
A footer entry point ("💬 Ask", visible only when a project is open,
via useMatch("/p/:projectId") read at the Shell level and passed down
as a prop -- Footer sits outside any one <Route>) opening a
bo…

- *(frontend)* project Q&A API client and event wiring
getProjectConversation/askProjectQuestion/clearProjectConversation
client functions and their types, following the existing shares
functions' pattern exactly. qa.answered registered in useEvents.ts's
…

- *(api)* project Q&A conversation and ask routes
GET/DELETE .../qa/conversation (creates an empty one on first access
rather than 404ing) and POST .../qa/ask, which enqueues
answer_project_question and returns the job id -- an answer must
survive th…

- *(queue)* result applier and qa.answered event for project Q&A
A structural no-op on the data model -- the handler already writes
the answer straight onto ProjectConversation, not a DataObject, so
there is nothing to merge into facts. Exists so the _APPLIERS
disp…

- *(queue)* answer_project_question job handler
THREAD mode like summarize_object, but unlike it this handler touches
Mongo directly -- the conversation document and each tool call the
loop makes -- since a chat answer needs live project data mid-l…

- *(ai)* capture context_length via list_models_with_context
A second method on both adapters, not a changed list_models() return
shape -- that function already has callers wanting only the id list.
AiProvider gains context_windows (model id -> length), populat…

- *(ai)* threshold-triggered conversation compaction
Folds turns before compacted_through into a summary once the live
tail estimate crosses qa_compaction_threshold (default 75%) of the
routed provider's context window, or the configured default when no…

- *(ai)* the tool-calling loop for project Q&A
Bounded at MAX_TOOL_CALLS=3, enforced in the loop since neither wire
format lets a caller cap the model's own choices. Exhausting the cap
without an answer forces one more call with tools withdrawn, s…

- *(ai)* search_objects and list_jobs tool execution, project/owner-scoped
Thin wrappers over search_service.search_objects and the same Job
filter shape GET /jobs uses. Scoping is structural: the JSON schemas
exposed to the model have no project_id/owner property, and
proje…

- *(models)* add ProjectConversation
One document per (owner, project_id), holding the outer user/assistant
turns of a project's Q&A chat history. The tool-calling loop's own
four-role scratch-state turns (adapters.ConversationTurn) are …

- *(ai)* add TaskSlot.PROJECT_QA
Settings page needs no change -- TaskRoutingPanel.tsx already maps
over the backend's slot catalog rather than a hardcoded list.

- *(ai)* thread tools/history through complete() and complete_sync()
Both widen to Completion | ToolCall | Failure. A ToolCall counts as a
successful round trip for provider-status recording purposes, even
though no final text came back yet. complete_sync() still recor…

- *(ai)* conversation replay via history param on both adapters
Adds ConversationTurn (four roles: user, assistant, tool_call,
tool_result) so a multi-turn tool-calling loop can feed a tool result
back to the model as a follow-up turn. Each adapter renders history…

- *(ai)* tool-calling in the Anthropic adapter
complete() gains an optional tools param, sent as input_schema per
Anthropic's own tool wire shape; a tool_use content block returns a
ToolCall. input is already a parsed dict on this wire format, unl…

- *(ai)* tool-calling in the OpenAI-compat adapter
complete() gains an optional tools param, sent as the OpenAI
function-calling wire shape; a tool_calls response returns a ToolCall
instead of Completion. Multiple tool_calls in one response take the
f…

- *(ai)* add ToolCall/ToolSpec types for tool-calling
Establishes the shared vocabulary both adapters will target in the
project Q&A tool-calling loop. No wire logic yet.

- *(launcher)* switch the icon to bioflow-mark-1024.png
Superseded 6de2c48 (which used assets/logo_colored.png, a DNA-strand mark)
per direction to use assets/bioflow-mark-1024.png instead -- confirmed
before regenerating that its four flat color bars (tea…

- *(launcher)* replace the stock Tauri icon with BioFlow's logo
launcher/src-tauri/icons/ still held Tauri's own scaffold icon (the cyan
and yellow interlocking-rings mark) from when the launcher crate was first
generated -- never replaced. Regenerated the full se…

- publish images to GHCR and reference them from compose
api, worker, and web move from build contexts to
ghcr.io/syntheticgio/bioflow-* image references, so a machine with no
source tree can start the stack -- the prerequisite the native launcher
(#28) nee…

- render prior runs on suggestion cards
Wires the backend's prior_runs field (Tasks 1-6) into the Actions tab:
each card now shows up to 3 prior runs with date, status, and links to
surviving outputs, so users can see whether a pipeline alr…

- attach prior runs to every pipeline suggestion card
Wires attach_prior_runs (Task 4) into suggestions_for so every card
carries a prior_runs list. Also updates the test suite's database
seams: stub_db now patches _runs_touching as a fourth seam, and th…

- gather prior runs for a card list in two queries

- shape a matched run into a card display row

- match a suggestion card to runs with the same parameters

- add Genome Analysis Review help page, update Workflow Diagrams
Adds "From Reads to Biology -- a field guide to genome assembly and
downstream analysis" under Help as Genome Analysis Review
(/help/genome-analysis-review), rendered the same way as Workflow
Diagrams…

- add an external-link icon to QC report links
FastQC/fastp/NanoPlot report names now show a small ↗ alongside the text,
both pointing at the same new-tab link -- makes it visually clear before the
click that these leave the app, without changing …

- add PNG/SVG download buttons to each workflow diagram
Small buttons in each diagram's lower-right corner, revealed on hover
(always visible on touch), linking to standalone PNG/SVG copies of
the same figure rather than the viewBox-stripped inline SVG use…

- add sticky Diagrams sidebar nav to Workflow Diagrams page
Restores the anchor-link table of contents from the source document
as a left sidebar, matching its look (uppercase label, numbered bold
titles, dimmed subtitles) instead of dropping it during the por…

- add Workflow Diagrams page under Help menu
Renders the five genome-assembly/downstream-analysis workflow SVGs
under BioFlow's own header at /help/workflow-diagrams, following the
same help-page pattern as BioFlow Calculations/Software/Data Sou…

- frontend for assembly completeness -- dialog, button, facts display
CompletenessDialog follows AssembleDialog's shape: the lineage is inferred
from organism metadata and shown as inferred/overridable, the same honesty
genome size already uses there. Adds a download st…

- Actions-tab card for assembly completeness
build_completeness_card, gated on FASTA format and excluding
pipeline_service.COMPLETENESS_EXCLUDED_ROLES (PROTEIN/TRANSCRIPT) --
not gated on provenance, matching launch_completeness's own rule that …

- launch path and API routes for assembly completeness
launch_completeness follows launch_vcf_stats's shape (read-only, no Run
record) rather than launch_assembly's: no derived object, just facts. Three
refusals kept deliberately distinct rather than coll…

- assess_completeness and download_lineage queue jobs
Part 3 of the post-assembly QC design. Two new handlers:

- install compleasm and wire its tool registry, runner, and lineage inference
Part 2 of the post-assembly QC design: builds miniprot and compleasm from
source (neither is packaged for trixie -- confirmed against the running
image -- and their obvious alternatives are both x86-o…

- add PipelineType.ASSEMBLY_QC ahead of compleasm
Part of the post-assembly QC design. compleasm judges an assembly rather
than producing one, and declaring it under the existing ASSEMBLE type would
put it in PipelineToolSelector's tool picker headed…

- compute assembly contiguity (N50/N90/L50/auN/gaps) in the FASTA parser
Part 1 of the post-assembly QC design
(docs/superpowers/specs/2026-08-02-post-assembly-qc-design.md): the
contiguity half needs no new tool, since N50 and friends are arithmetic
over lengths _parse_fa…

- STAR annotation-aware indexing (--sjdbGTFfile)
STAR ships indexing without an annotation and finds junctions de novo
(9,818 on real yeast data), but supplying a GTF improves sensitivity for
junctions with few supporting reads. Adds an optional --s…

- hash uploads client-side so dedup actually fires
upload_service.create_session already short-circuited on a matching
client_sha256, and the UI already knew how to render a dedup hit --
nothing produced the digest. crypto.subtle.digest isn't incremen…

- the assemble dialog
Reuses AlignerParamFields for everything the registry declares, since the
assembler schema serves the same ParamFieldMeta shape -- so mode, iterations
and threads are generated rather than hand-writte…

- launch de novo assembly, and stop the card claiming no assembler
Completes the backend path: launch service, endpoints, applier, and the
Actions card. Assembly is now runnable end to end from the Actions tab; the
dialog for changing parameters is separate and still…

- the assemble_reads queue handler
Runs Flye and harvests its outputs. Nothing launches this yet -- no service,
no endpoint, and the Actions card still says no assembler is installed.

- install Flye and declare the assembler registry
The tool layer for de novo assembly. No launch path yet -- nothing calls any
of this, and the Actions card still reads "No assembler is installed."

- add STAR as a fifth aligner, with a directory-shaped index layout
STAR's index is a directory of fixed filenames rather than files named by
appending to the reference path, which is the third layout shape
`IndexLayout` has documented since bowtie2 arrived without im…

- RNA-seq differential expression, from BAM to results table
Two pipelines, deliberately split at the point where the data model
changes shape. `quantify` is per-sample -- one BAM in, one counts file
out -- and fits the existing object-centric model exactly, so…

- scope the SSE stream to profiles, document schedules as global
Closes the last two unscoped routers.

- lay the activity page out as a three-column front page
/activity was the least broadsheet-styled surface left: broadsheet.css had no
.run-* rules at all, so it fell through to Classic's styles and rendered one
narrow column of five sections in a full-widt…

- tag files with what kind of sequence they hold
Adds a `sequence_type` metadata field -- Genomic, CDS, Protein or RNA --
shown beside size and format under the filename, and editable from the
metadata form on any file.

- give worktrees their own stack instead of hijacking the shared one
docker-compose.yml pins `name: biopipe` and the override's bind mounts are
relative, so `docker compose up` from a worktree does not create a second
stack -- it recreates *the* stack with its source p…

- run DeepVariant as a sidecar container
Removes both refusals -- the handler's _resolve_caller and the launch
path's validation in pipeline_service.py -- now that DeepVariant runs as a
sibling container rather than requiring a vendored bina…

- probe DeepVariant through Docker rather than PATH
DeepVariant runs as a sibling container rather than a binary, so
deepvariant() asks whether a docker client can reach a daemon and reports
the configured image tag as its version.

- give the worker a docker client and the host socket
For starting sibling containers: DeepVariant ships as an 8.83GB image,
mostly TensorFlow on a Python 3.10 this image does not otherwise carry,
so vendoring it would roughly double the image to gain on…

- build the DeepVariant docker run command

- map a read chemistry to a DeepVariant model

- translate container paths to host paths for sibling containers

- UniProt download dialog

- UniProt API client and types
Types verified against real API responses rather than read off the
Pydantic models: all three shapes -- the resolve envelope, the proteome,
and the protein -- match key for key.

- UniProt resolve and download endpoints

- ingest a UniProt download as a protein object

- UniProt download handler

- UniProt download launch service

- add UNIPROT_DOWNLOAD to RunKind
One member for both UniProt download shapes. A whole proteome and a
hand-picked set of proteins are the same request to the same endpoint --
only the query differs -- so splitting the enum would descr…

- resolve a UniProt input to a proteome card or a picker

- UniProt query construction, with the measured filters

- classify UniProt accession-box input

- report a deduplicated upload as a file that is ready
Blobs are stored once and shared across profiles, so a dedup hit can now
come from content another profile uploaded. An upload that finishes the
instant it starts reads as a bug, and "deduplicated, no…

- show the current profile in the header, with a way out
The label is the profile's emoji and username rather than a generic
"Profile" so the library you are working in is visible without opening
anything -- which is the point of putting it in the header at…

- pick a profile at startup, and gate the shell behind one
Adds the startup picker, the add-profile modal, and the gate in App.tsx that
chooses between them and the shell. Together these are what make the profiles
backend usable: until now the frontend sent n…

- send the profile on every request, including the two paths that bypass request()
Three paths talk to the API, and only one of them is obvious. request<T>
is the chokepoint every one of the ~60 api.* methods goes through, so the
header is injected there -- before init?.headers, so …

- hold the selected profile in a persisted store
Selecting a profile returns no token and sets no cookie, so the id held
here is the whole session -- which is why this store persists where
uiStore does not. Losing it on refresh would drop the user b…

- scope the pipelines and NCBI routes to the caller's profile
Closes the last cross-profile gap in the API. `pipelines.py` goes from 2
OwnerDep references to 21 across its 25 routes, and `ncbi.py`'s download
and resolve paths gain one each.

- scope search, facets and bulk edits to the caller's profile
search_service had no notion of an owner at all -- zero occurrences of the
string. Every public function now takes a required keyword-only `owner`, and
`build_filter` seeds its filter with the owner c…

- scope the jobs and uploads routes to the caller's profile
`GET /api/v1/jobs` answered 200 with every profile's jobs and no header at
all, and `list_uploads` did the same -- it read as empty only because no
upload happened to be in flight. Neither carried a T…

- scope every remaining route to the caller's profile
An unwired route is worse than an incomplete one. The service layer has
required `owner` parameters throughout, so a route that resolved nothing
still had to pass *something* -- and what it passed was…

- lay the Software page out two tools abreast
Halves the page height on a 15-tool index. Each section is its own
grid, so a new section always begins in the left column rather than
continuing beside the last tool of the one before it.

- open a produced file on its Results tab
A BAM or a called VCF/BCF is something the user asked the app to make,
and the first question about a produced file is what came out, not
whether the input passed QC. Objects without results still ope…

- warm the tool caches at startup, off the request path
Fires tool_cache.warm as a background task in lifespan so the ~15s cold
probe cost never delays startup, never gates /readyz, and any failure is
logged rather than surfacing as an unretrieved task exc…

- warm the probe caches, seeding from Redis

- persist tool probe results in Redis

- allow a probe result to be seeded from outside

- fingerprint a resolved binary for probe caching

- show the protein structure behind a variant
Adds a 3D button to residue-changing rows, opening iCn3D on the structure
of the protein the variant alters.

- serve a variant's protein structure
Wires the taxid walk to the UniProt resolver behind
GET /vcfstats/structure/{object_id}. Takes the object rather than a taxid
so the VCF -> reference -> organism walk stays on the server, where the
pr…

- resolve a gene to a protein structure via UniProt
A gene symbol is not an identifier. UniProt attaches SRP1 to two proteins
and ranks TIR1 (254aa) ahead of the intended importin alpha (542aa),
because TIR1 lists SRP1 as an alias -- so taking the top …

- resolve the organism taxid behind a VCF
The structure viewer's UniProt lookup has to be organism-scoped -- gene
symbols collide across organisms far more often than within one, so an
unscoped query can return an unrelated protein with a res…

- add the profiles API and prove the owner partition over HTTP
Tasks 1-9 made `owner` a real partition through the services, the queue and
the result appliers, but nothing created or selected a profile and no route
resolved one. This adds the HTTP surface: list, …

- give the download appliers the owner the job already carried
The two download appliers had no way to know whose data they were
writing. Every other applier resolves a parent object first and takes
that parent's owner, but a download produces the first object in…

- scope every job to an owner, dedup key included
`enqueue` was the last major writer that never set `owner`, so every Job in
the system inherited the "local" default -- and the dedup key it stores is
guarded by a unique partial index on `dedup_key` …

- adopt the pre-feature library instead of migrating it
Every document written before profiles existed carries the
TimestampedDocument default `owner: "local"`. Partitioning by owner
could have meant rewriting all of them across objects, projects, runs
and…

- make owner a real partition for pipeline runs
A run recorded what a user asked for but never recorded whose request it
was, so every PipelineRun carried TimestampedDocument's "local" default.
create_run now stamps the owner, which turns the filte…

- scope object_service by owner, closing the cascade leak
The reader half of this is routine: get_object, list_objects, list_sidecars,
set_pair, clear_pair, update_object, delete_object and object_with_blob now
take a keyword-only owner and filter on it, fol…

- scope project_service queries by owner
Every document already carried `owner` and two of the projects indexes
already led with it, but nothing ever read it as a filter. This makes the
field real for projects: every query that touches a par…

- add the X-BioFlow-Profile resolution dependency
Turns the client-supplied header into the `owner` string that service
functions will take as an explicit parameter. First use of FastAPI's
Depends()/Header() in this codebase; pyproject already ignore…

- add the Profile model
Profiles are the organizational boundary between people sharing one
library. The collection sits deliberately outside the owner partition it
defines: what matters is a profile's own id, stringified, w…

- filter the Software page by pipeline column
Clicking a column head narrows the page to that pipeline; clicking it again, or
the "Show all" link, restores everything.

- show gene, consequence and amino-acid change per variant

- add the annotate card, naming the missing input

- add the variant annotation run

- resolve the reference and annotation behind a VCF

- add a tool/pipeline matrix and favicon to the Software help page
The Software page listed every tool in six sections but could not answer
"what have I got for job X" without scrolling all six. The matrix at the
top puts tools against pipelines so that reads in one …

- index gene, consequence and amino-acid change per variant

- probe bcftools csq as a versioned capability

- build the bcftools csq command with -p a

- parse bcftools BCSQ consequence annotations

- route and link the Software and Data Sources pages

- add the Data Sources help page
Reuses the Software page's ruled treatment and classes: to a reader these
are two halves of one question -- what does this application depend on --
and different furniture would imply a distinction th…

- add the Software help page
A single scrolling index rather than a rail-and-detail browser.
PipelineToolSelector already covers choosing a tool mid-job, where showing
one description at a time is right. This page answers "what i…

- type the bibliographic tool fields and the source catalog
The Software and Sources help pages need these shapes before they can be
written, so they land first.

- serve the data source catalog at /system/sources
On system rather than pipelines: nothing here is a pipeline tool, and
the handler does no probing, so it cannot be slow or fail.

- add a data source catalog for the Sources help page
Static, with no version field: NCBI Datasets is whatever the API
returned today, and a fabricated version would read as provenance
without being it.

- add bibliographic fields to ToolMeta
Homepage, repository, citation, license, and a usage note, for the
Software help page. They reach the API through tool_with_meta's asdict
with no serializer change -- the fallback dict is the one plac…

- add genomic context button to variant rows

- let the sequence viewer open on a focused position

- add markerLabel helper for NCBI mk parameter

- add focusWindow helper for variant view ranges

- label chromosome bars with NCBI's real names
Fetches NCBI's sequence_reports at ingest so a bar reads IV or MT
rather than 1136 or 1224. Both accession namespaces map to one label,
so a GCA file is labelled as readily as its GCF twin.

- label bars with NCBI names, and point at re-ingest
Bar captions prefer bar.label when present, falling back to the
existing accessionTail digit-shortening. The tooltip and aria-label
now carry the full accession, length, and label together so nothing
…

- attach NCBI chromosome names to bars
facts.sequence_labels (accession -> NCBI's real name, e.g. "IV", "MT")
is now read alongside sequence_lengths and attached to each Bar as an
optional label. Purely cosmetic: classification (drawable b…

- store per-sequence chromosome names at ingest

- fetch per-sequence chromosome names from NCBI

- parse NCBI sequence reports into a label map

- show the Results tab for VCF and BCF files

- add the Variant Results tab component

- add the paginated, filtered variant table

- add the variant density and distribution charts

- add Variant Results types and client methods
Also restores frontend/src/vite-env.d.ts, which was missing from this
branch and made `tsc --noEmit` fail on every *.png import with no
relation to this change -- present on main, so untouched by the …

- add the Variant Results launch and pagination routes

- launch and apply the Variant Results computation

- add the run_vcf_stats job handler

- accumulate variant density and per-contig counts in one pass

- chromosome strip and NCBI Sequence Viewer on references
Draws a reference's chromosomes as proportional bars beside Base
Composition, from sequence_lengths the ingest already stores -- no NCBI
call to render. Clicking a bar opens NCBI's Sequence Viewer in …

- show the chromosome strip on the reference Quality tab

- add the NCBI Sequence Viewer modal

- add the chromosome strip component

- add a nucleotide accession link target

- detect NCBI-resolvable nucleotide accessions

- rank chromosome bars by length with overflow

- reject CDS and protein files as not chromosomal

- classify references with no lengths as needing QC

- add the indexed SQLite variant database

- derive the variant summary with a conditional PASS rate

- re-bin the QUAL and DP distributions into fixed histograms

- parse bcftools stats output for the Variant Results tab

- add the vcf_stats_dir setting

- remove the Classic theme toggle
The zustand store, its localStorage key, and the View menu all go. The
Broadsheet class now ships on <html> in index.html, so the scoped CSS layer
is unchanged and still lands before first paint.

- pin the Broadsheet class to <html>

- wire the Actions tab suggestions and local AI summaries
Actions tab: three sections (Computations, pipeline suggestion cards,
Manage this file) replacing the flat control list, with the headline's
button row folded into Computations and a Run QC prompt add…

- call trimming Preprocess in the dialog too
The operation filters by length and quality as well as trimming adapters,
which the narrower name undersells. UI strings only -- the route, job kind
and trim_* facts keep their names, since renaming t…

- split the Actions tab into Computations and Manage sections
Two presentational components for the reworked Actions tab, not yet
mounted -- DetailPanel wires them in the next change.

- render pipeline suggestions as a card grid
Each card is either runnable -- with the reason it is a sensible default --
or gated, with the honest reason it cannot run. The first available card
takes the primary button so the eye lands somewhere…

- add the pipeline suggestion type and client methods
The suggestions endpoint returns cards describing what a file can be run
through next. `launch.body` is typed as an opaque record on purpose: it is
the complete body for its own endpoint, and the thre…

- add the pipeline suggestions endpoint

- add the variants and assemble suggestion cards

- add the align suggestion card

- add reference resolution for the align card

- add the preprocess suggestion card
Promotes pipeline_service._read_chemistry to a public read_chemistry
rather than copying it: the qc_read_chemistry fact key is a contract
with what QC writes, and two readers of it would drift apart s…

- add the suggestion card model and genus classification

- complete the masthead status strip
The strip was showing only load state and stored bytes. Adds the file and
project counts on the left and the queue depth on the right, so it reads as
the design has it: what the library holds, then wh…

- rework the projects page against the Broadsheet design
Seven tweaks from the design review. Three are behavior and apply to both
themes; the rest are Broadsheet styling.

- add a switchable Broadsheet theme alongside the current UI
Implements the "BioFlow Broadsheet" mockup as a second selectable theme
rather than a replacement, so both can be lived with before one is chosen.
Pick it from View > Theme; the choice persists in loc…

- add a download action to the file row hover controls
A download arrow now sits left of the delete x on each file row, so
getting a file no longer means opening the detail panel first.

- readable timestamps, and group facts by their source
Timestamps in the facts table rendered as raw ISO strings
("2026-07-29T19:03:23.276489+00:00") because renderValue fell through
to String(value). They now go through formatDate, which also gains
secon…

- download an object's raw file from the Actions tab
Adds GET /objects/{id}/download, serving the stored bytes as an
attachment under the object's own name. One button in ActionsTab covers
reads, references, variants and alignments, since that tab is sh…

- update file type icons to flat redraw
Replaces the heavy-outline PNG icons with the updated flat design:
tinted paper, hairline outline, and one solid label band per type.
Hue groups the format family, lightness separates types within it.

- replace emoji file icons with PNG icons per format and role
- Add 15 PNG icons from bio-file-icons-v2 covering all file format kinds
- Create FileIcon component that maps format.kind and role to appropriate icon
- Create getFileIcon utility that returns format…

- paginated collapsible lists for large arrays (BAM coverage bins)
Replace boolean open/closed toggle with incremental pagination.
Each click reveals 20 more entries, updating the "+N more" link
instead of expanding everything at once. A thousand-entry BAM
coverage b…

- collapsible experiment grouping for BioProject runs

- unified NCBI download dialog with assembly components

- frontend types and client for unified NCBI resolve

- unified /ncbi/resolve endpoint

- assembly download launch service
Add assembly_service.launch_download, mirroring sra_service's shape:
validates the accession and component selection, creates the tracking
run, and enqueues the download_assembly job. Adds RunKind.ASS…

- ingest downloaded assembly components with their roles

- download_assembly job handler

- detect which components an assembly offers

- classify GCA/GCF accessions as assemblies
The resolver would previously send a GCF/GCA accession to esearch
db=sra, which finds nothing and returns a misleading "no runs found"
error. classify() now recognizes assembly accessions before the
I…

- explorer categories for annotation, protein and CDS files

- field vocabularies for annotation, protein and transcript roles

- add ANNOTATION, PROTEIN and TRANSCRIPT object roles
Prepares for NCBI GenBank/RefSeq assembly downloads, which yield a GFF3
annotation, a protein FASTA and a CDS FASTA alongside the genome FASTA
(already ObjectRole.REFERENCE). Protein and CDS FASTAs ar…

- install and probe the NCBI datasets CLI

- generate aligner parameter form and show resource warnings

- redesign tool selector as list and detail pane

- add client-side alignment resource estimate

- add aligner schema and envelope types and client calls

- serve aligner schemas and resource envelope, guard launch on block band

- add alignment resource estimator with warn and block bands

- dispatch aligner tools and index builders through the registry

- build bowtie2 and hisat2 alignment and index commands

- install and probe bowtie2 and hisat2

- add aligner registry with per-tool params, fields, and memory models
REGISTRY declares one AlignerSpec per Aligner: its tool probe, index
layout, params class, memory-estimation coefficients, and the form
fields the dialog will generate parameters from. Depends on
tool…

- add IndexLayout abstraction for suffix and prefix index shapes

- add bowtie2 and hisat2 aligner enum members and sidecar roles

- show read number on the paired-with row

- PairEditor control in the detail panel
Adds manual paired-end tagging UI for files whose names lack an R1/R2
marker, so pairing isn't limited to what filename inference can detect.

- read_number type and pair API client methods

- pair and unpair endpoints
Wires object_service.set_pair/clear_pair (from prior tasks) up as
POST/DELETE /objects/{id}/pair routes.

- clear_pair to undo a manual pairing

- symmetric writes for manual read pairing

- validation rules for manual read pairing
Task 3 of the paired-end tagging plan: set_pair validates self-pairing,
missing/cross-project mates, already-paired subjects on either side, and
non-reads objects (references and sidecars), returning …

- add PairRequest schema

- add read_number to DataObject

- header menu with manual storage cleanup

- project delete button with blast-radius confirmation
Adds ProjectDangerZone below Recent jobs in the project panel: a
Delete project button that, on confirm, fetches the deletion preview
(sub-projects/files/runs it would remove) before allowing the actu…

- deletionPreview and runScheduleNow client methods
Adds DeletionPreview/ActiveJob types and the deletionPreview client
wrapper. runScheduleNow was already present in client.ts from the
scheduled-jobs feature and matches the required signature, so no
c…

- project deletion-preview endpoint

- delete_project_tree, fixing the sidecar blob leak
Delegates each object to object_service.delete_object instead of detaching
blobs directly, so the sidecar cascade (e.g. a BAM's .bai) runs and its blob's
refcount actually reaches zero instead of bein…

- deletion_preview with active-job blocking

- collect_subtree for project deletion scope
Also fixes the shared beanie_models fixture (Task 1) to pin its Motor
client and dependent tests to a single module-scoped event loop.
Without this, pytest-asyncio 1.4's default per-function event loo…

- Help dropdown linking to BioFlow Calculations

- route for the calculations help page

- BioFlow Calculations help page

- show read quality in the detail panel header

- show read quality in the file listing

- quality badge component and tier colors

- assay-aware 1-5 read quality scoring

- connect paired-end reads visually in the file list

- add mate-aware ordering for the reads list

- expose read_number through the object API

- set read_number when linking mates
_link_mate already parses the R1/R2 token via pairing.split_mate to find
and link a paired-end file's mate, but never wrote read_number. The badge
in the file list is driven by read_number while the c…

- add read_number field to DataObject

- link "published assembly" in QC divergence warning to NCBI
Surfaces the assembly accession through facts (ncbi_assembly_accession)
so the QC tab's divergence warning can hyperlink "the published
assembly" to the NCBI Datasets page, reusing the existing
access…

- show longest and shortest sequence in the assembly panel

- record per-sequence FASTA lengths and assembly extremes

- label GC content by how it was actually sampled

- warn before a role conversion discards unsaved metadata

- wire the Results tab into DetailPanel for BAM objects
BamResults assembles AlignmentReport, the birds-eye and cumulative coverage
charts, the paginated per-contig table, insert-size/MAPQ histograms, and
provenance, with empty and prerequisite-blocked sta…

- birds-eye coverage, cumulative coverage, and per-contig table components
Hand-rolled SVG matching SequenceCharts.tsx's convention -- no charting
library. ContigTable paginates server-side against the TSV report rather
than the capped top-N slice in facts.

- frontend types and API client for BAM results

- bam stats result applier and report-serving route
Merges run_bam_stats facts onto the object, and serves the per-contig TSV
either paginated as JSON or as a full download -- reusing get_qc_report's
path-traversal guards, but without its sandboxed CSP…

- run_bam_stats job handler
Runs samtools idxstats, coverage, and binned depth over a coordinate-sorted,
indexed BAM. Resolves the .bai the same way call_variants does -- as a
sibling of the BAM via _resolve_blob(ctx.payload, "b…

- launch_bam_stats service function and eligibility checks
Refuses an unsorted or unindexed BAM with an actionable ValidationError
rather than auto-chaining index_bam, matching the documented precedent in
launch_variant_calling.

- bam_stats_dir setting

- MAPQ and insert-size histograms on the sampled alignment pass
Rides along on the existing bounded 200k-record sample in alignment_stats
rather than a fourth traversal of the BAM -- it already decodes every record
and already sums MAPQ, so capturing the distribut…

- bam_stats_runner pure functions
Command construction for idxstats/coverage/depth, output parsing, fixed-size
depth binning across the whole reference, cumulative coverage curve,
genome-wide summary, and TSV serialization for the per…

- wire extend_lease through the worker heartbeat and executor

- heartbeat renews to a per-job lease TTL

- JobContext records the lease a handler asks for

- apply_role_update records role as user-touched

- record which fields a user has explicitly set on DataObject

- add the variant calling dialog and wire it into the explorer
A BAM's detail panel gains a "Call variants" button that runs through the
same tool-selector flow trim and align use. The dialog reports the
inferred chemistry and the caller it implies, refuses CLR w…

- add variant calling API endpoints
The defaults endpoint reports whether the reference could be resolved,
so the dialog knows to ask for one rather than discovering at submit
time that it has to. Chemistry and reference are each resolv…

- add launch_variant_calling with chemistry-driven caller selection
Refuses CLR before anything is enqueued, and requires the .bai and
reference .fai to already exist rather than building them: both come from
jobs the user has already run, and an actionable "index it …

- add _apply_call_variants result applier
The VCF descends from both the BAM and the reference -- a variant call is
a claim about a position in a particular reference, and means nothing
without knowing which one. The .tbi attaches as a sideca…

- add call_variants job handler with Clair3 and bcftools paths
Reuses aligners.materialize for the reference and its .fai rather than
hand-rolling symlinks: both callers infer index paths from the filename,
which is the same problem the alignment handlers already…

- install Clair3 and bcftools, and probe them as tools
bcftools and tabix come from Debian; installing samtools does not bring
them along. Clair3 comes from bioconda via micromamba, the only arm64
distribution -- upstream's Docker image is amd64 only.

- add variant_runner with Clair3 and bcftools command builders
Caller selection is chemistry-driven: ONT and HiFi to Clair3, short and
unknown to bcftools, CLR refused outright rather than given a caller that
would produce confident wrong calls.

- add variant calling model vocabulary
ObjectRole.VARIANTS, SidecarRole.TBI, RunKind.VARIANT_CALLING and
RunJobRole.CALL_VARIANTS. A .tbi is to a VCF what a .bai is to a BAM, so
the existing sidecar machinery covers it with one new enum me…

- carry qc_read_chemistry from reads onto the aligned BAM
The align applier copied reads.metadata but not reads.facts, so the
chemistry QC inferred on the FASTQ was unreachable from the BAM it
produced. Anything picking a tool by chemistry -- variant calling…

- reconcile indexes before init_beanie at startup

- add index reconciliation module for startup-safe index changes

- build bwa-mem2 from source with sse2neon for arm64 support
On Apple Silicon (linux/arm64 Docker), bwa-mem2 was unavailable because
the upstream prebuilt binaries are x86-64 only and fail under Rosetta.
The tool selector showed it greyed out with a probe error…

- warn rather than block trimming a long-read file
fastp's adapter detection and length filters are built for short reads and
can discard most of an ONT or PacBio run under default settings, but
nothing said so -- _check_fastq_ready accepts any FASTQ …

- render long-read QC results instead of a blank panel
QcReport.tsx returned null unless qc_before_filtering was present, which
only fastp writes -- so a successful NanoPlot run on a long-read file
rendered nothing in the QC tab. QcFacts in types.ts never…

- infer read chemistry from QC numbers
Adds qc_stats.infer_chemistry(), a pure function that reads the mean read
length and quality NanoPlot already reports and returns a ReadChemistry
plus a short human reason ("15.2 kb reads at Q31 -- Hi…

- TrimDialog renders per-tool parameter fields for cutadapt and Trimmomatic

- trimDefaults takes a tool argument

- add CutadaptParams and TrimmomaticParams frontend types

- accept a tool on POST /pipelines/trim and GET /pipelines/defaults

- record which tool a trim run used on PipelineRun

- launch_trim and default_params take a tool parameter

- dispatch trim_reads across fastp, cutadapt, and Trimmomatic
Restructures trim_reads from a fastp-only handler into a dispatcher on
the payload's `tool` field, mirroring run_qc's platform dispatch. Adds
_run_fastp_trim, _run_cutadapt_trim, _run_trimmomatic_trim…

- mark cutadapt and Trimmomatic as runnable now that handlers exist

- add Trimmomatic command builder and -summary file parser

- add cutadapt command builder and report parser

- add Trimmomatic PE/SE paths and adapter directory setting

- add a tool selection screen before Trim and Align
Implements tool-selector-implementation.md. Between the panel button and the
parameter dialog, a radiogroup of tool cards -- per-tool summary and
strengths, disabled cards showing why (not installed v…

- SRA download dialog, and tests for the resolver and handler
Steps 9-13 of sra-downloader-implementation.md, completing the feature.

- download sequencing runs from NCBI SRA (backend)
Steps 1-8 of sra-downloader-implementation.md: everything server-side. The
frontend dialog is next.

- add a QC pipeline and describe the trim tools
Implements pipeline-tool-additions-qc.md Phases 1 and 2. The SRA downloader
plan names run_qc as its step-0 prerequisite, so this lands first.

- split the file details panel into QC, Metadata and Actions
The panel rendered thirteen sections in one scroll for every file kind, so
answering "did this BAM align well" meant scrolling past format detection,
checksums and storage paths to reach the mapping r…

- show the activity view as runs rather than loose jobs
One row per action the user took, expandable into the jobs that served it.
An alignment that produced seven rows now reads as four lines: Build index,
Align, Index BAM, and "4 files ingested" -- the p…

- group jobs into the run a user asked for
Clicking Align once produced seven rows in the activity view: an index
build, the alignment, a BAM index, and one header parse per produced file.
The view showed the machine's decomposition rather tha…

- align reads and manage reference indexes from the explorer
An Align button on ready FASTQ opens a dialog that defaults everything it
can from the server: the read group from the file's own metadata, the
preset from its platform, and the aligner from what is a…

- launch alignments, building the reference index first when missing
Adds launch_alignment and launch_build_index plus the API surface: tool
availability, per-object defaults, reference listing with index status, an
eager index build, and the alignment itself.

- align reads against a reference, producing a sorted indexed BAM
Three handlers: build_index produces sidecars for a (reference, aligner)
pair, align_reads pipes the aligner into samtools sort, and index_bam adds
a .bai and reads flagstat while the file is already …

- materialize references for aligners, and probe the alignment tools
Content-addressed storage keeps every blob alone under its hash with no
extension and no siblings; bwa-mem2 finds its index by appending suffixes to
the reference path and samtools wants a .fai beside…

- model sidecars as scaffolding attached to their parent
A `.bwt` is not a specimen. It exists only to accompany its reference,
means nothing alone, and is rebuildable at will -- unlike a trimmed FASTQ,
which is a file you search, annotate and align. `sidec…

- hold jobs until their dependencies succeed, and admit by weight
Alignment needs a reference index that may not exist yet, so the alignment
job has to wait for the index build. `delay_seconds` is a timer, not a
dependency, and waiting on a guess would dispatch agai…

- add the trim dialog, before/after report, and lineage panel
A Trim button on any ready FASTQ opens a dialog with the mate already
detected and pre-selected. Defaults come from the server rather than
being duplicated in the form, where they would drift from the…

- add an activity view showing what is running and why
A new /activity route with three sections: running jobs with live
progress, waiting jobs, and recent history. Rows lead with the file
name rather than the job type -- "NA12878_WGS_R1.fastq.gz +
NA1287…

- add pipeline endpoints for launching trim runs
POST /pipelines/trim queues a run over a FASTQ file or an R1/R2 pair.
GET /pipelines/tools reports resolved paths and versions so the launch
dialog can say "fastp is not installed" before a user commi…

- expose job logs and an active-states filter
logs/ has existed since Phase 0 and nothing had ever written to or read
from it. GET /jobs/{id}/log returns the tail, seeking to the end rather
than reading the file: a long fastp run's log reaches hu…

- turn finished trim runs into linked objects
_apply_trim_reads takes the handler's result and does the database work
the worker thread could not: ingests each produced file into CAS, links
it to both parents via derived_from, records the run in …

- add the trim_reads handler
Runs fastp over a FASTQ file or an R1/R2 pair. SUBPROCESS mode, COMPUTE
class, IoClass.HEAVY, max_attempts=2 -- a fastp failure is almost always
deterministic, and spending five attempts on a multi-ho…

- install fastp and fastqc, and probe them at runtime
Tools come from Debian rather than bioconda: the versions are current
(fastp 0.24.0, FastQC 0.12.1) and it avoids carrying a conda install for
two binaries. fastqc's JRE is ~200MB of the image on its …

- add object lineage, mate links, and an ingest path for produced files
Three fields on DataObject: derived_from (a list, because paired
trimming takes two inputs and produces two outputs each descending from
both), produced_by_job, and mate_object_id. Provenance is a typ…

- add a compute job class and streaming subprocess output
Groundwork for pipeline execution. Two independent pieces, both dormant
until a handler uses them.

- show the published NCBI assembly beside the measured file
Our parser measures the file on disk; NCBI describes the published
assembly. These legitimately differ -- a file may hold primary
chromosomes only, be filtered, or sit at a different patch level -- so…

- auto-assign the reference role from an assembly accession
Ingest now runs the assembly enricher alongside the SRA one and applies
its results: metadata onto the object, NCBI stats and provenance into
facts.

- enrich reference files from the NCBI assembly record
enrich_from_assembly is the sibling of enrich_from_sra and obeys the same
governing rule: enrichment never overwrites what a person entered. A field
that already holds a value is left alone, and where…

- add NCBI-sourced fields to the reference schema
tax_id, assembly_level, assembly_date, and paired_accession are filled
in by the NCBI assembly lookup rather than typed by hand, so none are
marked suggested -- they should appear once enrichment has …

- add assembly accession detection and NCBI lookup
Reference genomes downloaded from NCBI carry the assembly accession in the
filename, and NCBI holds far better metadata than anyone will retype:
organism, strain, assembly name, submitter, release dat…

- render reference-specific detail panel

- add the reads/reference conversion control

- add curated assembly facts display for references

- link assembly accessions to NCBI datasets

- scope the metadata editor schema to the object role

- categorize files by role in the project explorer

- add object role to the frontend API types

- accept a role query param on the schema endpoint
schema_for_api has taken a role since the previous commit, but no HTTP
route passed one, so the parameter was unreachable from outside the
process. The UI form fetches its fields from this endpoint an…

- make metadata schema resolution role-aware
Closes the runtime break left by the previous commit: update_object
already calls coerce_and_validate(..., role=obj.role), which raised
TypeError on every metadata save because the schema layer had no…

- apply role updates, distinguishing null from omitted
Every other field in update_object uses `.get(k) is not None`, which can't
tell an explicit null from a key the client never sent. That's fine for
name/tags/metadata, but role needs the opposite: clea…

- expose object role through the API schemas
ObjectUpdate and ObjectOut now carry role, so the wire contract keeps
pace with the storage field added in ed73490.

- add ObjectRole field to DataObject
Adds a role field so a user can explicitly declare a file's category
(e.g. reference genome) when it can't be derived from the detected
format alone -- a reference and a set of reads can both be FASTA…





### 🐛 Bug Fixes

- *(launcher)* drop AppImage from the Linux bundle targets
`npm run tauri build` on this machine failed with `failed to run
linuxdeploy` after prompting an unexpected GUI dialog mid-build. Root
cause: AppImageLauncher, a desktop-integration daemon present on …

- *(launcher)* find an existing install on relaunch instead of re-setup

- *(launcher)* stop Run/Stop/Update/status freezing the window
status, run_stack, stop_stack, update_stack, run_first_setup, and
apply_settings were all plain synchronous #[tauri::command]s that shell out
to `docker` (Command::output(), plus up to a minute of thr…

- *(launcher)* create the storage folder instead of rejecting it
The setup wizard's storage-location field hard-blocked Install whenever the
path didn't exist yet -- which is every fresh machine, since both defaults
`SetupDefaults` proposes (`~/BioFlow`, `~/.bioflo…

- exclude index sidecars from a prior run's listed outputs
Checked against a real project's align card, per CLAUDE.md's rule for
suggestion-rule changes: the card listed a BAM alongside six aligner
index files (.fai, .0123, .amb, .ann, .bwt.2bit.64, .pac) as …

- honor Platform filter on NCBI organism search, add assembly Level filter
The Platform dropdown was accepted by the single-accession lookup but never
plumbed through organism-name search (the assemblies + sequencing runs
screen), so picking Nanopore and searching by organis…

- allow inline scripts in NanoPlot report CSP so plots render
NanoPlot's report loads plotly.js from a CDN script but draws each
individual plot (weighted read-length histogram, etc.) via its own
inline <script> calling Plotly.newPlot() with no nonce or hash. Th…

- stop download icon svgs inheriting diagram sizing rule
.workflow-diagrams-frame svg matched any svg descendant, including the
small download-icon svgs sharing the frame with the diagram -- it
stretched them to width:100%/min-width:1100px, covering the who…

- keep the Broadsheet facts-table underline continuous across wrapped rows
.kv used align-items: baseline, which lets dt and dd shrink-wrap to their
own content instead of stretching to the row height. When dd wrapped to
more lines than dt (e.g. a multi-line chip list under …

- clear flye's probe cache in reset_cache()
flye was the only tool probe missing from reset_cache(), an oversight
from when it was first added -- every tool since (miniprot, compleasm)
got its cache_clear() call.

- accept ?profile= for routes opened as plain links
QC/BAM/VCF report links and file downloads are plain <a href> elements
opened by the browser directly, so the JS that attaches X-BioFlow-Profile
to fetch() calls never runs -- every such link 400'd wi…

- widen solo tool entries to fill the empty second column
.is-solo (previous commit) fixed the card's vertical position for a tool
with no row-mate, but only checked flye's case: alone in its whole
section, which auto-fit already gives full width by leaving …

- top-align the facts card for a solo tool entry
Bottom-pinning the card to line entries up in a shared row (previous
commit) also caught the odd tool out at the end of a section -- flye
alone in Assembly stretched to its own long prose's height and…

- allow Plotly CDN script for NanoPlot QC reports
NanoPlot's report has no static-image fallback -- every plot is a Plotly
figure loaded via a CDN <script> tag -- so it rendered as a near-blank page
under the sandboxed CSP shared with FastQC/fastp, w…

- warn when the align dialog's tool selector cannot use the file's chemistry
PipelineToolSelector reads facts.qc_read_chemistry off the object (when
launched for align) and warns above the tool rail whenever the focused
aligner is short-read-only (bwa-mem2, bowtie2, hisat2, st…

- maintenance jobs belong to the installation, not to one profile
`scheduler.tick` and `run_now` enqueued GC and file verification with a
hardcoded `owner="local"`. That reads as "the installation", but "local" is
not a neutral value -- it is the owner string of whi…

- the worktree test script's appended -q hid pytest's summary line
`pytest "${@:-tests/}" -q` appended a -q after the caller's own args, and
pytest's verbosity is additive, so both invocations the script documents came
out wrong:

- a phase change must not be dropped by the progress throttle
Found by running a real assembly. The job sat at "starting" for its entire
six-minute life: the handler reported "starting", Flye's `configure` and
`assembly` banners both arrived inside the same seco…

- launch_assembly built an invalid RunInput, 500ing on main
`RunInput` requires `name`; launch_assembly passed only object_id and role. All
2334 tests passed and the endpoint returned 500 the first time it was actually
called. The bug was live on main.

- genome-size inference, as the real library required
Two problems, both found by running the rules against real objects rather
than fixtures, and neither visible to the unit tests that existed.

- derive the Indexes panel's aligner list from the backend's keys
The panel hardcoded `["bwa-mem2", "minimap2"]` and had been stale since
bowtie2 and hisat2 landed; STAR made it two of five. The backend has
always keyed `reference_index_status` over every `Aligner` …

- reclaim the owner-scoping tests' scratch files
`ingest_local_file` consumes the file it is handed, so the happy path in
test_object_service_owner.py cleaned up after itself. The owner check
raises before ingest reaches the rename, though, which le…

- name the MAPQ scale wherever a STAR alignment's MAPQ is shown
Four surfaces presented STAR's locus codes as though they were phred
scores: the facts table's Mean MAPQ row, the mapping-quality histogram,
the per-contig Mean MAPQ column (samtools coverage averages…

- stop reporting a mean over STAR's MAPQ codes as a quality
STAR writes 255 for a uniquely mapped read and 3/1/0 for 2, 3-4 and 5+
loci -- ordinal codes, not phred scores. Averaging them produced
mean_mapping_quality 246.59 on a real yeast alignment, against ~…

- derive the help page's sections instead of mirroring PipelineType
The same fact was written down in three places, and main had two of them
wrong. `/help/software` rendered from a hardcoded GROUPS list, so
featureCounts and pydeseq2 were invisible on the page; Pipeli…

- stop telling users a genome needs fetching when two are present
`resolve_reference` refuses when a project holds several distinct references,
which is correct and deliberate -- the card picks on its own, so it declines
rather than guess between distinct genomes (d…

- the quantify dialog must not pick an annotation the server declined to
Found by running the counts step against a real project rather than by a
test. The backend gets this right: when a project holds annotations for more
than one assembly it returns `annotation_id: null,…

- three bugs the expression vertical only showed when run
All three passed the suite and appeared the moment real data and a real
browser were involved, which is the pattern CLAUDE.md keeps recording.

- reserve memory per aligner and genome, not a flat 8 GB
The piece docs/TODO.md's aligner entry asked for and the STAR change did not
deliver. Both align handlers declared mem_mb=8192 whatever the aligner and
whatever the reference, so the governor could no…

- store STAR's index sidecars, and fail loudly when they are missing
Found by running a STAR alignment in the real app rather than by a test.
`build_index` finished green having stored nothing: all eight index files
were dropped, and the failure surfaced a second later…

- pick the DeepVariant image by architecture, and resolve host.docker.internal on Linux
Verified the "Build and run on Linux" TODO entry against a real x86-64 Linux
stack rather than by audit. Four of its five predicted problems were
non-issues here -- the Dockerfile's arm64 branch corre…

- let every applier accept the keyword apply dispatches with
_APPLIERS dispatch calls handler(result, owner=...), but eleven of the
fourteen appliers declared launching_owner -- raising TypeError the
moment their job finished. _apply_result catches and logs it,…

- let the api probe Docker, and re-index over an existing .tbi
Two failures found only by running a real DeepVariant job end to end.

- only offer proteomes that can actually be downloaded
The strain picker could not work. A non-reference proteome's entries live
in UniParc but not in UniProtKB's searchable index, which is what both the
count and the download query go through -- so `prot…

- price a picked proteome so its reviewed choice appears

- a very long number is an empty result, not a 500

- do not retry a request UniProt has already rejected

- refuse a request naming both a proteome and accessions

- survive a UniProt field that is not the shape expected

- remove every quote from an organism name, not just the edges

- stamp the owner on upload sessions so deletion can reach them
upload_service.create_session built its UploadSession without an owner, so
every session took the "local" default from TimestampedDocument regardless
of whose project the upload was for. Harmless whil…

- restore path and stat metadata in binary fingerprint
Content hash alone dropped path identity (two tools resolving to the
same binary, or one tool moving PATH locations, fingerprinted
identically) and missed wrapper-script upgrades where only the
dispat…

- a stale profile id is a 404, not a validation error
_load answered both "that is not an id" and "that id names nothing" with the
same 422 validation_error. The second is the expected steady-state failure --
a profile id remembered in localStorage goes …

- make the adopted profile undeletable, and prove three guards
Deleting the adopted profile had no recovery path. It owns `owner:
"local"` -- every document from before profiles existed -- and once it is
gone nothing carries `adopted_legacy_owner=True`, so `get_c…

- ingest results under the parent's owner, not "local"
Every applier here resolves a parent object by id and then hands
ingest_local_file that parent's project_id -- but a literal
owner="local". Once a second profile exists those two disagree, and
ingest_…

- raise an AppError from deps so the picker can branch on it
deps.py was the only file in the backend still raising FastAPI's
HTTPException. That shape is `{"detail": ...}`; every other error here
goes through the AppError hierarchy and emits `{"code", "message…

- mount /data in the worktree test runner, and use it throughout the plan
The throwaway test container never mounted BIOINFO_HOME, so tests touching
reap_report_dirs operated on a tmpfs their assertions knew nothing about and
failed for entirely the wrong reason. The mount …

- put the annotate_variants decorator back on its handler
A helper added between `@handler("annotate_variants", ...)` and the
function it was meant to decorate captured the decorator instead, so the
queue registered `_csq_line_logger` under that name. Everyt…

- make the csq seam guard assert the card, and steady the filter
The guard read the probe back and stopped there, which only proves
`patch` replaced an attribute -- it always does. It would have passed
just as well if build_annotate_card reached a different referen…

- register the annotated VCF and stage the reference index

- require a GFF3 and a confirmed assembly match

- detect BCSQ by shape, not by field count

- narrow the duplicate-id marker and say why csq is not in all_tools
Code review raised two things.

- rank unknown consequence types above the benign tail

- give NanoPlot a longer probe timeout than the default
NanoPlot imports pandas, scipy and plotly before it will print a
version: 16.3s in a cold container against 2-4s warm. The 10s default
therefore failed it exactly when the app was starting up and prob…

- don't call a probe timeout 'not installed'
NanoPlot is installed and works -- it is a Python entry point that takes
~13s to answer --version, past the 10s probe timeout. _probe reports a
binary it could not run the same way as one it could not…

- size the facts rail's rule off the entry, not the viewport
The rail flips from a top border to a left border when auto-fit seats it
beside the prose. That depends on the entry's width, which the left
panel changes without the window moving -- so a viewport qu…

- drop the duplicate E-utilities docs link and a stray double space
homepage and docs pointed at the same NBK25501 page, which would render
as two links to one place.

- recognise bacterial and viral genomes as chromosome sets
classifyChromosomes required five sequences over 100 kb before it would
draw anything. That describes a eukaryote and nothing else: a bacterium
has one chromosome, so no prokaryotic or viral reference…

- accept NZ_ accessions, which carry letters in their bodies
The pattern grouped NZ_ with NC_/NT_/NW_/AC_ and gave them all a digit
body. That is right for the prefixes that number their own records, but
NZ_ wraps an underlying INSDC or WGS accession and keeps …

- keep alternate alleles distinct in marker labels

- remove per-object report directories when an object is deleted
delete_object cascaded sidecars and detached the blob but never touched
the Results directories keyed by object id under qc_reports/, bam_stats/
and vcf_stats/. Those sit outside objects/ so they are …

- make variant density chart readable for long-tailed bin counts
Linear scaling against the max bin renders almost the whole track as a
flat line -- verified against real data (6,641 variants, one bin of 205,
median non-empty bin ~2), a bin of 2 rendered under a pi…

- resolve DP from FORMAT or INFO so Clair3 VCFs work

- match vcf_stats_report test fixture to the real bcftools query column order

- pass appname so the viewer stops warning on every open
Without it NCBI opens a modal warning ("initialized with parameter
appname=undefined") on top of the tracks each time a chromosome is
opened. Verified gone in the browser.

- count multiallelic indels so the table agrees with the summary

- make the chromosome bars keyboard-operable and label them
The bars were SVG <g> elements with onClick and no tabIndex or onKeyDown,
so there was no keyboard path to any chromosome at all. The overflow
<select> is focusable but holds only sequences ranked 25+…

- instantiate the Sequence Viewer the way NCBI documents
The modal used NCBI's declarative embedding form -- a div with
class='SeqViewerApp' carrying data-id/data-tracks/data-width. Their docs
rule that form out for a div that starts hidden, which a modal's…

- use true sequence_count in the not-chromosomal message
The FASTA parser (backend/app/storage/parsers.py, MAX_STORED_CONTIGS = 50)
stores at most 50 entries in facts.sequence_lengths, so entries.length was
reporting the truncated map size instead of the re…

- spread allocate_bins' rounding excess instead of dumping it on the last contig
Per-contig roundings could sum a few bins past bin_count, and the old
correction subtracted the whole excess from the last contig alone --
enough to drive it to 0 or negative bins and crash bin_depth …

- degrade allocate_bins' per-contig floor when contigs outnumber bins
When len(contig_lengths) > bin_count, the one-bin-per-contig floor
overflowed: the correction line drove the last contig's count negative,
and bin_depth's cumulative offset then walked start_bin past …

- restore the missing vite-env.d.ts
Vite's client types were never declared, so tsc had no type for the 16 PNG
imports in src/icons and npm run lint failed on every one. Vite resolved
them fine, so nothing was broken at runtime -- but t…

- stop the manage grid overflowing a narrow panel
A grid item and a flex item both default to min-width:auto, so the control
column would not shrink below its widest child -- TagEditor's input, whose
size=20 gives it a ~159px floor -- and pushed the …

- count references by assembly and role, not by FASTA file
Two bugs the unit tests could not see, both found against live data.

- refresh pipeline suggestions when a file's role changes
Converting a file to a reference gives every set of reads in the project
something to align against, so it changes the align card of files other
than the one being converted. Uses the broad ["suggesti…

- guard the Role row like the pairing row
RoleConverter renders null on a BAM or VCF, so placing it last left the
label sitting over nothing rather than removing the row. canConvertRole
mirrors canPair, and the component now shares its condit…

- let the suggestion grid survive one failing builder
The grid is advisory -- everything on it is also reachable through the
Computations section -- so a contract drift in one card should not 500 the
whole Actions tab. Each builder is now isolated and lo…

- keep paired-read brackets aligned when a row is selected
The spine and tick shifted 10px -> 12px on .selected to clear the
selection edge, so selecting one half of a pair moved its bracket
relative to its mate and broke the line across the pair.

- restore missing page outline on file type icons
The icons were rasterized with ImageMagick, whose SVG renderer silently
ignored the stroke attribute -- the shipped PNGs contained no outline
pixels at all. Since the paper fill sits at ~1.1:1 against…

- list each program once in the QC program chain
A BAM's PG header has one line per invocation, so a tool run several
times (samtools especially) showed up several times in the Programs
field. Which tools touched the file is the useful part, not how…

- preserve precision for fractional values in FactsTable
formatNumber was a bare toLocaleString(), which caps fractional digits at 3
by default. Any fact whose value is a small decimal lost precision in the
generic key/value table: 0.8712 rendered as "0.871…

- render nested facts as readable text instead of [object Object]
Suppress BAM results facts that already have dedicated renderers
(coverage charts, cumulative chart, summary row, contig table) to avoid
rendering them as unreadable [object Object] rows in the generi…

- reset collapsed group state on a fresh resolve

- add missing one_liner for the datasets TOOL_META entry
The ncbi-genbank-refseq-download branch was written before one_liner
existed (added by a concurrent aligner branch that merged into main
first), so datasets' entry was missing it and the tools-panel i…

- remove stray route registration for sra_download inside ncbi.py

- run blocking assembly lookups off the event loop in launch_download

- exclude all genome-specific metadata keys from non-genome assembly components
tax_id, assembly_date, and paired_accession are produced by
AssemblyMetadata.to_metadata() and listed in REFERENCE_FIELDS just like
reference_build and assembly_level, so they belong in the genome_onl…

- use is_relative_to for the zip path-traversal guard

- fall back to filename labeling when the assembly catalog isn't a dict
json.loads succeeds on valid-but-non-dict JSON (e.g. the literal null), and
payload.get("assemblies") then raises AttributeError -- not caught by the
existing (ValueError, OSError, TypeError) tuple --…

- send the full merged params on launch, not just manual overrides
selectedTool (the aligner chosen in PipelineToolSelector) was merged into
the dialog's display params but never written back into the overrides
state that launchAlignment actually sent. A launch with …

- keep performance fields visible during a resource warning, narrow the field-value cast

- round the aligner-side breakdown number, matching Python's :.0f formatting

- match Python's ASCII x rather than Unicode × in the estimate message

- serialize one_liner in the tools API response
tool_with_meta() forwarded summary/strengths/runnable from TOOL_META
but dropped one_liner, so GET /pipelines/tools omitted it even though
every TOOL_META entry (and the frontend's now-required Pipeli…

- round memory estimates up and broaden the dominant-term heuristic

- guard against unwired aligners reaching AlignParams dispatch
Both launch_alignment and align_reads called the AlignParams alias
(Minimap2Params.from_dict), which always builds minimap2 params and never
reads the aligner key. A request naming bowtie2/HISAT2 -- v…

- post-merge fixes for paired-end tagging test suite and merge leftover
Two issues surfaced only after merging with main (which independently
shipped read_number inference while this branch was in flight) and
running the real container-backed test suite:

- inference no longer overrides a cleared pairing
_link_mate's docstring promised that a user-set link is never overwritten,
but guarded only against an existing pointer -- so a cleared pairing was
silently re-inferred on the next ingest. Checks user…

- prevent deletion-preview query from looping under StrictMode

- add cancel button to deletion-preview loading state
The loading branch (!preview.data) was the only one of four confirming-state
branches missing the cancel button, so a hung or failing deletion-preview
fetch left no way to back out of the confirm scre…

- route cascade delete through delete_project_tree

- scope reads reordering to the Reads category only
Final review caught that orderWithPairs was applied to every file
category via the shared CATEGORIES.map block, silently changing
References/Alignments/Variants/Annotations/Hi-C/Other from the API's
n…

- auto-index BAMs before computing Results, fix has_index race
The Results tab told users to index a BAM "from the Align button or the
Metadata tab" but neither exposes an indexing action. Compute Results now
queues index_bam first when a .bai is missing, then ch…

- run fixmate before markdup for paired-end alignments
samtools markdup relies on the ms (mate score) tag that only
samtools fixmate -m writes, and fixmate needs name-sorted input
while markdup needs coordinate order -- so paired-end alignment
was failing…

- sample FASTA GC content across the file, not its prefix

- clear the cancel flag when a blocked job fails on its dependency

- clear the cancel flag when the reaper marks a job dead
A job that dies via lease-expiry after exhausting retries is marked
DEAD directly in Mongo, bypassing release.lua's SREM cleanup. If it
had been cancel-requested, its id was left in bp:cancel forever,…

- re-ingest no longer re-asserts a reference role the user cleared

- pin modal actions to bottom with flex column scroll body
The Align dialog's submit button scrolled out of view when 'Aligner and
performance' was expanded — 822px of content in a 633px max-height. The
whole modal scrolled as one unit, taking the primary act…

- don't warn about cutadapt on long reads -- it isn't short-read-tuned
Found manually testing DRR1078403 (ONT): the long-read trim warning fired
for cutadapt too, but cutadapt's own tools.py summary explicitly
advertises "Works on any platform (Illumina, PacBio, ONT)" an…

- two bugs found testing DRR1078403 (Oxford Nanopore) manually
QC report links 404'd. qc_fastp_report, qc_fastqc_report, and
qc_nanoplot_report were all stored as "<object_id>/<relative path>", but
get_qc_report's `root` is already qc_reports_dir/<object_id> and
…

- align PacBio HiFi reads with map-hifi instead of map-pb
Every PacBio file was aligned with map-pb, tuned for CLR's ~85-88% base
accuracy, even when the reads are HiFi/CCS at ~99.9% accuracy -- silently
poor alignments rather than an error. Adds map-hifi an…

- surface PipelineRun.tool through the runs API and its frontend type
RunOut.of() enumerated every PipelineRun field by hand and silently
dropped tool, so GET /runs and GET /runs/{id} never returned it --
code review of the field's introduction caught that it was persis…

- remove dead branch in default_params, document _trim_tool's invariant
Code review of Task 9 flagged default_params' if/else as a no-op --
both branches returned the identical expression. Collapsed to one
return. Also documents that _trim_tool assumes its caller already
…

- read instrument models as platforms, and show in-flight pipeline work
Three fixes from using the align dialog on real SRA-enriched reads.

- make the header brand a link home
The brand was a plain div, so from a full-width view like /activity there
was no way back to the file explorer other than editing the URL --
clicking the logo is the convention people reach for first.

- link paired-end reads to each other at ingest
parsers._infer_pair_hint has recorded "this looks like an R1" since
Phase 3, with a comment promising pairs could "be matched later" -- but
nothing ever matched them. This keeps that promise.

- assign the reference role conditionally to survive a concurrent edit
The applier reads the object before a network lookup that can take
seconds. A user converting the file in that window would be silently
overruled by the stale snapshot -- the exact thing the never-ove…

- drop wrong-typed leaf values instead of writing them to metadata
The _obj guards stop a wrong-typed container from raising, but a leaf
arriving as a dict or list passed straight through into user-editable
metadata -- a dict in a text field nobody can correct by han…

- use indexed keys for the contig list; record conversion UX follow-up
A malformed or concatenated FASTA can repeat a contig name, and duplicate
React keys would misrender when toggling the show-all view.




















<!-- generated by git-cliff -->
