# Changelog

All notable changes to BioFlow, generated from Conventional Commits by
[git-cliff](https://git-cliff.org). One entry per commit: the subject plus
the first paragraph of the body where one exists; only `feat` and `fix`
reach the notes. See AGENTS.md "Release notes" for the contract.

## [0.6.0-beta] - 2026-08-26




### 🚀 Features

- *(ui)* draw the agent panel's header controls, not emoji (#833)
* feat(icons): draw agent settings, delete chat and restart agent





### 🐛 Bug Fixes

- *(queue)* rebuild reservation counters on reconcile, not leak them forever
The `bp:conc:{cpu,mem_mb,io_heavy}` counters are the admission gate claim.lua
reads to decide whether a job fits. claim.lua INCRBYs them on grant and
release.lua DECRBYs on every terminal outcome, but…

- *(ui)* say when a strip is missing chromosomes, and backfill stored ones
Two loose ends from drawing only an assembly's core chromosomes.

- *(ui)* draw a reference's actual chromosomes, not its longest sequences
The strip ranked every sequence by length and drew the top 24. For a human
reference that mixed chromosomes with scaffolds and patches, captioned the
sex chromosomes "23" and "24" from their accession…

- *(metadata)* label every chromosome, not one chromosome's scaffolds
NCBI's `sort_order` is the chromosome number, not a global rank: on GRCh38
all 44 scaffolds, patches and alt loci of chromosome 1 also report
`sort_order: 1`. Sorting on it alone and truncating at 50 …

- *(metadata)* label every chromosome, not just the first and its scaffolds
A GRCh38.p14 reference drew accession digits ("664", "665") where
chromosome numbers belonged. `sequence_labels` held 50 entries covering
only 10 of the 50 sequences the strip draws, so every chromoso…

- *(ops)* run CI on PRs into alpha, beta and release branches
Both gating workflows were scoped to `main`, so a PR into alpha/X.Y.Z or
beta/X.Y.Z ran nothing but cla-check: no frontend check, no backend tests,
no launcher build, no Conventional Commits check. `g…

- *(api)* repoint a node's stale Mongo URL when updating it, not never
A compute node's .env is written exactly once, at provisioning, and nothing
ever rewrote it afterwards. So a node provisioned before #803 keeps the
Docker bridge address that bug wrote -- on the node,…

- *(agent)* let the agent panel actually reach the agent (#828)
* fix(agent): spawn the agent on projects that have finished jobs

- *(ui)* make the agent panel opaque instead of see-through
The agent drawer styled itself with `var(--surface)`, `var(--surface-alt)`
and `var(--text-secondary)` -- a variable vocabulary this app's `:root`
never defines. An undefined custom property makes `ba…

- *(ui)* keep the "no QC" prompt inside the panel at any width
The prompt's row did not wrap, so on a narrower detail panel the Run QC
button ran off the right edge instead of dropping below the text.

- *(ops)* accept -alpha and -beta in the VERSION semver check
test_version_file_exists_and_is_semver required VERSION to match a bare
^\d+\.\d+\.\d+$, but ops/release.sh validates
^[0-9]+\.[0-9]+\.[0-9]+(-alpha|-beta)?$ and bump_version.py writes that
suffixed v…

- *(icons)* raise the agent icon so its face reads as vertically centred
The agent glyph was centred on its ink bounding box, but the antenna is a
hairline and the robot head is a solid block, so the mass the eye tracks
sat well below the geometric centre: head centre y=15…

- *(ops)* bump the launcher lockfile on release, not just its package.json
launcher/package-lock.json still declared 0.1.0 while package.json said
0.6.0-alpha. The lockfile sync added in #491 is gated on `$LINE = app` and
points at frontend/ only, but bump_app calls bump_lau…

- *(launcher)* report the version it actually is, on every screen
The launcher showed "Launcher 0.1.0" on six screens and nothing at all on
the Running screen -- the one a user looks at most. The string was a
literal, copied into five components at the first release…


## [0.6.0-alpha] - 2026-08-24




### 🚀 Features

- *(ui)* choose the assembler in the assemble dialog
The dialog rendered whichever assembler `default_assembly_params` chose
and offered no way to change it, so SPAdes and MEGAHIT -- both
installed, probed, resource-modelled and tested -- were reachable…

- *(pipelines)* assemble short-read communities with MEGAHIT

- *(pipelines)* score metagenome bins with CheckM2
A bin is a hypothesis. MetaBAT2 will emit forty of them from noise, and
nothing about a FASTA says whether it is a near-complete genome or a pile of
misassigned contigs. CheckM2 gives each bin a compl…

- *(pipelines)* bin a metagenome assembly into per-organism genomes
A metagenome assembly is one FASTA holding contigs from many organisms at
once. The unit a biologist wants -- a MAG, one organism's draft genome --
does not exist until binning separates them, and not…

- *(pipelines)* add metaSPAdes (--meta) for short-read communities
SPAdes ships with a metagenome mode this app never exposed, so a mixed
short-read community could not be assembled at all.

- *(taxonomy)* support taxonomic labeling on FASTA contigs and bins (#730)

- *(ui)* compare BUSCO, quality and depth charts between objects
Stage 2 of the object-comparison work: after the Nx overlay (stage 1),
extend the comparison view to overlay three more charts that two objects
of the same kind can carry -- BUSCO completeness bars, p…

- *(pipelines)* implement variants-in-regions, annotation comparison, and sequence extraction
Implement Stages 2, 3, and 4 of #632 for seqkit/bedtools user features:
- Stage 2: variants in regions (bedtools intersect between VCF and Annotation)
- Stage 3: annotation comparison (bedtools jaccar…

- *(ui)* overlay Nx curves for two-object comparison
Stage 1 of #645: the narrowest useful comparison. Add a comparability
table (comparableCharts.ts) keyed on facts, a ?sel=object:A&cmp=object:B
route that overlays two assemblies' Nx curves on shared a…

- *(pipelines)* merge N StringTie transcript assemblies
Add stringtie --merge as a project-level operation: pick N per-sample
assembled-transcript GTFs, get one non-redundant annotation. It is the
standard prerequisite for quantifying a multi-sample RNA-se…

- *(pipelines)* haplotag a phased VCF onto its alignment with whatsHap
reads the PS tags a phased VCF already carries and stamps them onto the
reads of one alignment, producing a haplotagged BAM (not a VCF, unlike
phase/polyphase).

- *(pipelines)* add metaFlye (--meta) for community assembly
Flye is installed, probed, and registered, but had no metagenome mode
exposed -- assembling a mixed-community sample was not possible at
all. Adds a `meta` ParamField to FLYE_SPEC, wired through to `-…

- *(frontend)* add the unlabelled per-contig GC-vs-coverage blobplot

- *(pipelines)* store the capped per-contig blobplot report

- *(pipelines)* aggregate the GC-depth join per contig with a length cap

- *(pipelines)* add Filtlong long-read filtering tool

- *(frontend)* show the GC-vs-coverage bias curve on BAM results
Renders the gc_bias job's mean-depth-by-GC-bin curve as a line/area chart
on the BAM results panel, gated on gc_bias_status/gc_bias_curve the same
way CoverageDepth is gated on its own fact. Closes ou…

- *(pipelines)* register gc_bias in the canvas and running-now registries
gc_bias already had a provenance_walker._NO_NARRATIVE_STEP entry from
an earlier task; this adds the two remaining registrations so the
canvas node type and running-job Launch-button greyout work for …

- *(pipelines)* offer the gc_bias card with three named refusals
Adds build_gc_bias_card to the Actions-tab suggestion pipeline, gated
on FormatKind.BAM like build_coverage_card and on a resolved
alignment_target like build_consensus_card. Independently mirrors
lau…

- *(api)* expose the gc_bias launch route

- *(pipelines)* launch gc_bias with three named refusal reasons
Resolves the BAM's alignment target, its reference's gc_tracks, and the
BAM's own windowed coverage report entirely in the launcher, since the
gc_bias handler runs in a thread and cannot reach the dat…

- *(pipelines)* compute and store the GC-bias curve for a BAM

- *(pipelines)* join reference GC against alignment depth by window
Pure join/aggregation module: gc_tracks and mosdepth already tile every
contig on the same window grid, so the join is a (contig, window index)
key lookup with no resampling. Aggregation weights each …

- *(pipelines)* summarize ONT base modifications from BAM tags with modkit

- *(pipelines)* register modkit for ONT base-modification calling
Adds the modkit probe and TOOL_META entry (#631, stage 0). Installed
from bioconda's linux-aarch64 build rather than ONT's x86-64-only
GitHub release binaries -- verified against bioconda's repodata b…

- *(ops)* add interactive test-timing helper script
Lets a developer pick backend, frontend, ruff, or all via an arrow-key
TUI, runs the suite through the isolation this repo already has
(run-worktree-tests.sh / docker compose exec / the web container)…

- *(pipelines)* retain samtools stats and QUAST report.tsv for MultiQC
Closes the two gaps #624 deferred: MultiQC's samtools and QUAST modules
found nothing to summarize because neither raw output survived past its
job's workdir.

- *(pipelines)* add Liftoff annotation transfer for eukaryotic assemblies
Liftoff lifts an existing reference annotation (GFF3/GTF) onto a target
eukaryotic assembly by aligning the annotated features with minimap2 and
projecting them -- the eukaryotic counterpart to Bakta'…

- *(frontend)* band substitution spectrum by Ti/Tv category
bcftools emits the twelve substitution classes in its own order, with
transitions and transversions interleaved, so the two categories a
scientist actually checks -- the same categories the summary ro…

- *(pipelines)* phase called variants with whatsHap
Adds read-based phasing of a called-variant VCF against its alignments
(whatshap phase for one sample, polyphase across several), producing a new
phased VCF carrying PS tags. The phase set is surfaced…

- *(frontend)* show the project's MultiQC report in the panel header
Implements the seven states from the design. The report lives in the left
project panel rather than a file's QC tab, which is the placement question
this feature turned on: a MultiQC report summarizes…

- *(api)* report a project's MultiQC state in one read
The Project QC panel renders seven states, and the interesting ones are
combinations: a failed run with an older report still on disk says
something different from a failed run with nothing, which dif…

- *(pipelines)* offer an aggregate QC report from the Actions tab
Adds launch_multiqc_report, the POST /pipelines/multiqc route, a GET route
serving the generated report, and the Actions-tab card.

- *(queue)* add the project-scoped multiqc_report handler
The first job here scoped to a project rather than an object: it summarises
QC other jobs already produced across many objects and belongs to none of
them. So the payload carries project_id, the outpu…

- *(qc)* retain fastp's JSON so an aggregate report can parse it
MultiQC reads raw tool output, not the rendered HTML this application
keeps. _run_short_read_qc parsed fastp's JSON into facts and left the file
in the workdir, which is reaped when the job ends -- so…

- *(pipelines)* add the MultiQC command builder
Pure command construction, the same split quast_runner uses. Two flags are
load-bearing rather than cosmetic, both found by running the real binary:

- *(pipelines)* show transcript assembly runs in provenance and activity

- *(pipelines)* format HISAT2 output for transcript assembly by default

- *(pipelines)* offer transcript assembly on splice-aware alignments only

- *(pipelines)* launch transcript assembly and keep its output out of annotation pickers
Wires the launch path for StringTie transcript assembly: pipeline_service
.launch_transcript_assembly() (mirroring launch_quantify's BAM+annotation
shape and launch_salmon_quantify's budget/enqueue st…

- *(queue)* ingest StringTie output as an assembled-transcripts object
Adds _apply_transcript_assembly, registered in the results dispatch
table beside salmon_quantify, so a finished transcript_assembly job
becomes a first-class DataObject with role=ASSEMBLED_TRANSCRIPTS…

- *(queue)* add transcript_assembly job handler

- *(models)* add transcript-assembly run kind and assembled-transcripts role

- *(pipelines)* add StringTie command construction and GTF parsing

- *(pipelines)* install and register StringTie transcript assembler

- *(services)* offer Delly on the SV card for short-read BAMs

- *(pipelines)* dispatch SV calling to Delly for short-read BAMs
Ties the Delly pieces together at the two points that choose a caller: the
call_structural_variants handler and launch_structural_variant_calling.

- *(pipelines)* build Delly short-read SV commands

- *(pipelines)* install Delly 2.6.0 and document it

- *(pipelines)* map read chemistry to an SV caller in one place

- *(frontend)* render the coverage depth report as a per-contig track
Adds the Coverage depth panel to a BAM's Results tab, gated on its own
fact like the feature coverage panel beside it, and placed after it so the
two read together: per annotated feature, then per win…

- *(pipelines)* measure depth over an uploaded target BED
Adds region mode: when the launch carries a regions_id, mosdepth measures
that BED's intervals instead of the generated uniform windows. Same rows
out either way, so the report and facts now record wh…

- *(services)* offer a Coverage depth card on any completed alignment
Adds build_coverage_card and registers it in CARD_BUILDERS, and flips
mosdepth's TOOL_META to runnable now that something dispatches to it.

- *(services)* launch coverage jobs and apply their facts to the BAM
Wires the coverage handler to the rest of the app: launch_coverage in
pipeline_service, its node-type adapter and spec, the POST and report
routes, and the applier that merges the returned facts onto …

- *(queue)* add the coverage handler running mosdepth over windows
A SUBPROCESS handler on the feature_coverage_handlers.py model: read-only
like bam_stats, so no derived object -- one JSON report under coverage_dir
plus summary facts merged onto the BAM.

- *(pipelines)* add the mosdepth runner (command, windows, parsers)
Pure module on the feature_coverage_runner.py model -- no subprocess here,
so the command builder, window generator and parsers are all unit-testable
without the binary.

- *(pipelines)* install mosdepth and register the tool
First stage of #626: mosdepth is installed, probed, and documented on
/help/software, but nothing dispatches to it yet -- `runnable=False`
until the coverage card and its launch function ship, the sam…

- *(pipelines)* install MultiQC in an isolated venv and register the tool
MultiQC pins kaleido==0.2.1; this image carries kaleido 1.3.0 for NanoPlot.
Installing into the shared environment downgrades kaleido and breaks
long-read QC silently -- nothing imports kaleido at sta…

- add raw p-value histogram to DE results
A diagnostic the volcano and MA plots cannot provide: the shape of the
raw p-value distribution across every tested gene. Uniform across [0,1]
with a spike near 0 is healthy; a hill in the middle mean…

- *(ui)* add mirrored indel length distribution chart to variant results
The data was already computed by bcftools stats (IDD section), persisted,
and typed -- nothing rendered it. Adds a hand-rolled SVG mirrored bar chart
where deletions extend left of the zero line and i…

- *(pipelines)* identify organisms in reads with kraken2 and bracken (#685)
* docs(pipelines): design spec for Kraken2 classification / contamination screening

- *(pipelines)* offer salmon where a transcriptome is available
Adds the Actions-tab suggestion card for alignment-free RNA-seq
quantification: build_salmon_quantify_card guards on FASTQ, checks
tools.salmon() fresh at call time, and gates on a project having a
di…

- *(pipelines)* add a salmon quantification node
Wires launch_salmon_quantify (reads + transcriptome, paired-end via
mate_id, RunKind.QUANTIFY with run_tool="salmon" to disambiguate from
featureCounts) into the salmon_quantify node type, resolving a…

- *(queue)* register salmon output as a counts object

- *(queue)* quantify a sample with salmon in one job

- *(models)* add the salmon index sidecar role

- *(pipelines)* write salmon counts in the shape the parser reads

- *(pipelines)* build salmon index and quant commands
Adds command-builder functions for salmon index and salmon quant, plus path helpers
for quantification output and sample name derivation. These functions construct the
command-line arguments needed to…

- *(pipelines)* sum salmon transcript estimates into gene counts

- *(pipelines)* read salmon's quant.sf into per-transcript reads

- *(pipelines)* install salmon and probe for it

- *(pipelines)* add SnpEff as an alternative variant annotator
Adds SnpEff (pegi3s/snpeff:5.2a, ON_DEMAND_IMAGE) behind the existing
annotate_variants node type, alongside bcftools csq. The card now picks
whichever annotator is available, defaulting to bcftools c…

- *(pipelines)* annotate structural variants with GFF gene overlap (#671)
Option A implementation for structural variant annotation (#652).
Intersects SV intervals [POS, END] against reference GFF/GTF features to attach overlapping gene lists to symbolic SVs (e.g. <DEL>, <I…

- *(frontend)* add Medaka --bacteria opt-in to the polish_long launch dialog (#670)
Expose Medaka's bacterial-methylation model (--bacteria) from the
polish_long Actions-tab card via a launch dialog, matching iVar's primer
scheme and completeness's lineage-override pattern.

- *(services)* offer medaka polishing for long-read projects
Adds build_polish_long_card, the Medaka sibling of build_polish_card,
as a separate card rather than a smarter single one -- the two tools
take different reads and fail for different reasons, so mergi…

- *(pipelines)* register the medaka polish node type

- *(api)* launch medaka polishing from a draft and long reads

- *(queue)* polish long-read assemblies with medaka

- *(services)* identify long-read sets for polishing

- *(pipelines)* probe and document medaka

- *(pipelines)* count positions medaka changed in the draft

- *(pipelines)* record which model medaka resolved

- *(pipelines)* build medaka_consensus commands

- *(frontend)* per-feature coverage table on BAM results
Adds the Results-tab section for the feature_coverage pipeline
(bedtools coverage of a BAM against a project annotation): a summary
line plus an unpaginated table of every feature, sorted worst-breadt…

- *(pipelines)* feature coverage card, launch endpoint, and report API
Adds the Actions-tab card, POST /pipelines/feature-coverage launch route,
and GET /pipelines/feature-coverage/{object_id}/report, then flips
bedtools' TOOL_META runnable flag now that it has a real co…

- *(services)* launch feature coverage as a classified pipeline node
Adds launch_feature_coverage, requiring the BAM's .bai and the reference's
.fai to already exist (matching launch_variant_calling's precedent) rather
than chaining index builds. Classifies it as a rea…

- *(queue)* feature_coverage handler running bedtools coverage

- *(pipelines)* bedtools coverage command builder and parser

- *(ui)* show sample correlation as a heatmap grouped by condition
Hand-rolled SVG matching the other expression charts. Samples are reordered by
condition rather than left in matrix order, so replicate blocks land on the
diagonal -- without that the blocks are still…

- *(pipelines)* report sample-to-sample correlation alongside the DE projection
The projection catches a swapped or mislabelled sample; it cannot say how
strongly replicates actually agree. Two samples can sit adjacent on PC1/PC2
while correlating poorly, since the first two comp…

- *(ops)* install seqkit in the backend image, both arches

- *(pipelines)* probe bedtools and seqkit as first-class tools

- *(frontend)* chart long-read length and quality distributions on the QC tab
Two new hand-rolled SVG charts read the facts the backend now persists: a
base-weighted read-length histogram (bases per bin, not reads, with N50
marked on the log axis) and a length-vs-quality densit…

- *(pipelines)* bin NanoPlot's per-read TSV into long-read length and quality distributions
--raw now writes per-read length/quality pairs alongside the existing
NanoStats summary; a new nanoplot_raw module bins them into a base-weighted
length histogram and a length-vs-quality density grid,…

- *(pipelines)* convert hifiasm GFA contigs to FASTA

- *(frontend)* shade the alignment's chromosomes by depth
Adds a "Depth by chromosome" strip to a BAM's Results tab: each contig drawn
to scale as in the reference's chromosome map, shaded by its depth against a
typical contig. The failure modes it exists fo…

- *(frontend)* read contig depth against the median, not the genome mean
The baseline a per-contig depth chart normalizes against decides whether it
reports biology or noise, and the obvious choice -- `bam_stats_summary`'s
`mean_depth` -- is the wrong one. That figure is t…

- *(ui)* overlay the before/after quality curves on the trim report
Shows where trimming acted along the read, not only how much it removed. The
two curves come from one fastp pass over the same reads, so they are directly
comparable; where they separate is the answer…

- *(pipelines)* keep fastp's before/after quality curves on the trim report
The scalar deltas say how much trimming removed; they cannot say where it
acted. The per-cycle curves can -- a clipped 3' decay, an adapter tail, a
uniform lift, or (the case worth catching) two curve…

- *(ui)* show k-mer spectrum estimates on the assembly panel
kmer_spectra had no consumer anywhere: the K-mer spectrum card launched
a job whose headline outputs (estimated genome size, heterozygosity)
had no display even once the facts reached the object. Rend…

- *(pipelines)* pass curated minimap2 flags

- *(ui)* expose curated minimap2 fields in the align dialog

- *(pipelines)* validate curated minimap2 parameters

- *(e2e)* add launch, find, patch, and get_object steps to the harness
The reads path was blocked on the harness having no way to start a pipeline
over HTTP. The QC/trim/align flow is deliberately not reachable through the
`run_pipeline` MCP tool: that passes `params` st…

- *(frontend)* expose Bowtie2 workflow parameters

- *(aligners)* emit Bowtie2 pair geometry flags

- *(aligners)* add Bowtie2 workflow presets

- *(aligners)* validate Bowtie2 paired-end parameters

- *(export)* pack included report artifacts

- *(export)* add typed report artifact manifest rows

- *(export)* discover object report artifacts

- predict protein structure via ESMFold sidecar when none is deposited (#576)
* feat: add sequence reader, ProteinPrediction model, and PREDICTION_SIDECAR_URL config

- *(align)* tag aligners as recommended or incompatible by read chemistry
None of the five aligners had a `recommendations` entry, so the align
picker never showed a "Recommended" badge (bwa-mem2 for short reads,
minimap2 for long reads) and had no way to flag a chemistry-i…

- *(ui)* explain metadata history metrics and standalone charts (#579)

- *(frontend)* find the files a launch's selections were derived from
`supersededBySelection` answers "did anything already chosen come from
this file", which is the link a picker needs to avoid offering the raw
version of a read set the user has already added.

- *(pipelines)* declare per aligner whether its index builder reads gzip
hisat2-build and STAR's genomeGenerate both require a plain-text FASTA;
bowtie2-build, bwa-mem2 index and minimap2 -d accept gzip and bgzip alike.
Measured against the binaries this image ships rather…

- *(ui)* put info markers on the Results tab's reported numbers
Wires InfoMarker into the four Results renderers, the same way #557 did
for the Quality tab: BamResults and its coverage, contig-depth, depth
histogram and per-contig table; VariantResults and Variant…

- *(ui)* explain the Results tab's numbers in the metric registry
Adds the 34 entries the Results renderers need: the BAM headline row and
its coverage/depth charts, the variant call-set summary, the differential
expression summary and plots, and the annotation bloc…

- *(ui)* badge offloaded files and offer to free their space
Adds the frontend half: an NCBI badge on an offloaded row, a Storage
control in the manage panel, and a download-size warning in the align
dialog when an input would have to be fetched first.

- *(pipelines)* fetch offloaded alignment inputs before the job runs
Wires ensure_local into the alignment launch, the workflow that motivates
the feature: trim reads, offload the raw FASTQ, then align against it later.
Each of R1, its mate and the reference is checked…

- *(queue)* fetch an offloaded file's bytes back into the object that had them
Completes the round trip: fetch_remote handler, _apply_fetch_remote applier,
object_service.restore_bytes_for_object, and pipeline_service.ensure_local
to queue the fetch a launch needs.

- *(objects)* release a re-fetchable file's bytes, keeping it in the project
Adds the offload path: blob_service.release_bytes_for_object,
object_service.offload_object, and POST /objects/{id}/offload.

- *(objects)* refuse byte access to an offloaded file, naming the fetch
Every path that reaches for an offloaded object's bytes now refuses with a
message about fetching it back, rather than falling through to a message
written for an upload still in flight.

- *(models)* record whether an object's bytes are here, and where to refetch them
Adds `Locality` (LOCAL/REMOTE) and `RemoteSource` to DataObject, the model
half of offloading a re-fetchable NCBI download while keeping the file in
the project tree.

- *(ui)* offer "Launch anyway" on an Actions card refused for memory

- *(pipelines)* probe SPAdes and make it a selectable assembler

- *(pipelines)* install SPAdes 4.3.0, prebuilt on amd64 and built from source on arm64

- *(pipelines)* build SPAdes commands with a floored memory ceiling

- *(pipelines)* declare SPAdes outputs and running modes in the registry

- *(pipelines)* add SPAdes assembly parameters with three running modes

- *(ui)* show which parameter set configured a run

- *(api)* wire from_parameter_set through align/assemble launch routes
Task 8 (frontend, merged) sends from_parameter_set on launch requests when a
saved parameter set configured the run, but AlignRequest/AssembleRequest and
pipeline_service.launch_alignment/launch_assem…

- *(ui)* offer saved parameter sets in the align and assemble dialogs
Mounts ParameterSetPicker above the biology fields in AlignDialog and
AssembleDialog, and threads from_parameter_set provenance (set id, name,
revision, and whether the form was edited after applying)…

- *(ui)* add a parameter-set picker with a persistent drift notice

- *(frontend)* add parameter-set types and client methods

- *(provenance)* record which parameter set configured a run

- *(api)* add parameter-set CRUD and an apply-time resolve route

- *(pipelines)* resolve saved params against a tool's current schema

- *(pipelines)* derive parameter-set eligible keys from tool specs

- *(models)* add a ParameterSet document for saved pipeline knobs

- *(ui)* give the database index a Remote Databases header
Everything below "Local Databases" -- the 475-entry catalog, its search,
filters and cards -- had no heading of its own, so it read as a
continuation of the local list rather than a separate section.

- *(ui)* explain the Quality tab's QC rows and charts
Wires the info marker into the rest of the Quality tab: both QcReport
blocks (short-read fastp/FastQC and long-read NanoPlot) and the eight
charts in the grid, plus the chromosome strip beside them.

- *(ui)* explain every reported number behind an info marker
Every figure BioFlow reports is one someone might copy into a methods
section, and several are not what their label implies. "Mean quality" is
the mean of the per-position means rather than the mean o…

- *(metadata)* migrate stored platform values to instrument_model
Carries the existing rows across the split in the previous commit. Every
object written before it holds an instrument model in `metadata.platform`;
this moves that value to `instrument_model` and re-d…

- *(metadata)* split instrument model out of the platform field
`metadata.platform` held two vocabularies at once. The schema declared it
an ENUM over family names ("Illumina NovaSeq", "PacBio"), while SRA
enrichment wrote the INSTRUMENT_MODEL into it -- so all 55…

- *(ops)* decide compose invocations by tokenizing, not phrase matching
`compose_target.py` answers one question -- does this Bash command invoke
compose against the host's stack -- by tokenizing the command line with
shlex and inspecting the command words, rather than ma…

- *(ui)* offer "Adjust…" beside Launch, opening the dialog on that card's run
The one-click cards turned out to be used for more than quick launches, so
a card now offers both routes: Launch runs it with the settings already
decided, and Adjust opens that operation's settings d…

- *(api)* say which settings dialog can adjust a suggestion card's run
Each card already carries the complete launch body for its endpoint, which
is also everything a settings dialog would ask for -- the reference, the
tool, the params. What it did not say is whether a d…

- *(queue)* add an exact declared-vs-budget refusal predicate

- *(models)* add SequencingPlatform enum and exhaustiveness tests (#543)
Adds SequencingPlatform StrEnum over SRA PLATFORM tags in
app/models/object.py, alongside SequenceType. Exhaustiveness-tests
LONG_READ_PLATFORMS, SHORT_READ_PLATFORMS, _SAM_PLATFORM_PATTERNS,
and _PLA…

- *(pipelines)* add progress parsers to samtools sort/merge and polish alignment calls (#542)
* feat(pipelines): add progress parsers to samtools sort/merge and polish alignment calls

- *(ui)* add a project export action with a size-preview dialog
Adds a project-level Export action alongside ProjectDangerZone: a dialog
lets the user adjust the blob-inclusion threshold (default 100 MB,
matching the backend), queue an export via POST
/projects/{i…

- *(export)* add endpoints to create, list, and download project exports
Adds POST /api/v1/projects/{id}/export (enqueues launch_project_export),
GET /api/v1/exports (lists archives in exports_dir), and
GET /api/v1/exports/{name}/download (streams one, rejecting a traversa…

- *(export)* run the export as a queued job
THREAD mode, not SUBPROCESS: it packs a tarball in-process. Registered in
EXCLUDED_LAUNCHES since an export produces an archive on disk rather than
an object a downstream node could consume. Also clas…

- *(export)* pack the project archive with a versioned envelope
Adds export_project(), the top-level function that runs collect() ->
redact() -> build_manifest() -> render_report() and packs the result into
a single .tar.gz under settings.exports_dir. Greps the pr…

- *(export)* render the analysis report through provenance_report
Reuses render_markdown rather than adding a second renderer, so its gap
markers carry into the archive -- a reader scanning for a tool version
sees the question asked and unanswered rather than assumi…

- *(export)* list every blob in the manifest, flagging excluded bytes
Manifest rows cover every blob in scope, including ones whose bytes are
left out of the archive -- the recipient can tell "not sent" from "does
not exist". content_sha256 falls back to blob.id when un…

- *(export)* redact secrets, filesystem paths, and machine identity
Names the collections it serializes rather than dumping and subtracting.
This inverts ops/backup.sh deliberately: backup fails safe by including
every collection, export fails safe by excluding all bu…

- *(export)* collect a project subtree and everything it references

- *(export)* add exports directory and archive format constants

- *(ui)* add a structure tab that browses a protein FASTA's proteins in 3D

- *(api)* report a protein record's structure and why it has none

- *(api)* resolve a protein accession to its PDB structures via UniProt

- *(api)* serve a paged, searchable protein record list for a protein FASTA
Adds GET /objects/{id}/protein-records?offset=&limit=&q= returning total,
truncated, and rows of {ordinal, identifier, description, length,
has_reference}. Search matches identifier or description, ca…

- *(api)* index a protein FASTA's records at ingest so its proteins can be listed
Scans every record in a protein FASTA at ingest time and writes an
identifier, description, length, byte offset, and parsed UniProt/RefSeq
accession per record to a new protein_records collection. Gat…

- *(models)* add a protein record collection for FASTA record listings

- *(metadata)* read UniProt and RefSeq accessions out of protein FASTA headers

- *(ui)* offer short-read assembly instead of refusing it
The Actions-tab assemble card no longer refuses short reads with "Short-read
assembly is not installed" now that ABySS is wired end to end. The card
offers the run and its `why` text says whether the …

- *(api)* assemble short reads paired, resolving the mate at launch
launch_assembly now identifies a mate for paired-layout assemblers (ABySS)
by delegating to the existing suggest_mate discovery, adds one refusal on
top -- an explicitly-supplied candidate whose read …

- *(queue)* run abyss with a resolved mate and a bloom filter budget

- *(pipelines)* parse abyss progress and its own stats table

- *(pipelines)* build abyss command lines as make variable assignments

- *(pipelines)* route short reads to abyss in the assembler registry

- *(pipelines)* let an assembly memory model charge for read volume

- *(pipelines)* add abyss assembler params with a k-mer field

- *(pipelines)* install abyss and declare it as a tool
Adds the abyss apt package to the backend image and a probe/TOOL_META
entry so later short-read assembly tasks can build on it. abyss-pe is
a GNU Make wrapper rather than a conventional CLI, so the pr…

- *(ui)* add reload button to launcher status line (#520)
Adds a subtle ↻ button in the launcher's status line that triggers
window.location.reload(). Primarily for development use when making
changes to the launcher's frontend code.

- *(ui)* put annotation feature type and biotype charts side by side (#509)
Wraps the 'Features by type' and 'Features by biotype' sections in the
annotation results panel with the existing FactsColumns component, so they
render in two columns instead of stacked vertically.

- *(frontend)* add a Storage page reporting database and disk drift
Renders the drift sweep result from GET /api/v1/maintenance/drift (task
8) at /settings/storage: per-category counts, reclaimable bytes, and
last-checked time, following the same settings-page/Setting…

- *(api)* serve the latest storage drift report

- *(api)* schedule the storage drift sweep every six hours

- *(api)* compose the drift detectors into one stored sweep

- *(api)* detect report directories missing from claiming objects

- *(api)* map each report predicate fact to its report root
Adds REPORT_ROOTS, its companion REPORTS_WITHOUT_DIRS, the exhaustive
ALL_REPORT_STATUS_FACTS, and object_claims_report() to drift_service.py,
plus exhaustiveness tests in test_drift_registry.py that …

- *(api)* report blobs verify_files has confirmed missing

- *(api)* detect orphaned files and stalled ingests under objects/
Adds find_orphaned_files(), the first storage-drift detector: walks
objects/ and reports files with no Blob record (ORPHANED_FILE) or
whose record is still PENDING past GC_GRACE (STALLED_INGEST). A
PE…

- *(api)* add the stored drift report document

- *(api)* tell each suggestion card whether its work is already running
Adds a `running` field to every card, true when a queued or running job of
the type that card's endpoint produces is attributed to the file.

- *(ops)* restore a backup and verify /data against its manifest
Restore gates on the recorded version, confirms interactively before
overwriting, and asserts per-collection document counts afterwards so a
partial restore fails loudly rather than looking like a suc…

- *(ops)* write the restore contract into every backup
The machine that needs the contract may not have a checkout, so it ships
inside the backup directory rather than only in docs. States plainly what
restore recovers, what it does not (blobs, provider k…

- *(ops)* take a backup into a timestamped directory
mongodump plus the four companion files: the /data manifest, the
provider summary, manifest.json with per-collection counts, and the
restore contract. The counts are load-bearing -- restore asserts ag…

- *(ops)* summarize configured providers without their keys
Restore has to tell the user why their providers are broken and which to
fix. It does that from a name, model, slot and last-four digest -- never
the key and never the Fernet secret, so the backup sti…

- *(ops)* enumerate /data blobs into a standalone manifest
The blobs collection already records id, size, rel_path and
content_sha256, so the manifest is a projection rather than new
bookkeeping. It is written as its own TSV -- not left inside the dump --
so …

- *(ops)* stamp backups with a filesystem-safe UTC timestamp

- *(ops)* add the backup script skeleton and its make targets
Dispatch, usage, and the three environment knobs. The Mongo target is an
environment variable from the start because the round-trip test must be
able to redirect it away from the live stack.

- *(api)* report the served git revision on the version endpoint
VERSION alone cannot answer "is this stale?" -- it is unchanged by the branch
the checkout is parked on, which is the thing that actually decides what code
runs. The fields are null in a shipped image…

- *(api)* log which checkout the api and worker are serving at startup
One line in the logs turns a half-hour hunt into a glance. Warns rather than
informs when HEAD is not origin/main: a stale checkout is not a fact about
the environment, it is a bug waiting to be misat…

- *(api)* read the served checkout's git revision without a git binary
The api and worker containers run bind-mounted source, so what they execute
is whatever the checkout has on disk -- not what VERSION claims, and not
necessarily main. Nothing surfaced that, so a check…

- *(frontend)* add a Set Default button to each AI provider
Lets a user make a provider the global default directly from its
detail pane, instead of only through the Task routing table.

- *(frontend)* add formatRelative for lists that span more than a day
formatClock renders a bare clock, which is right for rows that all belong
to the current session but loses the day for anything older. A list of
finished runs spanning a week gave every line a time an…

- *(api)* fail provisioning when the node's worker does not stay up
`docker compose up -d` exits 0 as soon as the container is created, which
is before it can crash. A node whose worker died on startup -- bad image,
unwritable storage path, unreadable .env -- provisio…

- *(frontend)* validate API responses behind irreversible UI actions (#422)

- *(frontend)* show remaining-time estimates on running jobs in the queue view (#430)
Surfaces timing estimates on running-job rows in QueuePanel by fetching job
details and using the same label logic as IngestProgress. Extracts the shared
timing label computation into lib/jobTiming.ts…

- *(ops)* auto-restart the worker on source changes in the dev override (#428)
Adds watchfiles to dev dependencies and adds a watchfiles command to the
worker service in docker-compose.override.yml so the worker auto-restarts
when backend source files change. This eliminates the…

- *(pipelines)* canvas node type for annotation subset export (#392)
* docs(pipelines): spec the annotation export canvas node type

- *(frontend)* offer sequence extraction on a GenBank's Results tab

- *(frontend)* add the GenBank sequence extraction API calls

- *(api)* add endpoints to extract and look up a GenBank's sequence

- *(pipelines)* guard GenBank sequence extraction against a second run

- *(pipelines)* ingest an extracted GenBank sequence as a reference

- *(pipelines)* add the extract_genbank_sequence handler

- *(pipelines)* stream a GenBank ORIGIN block into wrapped FASTA

- *(pipelines)* parse bases out of a GenBank ORIGIN line

- *(ops)* add local commit-msg hook mirroring CI's subject check

- *(annotation)* edit and materialize annotation records

- *(e2e)* add fixtures and starter tests

- *(e2e)* add desktop plugin page

- *(e2e)* add harness backend (clients, store, loader, runner, API)

- *(e2e)* add install script

- *(e2e)* add harness config loader

- *(e2e)* scaffold harness directory and Python environment

- *(icons)* add reads-type tooltips showing Illumina/PacBio/Nanopore/Unspecified
The reads-type icons (reads_raw_illumina, reads_raw_pacbio,
reads_raw_nanopore, and their trimmed variants) in the Project Explorer
and other object surfaces had no hover tooltip — the SVG <title> chi…

- *(ui)* export the annotation table's current filter
Shows matched and exported counts together: the closure adds parents and
children, so the exported number is routinely larger, and showing only one
of the two reads as a bug.

- *(api)* add annotation subset export routes
The count route is separate from the export so the UI can show matched
against exported before anything is queued: the closure is routinely larger
than the matched count, and an unexplained difference…

- *(queue)* register an exported annotation subset as a derived object
derived_from the source annotation, so the subset's lineage is inspectable.
Carries no annotation results facts: the subset opens to a Compute results
button like any other annotation, leaving whether…

- *(queue)* add the annotation subset export handler
Receives the filter as it was applied in the table and passes it straight to
closure_lines -- it never re-derives the filter, which is what keeps the
exported subset and the displayed table from drift…

- *(pipelines)* write a verified annotation subset
Copies original source lines rather than serializing Feature, which keeps
neither the GFF source column nor phase -- a reconstructed CDS line would
silently lose its reading frame.

- *(pipelines)* compute the closure of an annotation filter
A filter matches rows, but a valid annotation needs whole trees: a gene
without its exons leaves Parent= references dangling, and exons without
their gene leave orphans. The closure is every match plu…

- *(pipelines)* store each feature's source line in the annotation index
No index on the column: subset export selects by filter and reads the line
out, it never looks a feature up by line. Rows sharing one source line
under multiple parents all record that line.

- *(pipelines)* record the source line number on every parsed feature
Set by the handler's read loop rather than the parse functions, which take
one string and cannot know where it came from. None for GenBank, whose
features span multiple lines and whose segment childre…

- *(frontend)* pair RefSeq/GenBank twins with a namespace toggle
A reference downloaded from NCBI arrives as either a GCA (GenBank) or GCF
(RefSeq) accession, and a project that needs both currently shows them as
two separate rows. They are one assembly in two name…

- *(launcher)* stage update notification for alpha/beta users
Adds a new stage-update affordance that detects when a newer alpha or beta
tag is available and offers a one-click update with confirmation dialog.

- *(launcher)* add update_to_stage Tauri command

- *(launcher)* add update_to_stage action and tests for check_stage_update

- *(launcher)* add check_stage_update and set_bioflow_tag helpers

- *(ui)* say why annotation coverage is missing instead of drawing blank bars

- *(pipelines)* backfill annotation analysis when a reference lands

- *(pipelines)* analyze an annotation when it finishes ingesting

- *(pipelines)* record whether an annotation analysis resolved a reference
Adds contig_lengths_known to the launch_annotation_stats payload and
annotation_contig_lengths_known to run_annotation_stats's facts, so the
ingest backfill (a later task) can find annotations analyze…

- *(pipelines)* add the auto-analysis eligibility rule for annotations

- *(storage)* add resolve_report_file containment helper
Both stats report endpoints hand-roll the same three-step guard with two
different spellings of the containment check. Extract it so a third result
type inherits the guard rather than re-deriving it -…

- *(ui)* show the annotation track above the feature table

- *(ui)* draw annotation features along a coordinate axis

- *(frontend)* add the annotation window client and its types

- *(api)* serve annotation features or their density for a window

- *(pipelines)* resolve an annotation's reference by provenance or accession

- *(pipelines)* assemble gene models for a coordinate window

- *(pipelines)* count annotation features per coordinate bin

- *(pipelines)* pack overlapping annotation features into rows

- *(annotation)* report unresolved parentage on the results summary

- *(annotation)* add a genes/all/unresolved view toggle to the feature table
Adds the frontend half of the parent-resolution and gene-summary work
from tasks 1-6: a three-tab toggle (Genes/All records/Unresolved) above
the annotation feature table, backed by the new /genes rou…

- *(annotation)* serve the genes view and reach unresolved records over the API
The features route gains a `view=unresolved` parameter that clears
top_level_only and filters to UNRESOLVED_STATUSES, making a dangling/
ambiguous/self/cyclic record reachable for the first time. A ne…

- *(annotation)* resolve hierarchy and build the gene table during the compute job
run_annotation_stats now classifies every feature's parent reference and
builds the genes table against the temporary database, before the atomic
rename into place -- a database is only published once…

- *(annotation)* build a per-gene summary table with de-duplicated descendant counts

- *(annotation)* store one row per parent relationship and filter by resolution status
Fixes build_annotation_db, which crashed after Feature.parent became
Feature.parents (a tuple) in the previous commit: it now writes one
row per declared parent relationship so a shared exon is reacha…

- *(annotation)* classify every parent reference as resolved, dangling, ambiguous, self, or cyclic

- *(ui)* label, icon, and results tab for GenBank files
Adds the "GenBank" format label and maps it to the existing GFF
annotation icon. DetailPanel also gated the Results tab and its
AnnotationResults branch on format kind (gff/gtf/bed) -- without
adding …

- *(annotation)* make GenBank files eligible for annotation results

- *(annotation)* build feature tables and stats from GenBank files

- *(formats)* detect GenBank by extension and LOCUS header

- *(pipelines)* stream GenBank records without loading sequence

- *(pipelines)* emit GenBank features as parent rows plus segment children

- *(pipelines)* parse GenBank qualifier blocks with wrapped values

- *(pipelines)* parse GenBank locations without flattening joins

- *(formats)* add GENBANK format kind
Adds FormatKind.GENBANK and places it in FORMAT_FIELDS alongside the
other interval/annotation formats (GFF, GTF, BED). First of an 11-task
plan for GenBank annotation support (#294); every subsequent…

- *(ui)* say what SSH key BioFlow installs and retains when provisioning

- *(ui)* show each node's version and offer an update when it is behind

- *(api)* endpoints to update a compute node and poll its progress

- *(services)* update a node's image over SSH, verified by re-enrollment

- *(api)* install a managed SSH key during node provisioning
Adds an install_key phase between write_env and pull_image in
_provision_node: generates a keypair, appends it to the node's
authorized_keys over the existing connection, verifies it authenticates
ind…

- *(services)* generate and install a managed SSH key on compute nodes

- *(api)* expose the primary's image digest as the staleness reference

- *(api)* persist and expose the backend version each node reports
Enrollment now writes image_digest/version onto the Node document
instead of only Redis, which expires when a worker stops
heartbeating, and enumerate_nodes/list_nodes surface those fields
(plus an up…

- *(queue)* worker reports its image digest and version in the heartbeat

- *(models)* record SSH connection details and reported version on Node

- *(models)* add NodeUpdateTask for tracking node image updates
Mirrors NodeProvisionTask's structure but avoids the bare-string
Settings.indexes bug found in that model: task_id and node_id are both
declared via field-level Indexed(...) annotations only, with no
…

- add project-level operations with collapsible actions accordion
Add a 'Project actions' accordion above the filter box in the left panel
that lists project-scoped operations. Clicking an action opens its form
in the right panel via the existing ?sel= URL param sys…

- *(frontend)* add optional additional reads to the align dialog
The dialog keeps the one-file default -- the primary reads row and the
paired-end auto-mate are untouched -- and adds an "Add another read
file" picker beneath them. Each added file resolves its own m…

- *(pipelines)* record additional read sets in alignment provenance
An alignment run's inputs now list every additional set member under its
own role -- extra_reads for the set's own file, extra_mate for its mate
-- instead of the set being invisible to the activity v…

- *(pipelines)* concatenate additional read set mates into the R2 stream
A paired alignment with additional read sets previously threaded every
set's R1 into the combined R1 stream and dropped the mates entirely.
Each set's mate now concatenates into the R2 stream alongsid…

- *(pipelines)* resolve and validate paired additional read sets at launch
Each additional set is now a typed pair (R1 plus optional mate) resolved
the same way as the primary: explicit link, else suggest_mate. The set's
mate is auto-included, so adding one file of a pair is…

- *(api)* accept ordered additional alignment read sets
A file-level alignment launch can now carry additional read sets as a typed
top-level request field, a sibling of mate_object_id: each set is an R1 plus
an optional mate, and the whole run shares one …

- *(ci)* publish images and launcher bundles in one release
A v* tag now builds the launcher alongside the images and attaches the
bundles to the single GitHub release it creates (#335). A launcher failure
leaves the release published with images and no bundle…

- *(ops)* constrain launcher-only releases to exceed the app version
Keeps the launcher-never-below-app invariant that lets a combined cut
overwrite the launcher version safely (#335), and rejects pre-release
launcher-only cuts, which have no images to be staged agains…

- *(ops)* bump the launcher version as part of an app release
One release, one version (#335). A combined cut overwrites whatever the
launcher declared, so a launcher-only release's drift never survives the
next app release.

- *(docs)* update GitHub Pages landing page to v2
Replaces docs/index.html with a redesigned version of the landing
page (richer layout: numbered section nav, stats sidebar), still a
self-contained static bundle with no build step.

- *(ui)* give annotation files a results view

- *(ui)* browse annotation features with filters and a locus jump

- *(ui)* chart annotation feature types, density, and coverage

- *(frontend)* add the annotation results client and its types

- *(api)* serve the annotation feature table, filtered and paged
Adds launch_annotation_stats + _check_annotation_stats_callable to
pipeline_service.py, and three routes (POST /annotationstats, GET
/annotationstats/features/{id}, GET /annotationstats/children/{id})…

- *(pipelines)* compute annotation summaries and the feature index

- *(ops)* keep annotation results artifacts in a reaped directory

- *(pipelines)* store annotation features in a queryable SQLite index

- *(pipelines)* accumulate annotation aggregates in one bounded pass

- *(pipelines)* normalize GFF3, GTF, and BED lines to one feature shape

- *(docs)* add GitHub Pages landing page
Adds the project landing page as docs/index.html, a self-contained
static bundle (fonts and assets inlined) with no build step. Once
GitHub Pages is enabled to serve from main /docs, this becomes the
…





### 🐛 Bug Fixes

- *(ui)* reach the Record form's field help by keyboard and touch
The metadata form rendered its help affordance as a bare <span title=…>,
which opens on mouse hover and nothing else. The help text the form now
carries on all 61 fields was therefore unreachable thre…

- *(ui)* explain the AI summary and SRA rows, not leave them in "Other"
Seven facts the backend writes -- four ai_summary_* and three sra_* --
were in none of LABELS, METRIC_INFO or NO_INFO_NEEDED. Unlabelled facts
are not dropped from the table, they render with a title-…

- *(api)* refuse to provision a node with an unreachable primary address
Provisioning discovered the primary's address with a UDP-connect trick run
inside the API container, which returns the container's own Docker-network
address (172.19.0.6). That went into the node's .e…

- *(api)* word the non-empty-project conflict for a person
The 409 raised when deleting a project with contents told the caller to
"pass cascade=true". That string reaches a user: the UI surfaces the
error body in a toast, so a failed delete advised setting a…

- *(ui)* delete a non-empty project instead of failing with a 409
The project list's row action called deleteProject without cascade, so
deleting any project that had contents failed. The server's 409 body was
piped straight into a toast, which put API wording in fr…

- *(models)* explain every Record field, not half of them
The Record form's ⓘ marker renders only where a FieldDef sets `help`, so a
field without one looks finished -- labelled input, no marker, nothing
broken -- while being the one field on the form nobody…

- *(ui)* explain every reference quality row, not four of them
A reference's Quality tab renders AssemblyFacts rather than FactsTable, so
the LABELS partition in metricInfo.test.ts never saw a single one of its
rows. It shipped with info markers on four of thirty…

- *(ui)* drop a reference's download link rather than blanking the tab
A payload with no `index_ids` made the index panel read a key off undefined,
which threw and took the whole metadata tab down with it. A missing download
link is not worth a blank page.

- *(api)* return a reference's index_ids beside indexes, not inside it
Opening the metadata tab for a reference with any built index rendered a
blank page, on "undefined is not an object (evaluating
'entry.index_ids[name]')".

- *(services)* offer assembly when any installed assembler fits the reads
The assemble card asked whether the *chemistry's default* assembler was
installed. On an image without ABySS that made a short-read FASTQ's card
read "abyss is not installed" with no Launch and no Adj…

- *(pipelines)* validate the assembler that runs, not the chemistry's
`launch_assembly` checked availability and read layout against
`spec_for_chemistry(chemistry)` while enqueuing `parsed.assembler`.
Nothing could reach that divergence -- `default_assembly_params` alwa…

- *(storage)* clean up coverage, gc_bias and the other newer report dirs
Per-object report directories under coverage/, gc_bias/, feature_coverage/,
variants_in_regions/, annotation_comparison/ and methylation/ survived their
object being deleted, so they leaked permanentl…

- *(services)* name the assemble card's assembler from its spec
The card's title read `spec.assembler.value` while the sentence beside
it said "assembled with ABySS" in prose. Three assemblers now declare
layout="paired", so the hardcoded name is a sentence that c…

- *(services)* re-estimate a community assembly with its own memory model
_assembly_estimate never passed `meta=`, so every replan of a metagenome
assembly re-estimated against the assembler's single-genome model. For a
community that model has no genome size to consume, so…

- *(pipelines)* estimate memory for an always-meta assembler
estimate_assembly_mb selected its read-volume branch on
`meta and spec.meta_memory_model is not None`, which assumes every
community assembly is a *mode* some assembler switches into. An
assembler tha…

- *(pipelines)* declare CheckM2's measured memory, not a guessed 16 GB
Q1 forbids fitting mem_mb from the memory model, and the first value was
neither fitted nor measured -- it was a guess picked from the tier other
16 GB jobs use.

- *(pipelines)* pin CheckM2 to the database version it actually requires
The first pin named Zenodo record 7563512, found by searching for "CheckM2
database" and taking the result whose title said so. It is the wrong
database, and would have failed at the worst possible mo…

- *(pipelines)* name BED features from the annotation, not the variant
`bedtools intersect -wao` writes the A record, then B, then the overlap, so
the B record has to be addressed from the end of the row. The BED branch
sliced `cols[:-1]` instead -- the whole row minus t…

- *(frontend)* suppress gc_bias and gc_blob facts in generic FactsTable
Read-only facts emitted for GC coverage visualizations (gc_bias_*,
gc_blob_*) were leaking into FactsTable's generic renderer as a wall of
raw chips, duplicating what GcBiasChart and ContigBlobChart r…

- *(ops)* stop leaking Mongo/sshd volumes from worktree test runs

- *(pipelines)* initialize beanie in feature coverage test fixture
The client fixture in test_feature_coverage_reports.py constructs an
unsaved Job(...) without requesting beanie_models, so the test only
passes when another file already initialized Beanie earlier in …

- *(pipelines)* install Liftoff with --no-deps and a real parasail wheel
Pinning liftoff to a version that exists on PyPI (99e15314) traded one
build failure for another: every PyPI release of Liftoff pins its
dependencies with exact `==` versions from 2020 (numpy 1.21.0, …

- *(pipelines)* pin liftoff to a version that actually exists on PyPI
liftoff==1.7.2 was never published -- PyPI's latest release is
1.6.3.2 -- so every worker build failed on this step. Found while
rebuilding the worktree stack to verify #631; the wheel resolution
past…

- *(pipelines)* remove Filtlong's dead release-tarball install step
The from-source build (added in a8ba1076) already installs Filtlong;
this leftover block from before that fix still tried to download a
GitHub release asset that no longer exists (rrwick/Filtlong's re…

- *(frontend)* correct the blobplot's depth axis, cap caption, and partial-data warning
Fixes six findings from the Stage 2 (#641) whole-branch review:
- minDepth was pinned to a hardcoded 0.1 for every real dataset, mislabelling
  the y-axis and crushing the log-scale plot into a small …

- *(frontend)* explain an all-unscoreable blobplot instead of rendering empty
An assembly where every kept contig is all-N would otherwise render a
blank scatter with a "showing N contigs" caption implying a clean result
with zero visible points -- indistinguishable from a genu…

- *(pipelines)* surface partial GC tracks and empty curves in gc_bias
Final integration review on issue #640 Stage 1 found two load-bearing
gaps and three minor issues once all seven tasks' seams were viewed
together:

- *(queue)* hold the sv_stats_dir patch for the whole test module

- *(ops)* satisfy shellcheck on the durations table prefix
CI's shellcheck run (no --config exemption) flags the sed 's/^/  /'
idiom as SC2001; the local pre-commit hook doesn't catch it because it
only warns. Replace it with a plain read loop.

- *(pipelines)* classify phase_variants handler and wire budget refusal
Two exhaustiveness-registry checks the full suite runs caught that the
targeted run missed:
- the provenance walker requires every registered handler to carry a step
  verb, so phase_variants is now "…

- *(ops)* stop the docs-only skip step failing on a YAML comment
The skip step added for #732 is double-quoted and contains '#732', so
YAML treats everything from the '#' as a comment and the run: value
parses to 'echo "Docs-only PR (see' -- an unterminated quote t…

- *(pipelines)* default qc_summarizable so the context dataclass builds
A dataclass cannot place a non-default field after a defaulted one, and
sibling_snf_ids -- which landed on main while this branch was in flight --
is defaulted. Caught by the full suite after the reba…

- *(ops)* make required build-check jobs report on docs-only PRs
paths-ignore made the four required jobs absent (not success or
skipped) on a docs-only PR, so the branch ruleset waited on checks
that would never arrive and the PR sat BLOCKED forever. Each job now
…

- *(services)* explain how _variant_dedup_key includes the caller
The docstring said the key includes the caller; the returned string
mentions only the BAM and a params fingerprint. Verified against real
objects that the caller does reach the key -- VariantParams.as…

- *(frontend)* give assembled_transcripts its own role display
The backend added ObjectRole.ASSEMBLED_TRANSCRIPTS (D4) to distinguish a
StringTie-produced GTF, a proposed transcript model, from an authoritative
downloaded annotation. The frontend never learned ab…

- *(pipelines)* suppress --dta when spliced alignment is disabled
D6's flip of Hisat2Params.dta's default to True argued this cannot affect
DNA-seq alignments because the auto-suggested RNA-seq card only selects
HISAT2 for spliced assays. That's incomplete: the manu…

- *(pipelines)* reject non-BAM formats in launch_transcript_assembly
_check_quantifiable is shared with featureCounts and accepts BAM/SAM/CRAM,
but StringTie 2.2.1 only reads BAM. The Actions-tab card only offers BAM
objects, but POST /pipelines/transcript-assembly is …

- *(pipelines)* stop mosdepth's help text promising CRAM support
TOOL_META's summary is served on /help/software and said mosdepth reads
"an indexed BAM or CRAM". True of the binary, false of BioFlow's coverage
job: build_coverage_card returns None for any non-BAM,…

- *(tests)* patch tools.delly in SV card tests instead of trusting the environment
CI's backend suite runs inside ghcr.io/syntheticgio/bioflow-ci-runner, a
separate, hand-maintained image (.github/Dockerfile.ci) that does not
install Delly (or Sniffles) -- unlike the app's own Docke…

- *(frontend)* route Delly SV callsets to the SV results view
Results-tab routing, the SvRecord.support doc comment, and two
user-facing strings all assumed Sniffles2 was the only structural
variant caller. facts.variants_called_by is now "sniffles2" or
"delly",…

- *(pipelines)* name Delly SV output .delly.vcf.gz, not .sniffles.vcf.gz
_sv_payload hardcoded the .sniffles.vcf.gz output_name suffix
regardless of which caller ran, so a launch-path Delly callset was
always stored on disk under a Sniffles-looking filename -- the
handler'…

- *(pipelines)* read SV support per caller, not from Sniffles' key alone

- *(sv_provenance)* use presence check instead of falsy override for caller
The fallback to "sniffles2" was using `or`, which incorrectly overrides any
present-but-falsy value (empty string, None). Changed to a true presence check
with `in` operator, so the fallback applies o…

- *(queue)* stamp SV provenance with the caller that actually ran

- *(services)* put the caller in the SV dedup key so two callers cannot collide

- *(frontend)* stop the depth tooltip reading "typical depth of mean"
formatRatio already renders "1.67x typical depth", so appending "of mean"
produced "1.67x typical depth of mean" -- and the baseline is the median
rather than the mean, which is the distinction format…

- *(queue)* drop the other mode's facts when a coverage run replaces it
Facts merge onto the object, so a region run left the previous windowed
run's `coverage_window_count` sitting beside its new
`coverage_region_count` -- two mutually exclusive descriptions of the one
r…

- *(ops)* run the worker under the app interpreter, not Medaka's
The image puts Medaka's venv first on PATH (the polish jobs call
`medaka` by bare name, so it must stay there), which makes a bare
`python` Medaka's interpreter -- one with none of the app's
dependenc…

- *(sv)* wire merge structural variants card to sibling SNF callsets (#713)
The merge SV card sent only the clicked SNF's ID to the launch endpoint,
which reject_merging fewer than 2 callsets. Added sibling_snf_callsets()
to pipeline_service to find all SNF sidecars in a proj…

- *(tests)* read /proc for process liveness instead of shelling out to ps
The process-group cancellation tests failed in CI with "the process group
was not signalled" -- the opposite of what had happened. The group was
signalled and the grandchild was killed; `_pid_alive` j…

- *(services)* map the SV-merge endpoint so its Launch button greys out
The card added in #690 posts to /pipelines/merge_structural_variants, which
ENDPOINT_JOB_TYPES did not know, so running_now could not tell the UI a
merge was already in flight -- the Launch button sta…

- clear every lint error and test failure in the tree, not only this branch's
CI's ruff job reported 17 errors across backend/app, backend/tests, ops and
e2e, most of them older than this branch, and the suite carried three
failures written off as pre-existing. Splitting those …

- *(tests)* build the Mongo test URI by rebuilding its query, not by substring
Two bugs that only the worktree runner's URL shape exposes, both inherited
from the munging this moved out of the beanie_models fixture.

- *(tests)* connect to Mongo by service name when running inside the network
The mongo: -> 127.0.0.1: rewrite in the beanie_models fixture assumed
pytest runs on the host, where the compose service name does not resolve
and the published port is the only way in. Inside the api…

- *(ops)* make worktree test runs reach pytest and a valid Mongo URI
Two defects in the worktree test harness, both of which stopped
`./backend/run-worktree-tests.sh` from running at all in this image:

- *(pipelines)* close the bracken tool entry and import SidecarRole
Two errors that ruff caught as more than style, both of which broke code
rather than formatting:

- *(pipelines)* close three cross-task gaps found by the final SV review
The final whole-branch review of the Sniffles2 structural-variant plan
(#619, since independently merged via #671/#672/#690) found four Important
issues, each visible only across task boundaries -- no…

- *(pipelines)* give SV merge its own run kind (#690)
merge_structural_variants reused RunKind.STRUCTURAL_VARIANT_CALLING, so the
(run_kind, run_tool) pair was not unique and workflow_derive._node_type_for
returned the last matching spec -- deriving a ca…

- *(pipelines)* correct salmon docs and port requirement after the per-job indexing ruling
The controller ruled that the Salmon index is built fresh inside each
job's workdir and discarded, not cached and reused across samples via
a sidecar. Three pieces written against the original reuse p…

- *(queue)* detect a salmon mate file the same way this codebase's other handlers do
reads2_blob_id is a key nothing in this codebase ever sets, so the
paired-end branch in salmon_quantify could never fire and every job
silently ran single-end. Switch to mate_sha256/mate_path, matchin…

- *(pipelines)* sort salmon test imports per isort convention
The prior E402 fix moved Path to the top of the file but left it
ahead of pytest, which ruff's import-sort rule (I001) still flagged.

- *(pipelines)* move Path import to top of salmon test file
E402 violation: module-level import not at top of file. Moved the pathlib
import from line 161 (mid-file) to the top-of-file import block alongside
pytest and ValidationError.

- *(pipelines)* drop unused forward-provisioned imports from salmon_runner

- *(ops)* skip heredoc bodies in compose_target guard (#674)
Heredoc bodies contain data passed to stdin, not host commands. Leaving
heredoc body lines in place caused prose carrying shell operators (e.g. ;, &&)
to be split into command segments, misfiring bloc…

- *(docker)* vendor upstream salmon binaries to prevent SIGILL on arm64 (#669)
Debian trixie's salmon 1.10.2 arm64 package crashes with SIGILL (exit 132)
during salmon quant on Apple Silicon / ARM hypervisors due to CPU extension
instruction set mismatches. Replaces Debian packa…

- *(pipelines)* accept GTF annotations for feature coverage, fix wiring and sort corruption (#662)
Three bugs found in a final whole-branch review of the bedtools feature-coverage
pipeline, each missed because its own task's review was scoped too narrowly:

- *(provenance)* classify polish_long_assembly in narration verbs
polish_long_assembly (the Medaka handler registered earlier in this
plan) had no entry in provenance_walker's _STEP_VERBS, so its
provenance step silently fell back to the generic verb instead of
"pol…

- *(queue)* execute medaka_consensus wrapper, not the raw medaka binary
polish_long_assembly passed tools.medaka()'s probed path (the raw medaka
dispatcher, settings.medaka_binary_path) into build_consensus_command,
which builds argv for the medaka_consensus wrapper scrip…

- *(pipelines)* stop medaka's env from absorbing later pip installs
Medaka's ENV PATH addition sat right after its install layer, so every
bare pip install later in the build (NanoPlot, pydeseq2, ragtag, and
the app's own dependency set) resolved pip through Medaka's …

- *(pipelines)* add frameshift detection and correct position count semantics

- *(pipelines)* drop unused pathlib import in feature_coverage_runner tests
CI's ruff check caught it; the local suite never runs ruff, so it stayed
green while this failed only on push -- same shape as the #217/#314
import-order gap CLAUDE.md documents.

- *(services)* register feature_coverage in provenance and running-now maps
The full suite caught two hand-maintained registries Stage 1's tasks never
touched: provenance_walker's narrative-step classification (feature_coverage
belongs beside run_bam_stats -- facts written ba…

- *(ops)* bound apt-get fetches in launcher-build.yml's Linux step (#658)
The Linux bundle job ran a bare `sudo apt-get update` with apt's
default fetch behaviour — the same exposure that hung build-check.yml's
backend-smoke job on PR #609 (#577). The frequency is lower bec…

- *(pipelines)* correct seqkit citation and mark both tools not runnable
seqkit's TOOL_META cited the superseded 2016 bioRxiv preprint instead
of the SeqKit2 paper the README's own Citation section now points to.
Also set runnable=False on both bedtools and seqkit: no job …

- *(ops)* bound network fetches in the full backend test job too
backend-full-test landed on main while this branch was in flight and
carries the same unbounded-fetch defect: a bare `apt-get update`, plus
two curl downloads from third-party hosts (NCBI's FTP server…

- *(ops)* bound apt-get fetches so a stalled mirror fails fast
Both apt-get steps in the backend-smoke job fetched with apt's defaults,
so a mirror that accepted the connection and then stopped sending had
nothing to bound it. On PR #609 that hung "Install system…

- *(build-check)* move mongod --replSet and --bind_ip_all to cmd field
--replSet and --bind_ip_all are mongod arguments, not Docker create flags.
Passing them via 'options' caused 'unknown flag: --replSet' at container
start. Use 'cmd' for the container entrypoint argume…

- *(pipelines)* match real meryl output, scan assembly for repeats
Every meryl-analysis run parsed to nothing because both parsers expected
formats meryl never emits (verified against meryl 1.4.2 in the worker
image):

- *(queue)* apply analyze_meryl_tracks facts to the assembly object
The handler returned {object_id, job_id, facts} exactly like
analyze_gc_tracks, but _APPLIERS had no analyze_meryl_tracks entry and
results.apply() silently returns for unknown job types -- so kmer_sp…

- *(backend)* keep strong references to fire-and-forget asyncio tasks
The event loop holds only a weak reference to a scheduled Task, so the
feedback-webhook notification and the agent command-rejection error event
could be garbage-collected before running -- silently d…

- *(pipelines)* preserve explicit minimap2 choices

- *(pipelines)* reject non-finite minimap2 preset floats

- *(pipelines)* omit unset minimap2 overrides from presets

- *(pipelines)* wrap minimap2 numeric parse errors

- *(pipelines)* reject minimap2 numeric sentinels

- *(protein-structure)* distinguish a UniProt outage from a zero-candidate result
`protein_structure.resolve()` returned bare None for two different
situations: a UniProt outage/timeout and a query that succeeded but
matched nothing. The Structure tab mapped both to `lookup_failed`…

- *(queue)* stop index_bam and ingest_headers erasing each other's facts
Both appliers loaded the whole facts dict, merged their own keys in
memory, and wrote the merged dict back. After an alignment they finish on
the same BAM at nearly the same moment, so whichever loade…

- *(queue)* fail a chunked alignment that refuses to merge, don't report success
`_apply_align_reads_chunked` refuses to enqueue the merge when it cannot
resolve a BAM for every bucket sub-job -- merging a partial set produces a BAM
missing a slice of the reference while claiming …

- *(queue)* let an applier fail its job instead of reporting a silent success
`_apply_result` caught every exception and logged `result_apply_failed`, so a
job whose applier gave up still reported succeeded. That is right for the case
it was written for -- a write-back that bro…

- *(frontend)* preserve preset while schema loads

- *(aligners)* preserve Bowtie2 preset provenance and pairing semantics

- *(frontend)* default Bowtie2 to standard preset

- *(frontend)* refresh Bowtie2 preset resolution

- *(models)* pin mean_plddt to the 0-1 scale the pipeline stores it on
test_create_prediction passed mean_plddt=85.5 and failed validation
against the field's le=1.0 bound. The reported diagnosis was that the
constraint rejects what the sidecar emits, but the call site s…

- *(export)* validate report paths and bytes before packing
Reject symlinked or escaped report paths during discovery and packing, and verify report snapshots against their recorded size, digest, and threshold before writing tar members.

- *(export)* reject escaped report files

- *(queue)* refuse to merge a chunked alignment missing any bucket
_apply_align_reads_chunked skipped past any bucket sub-job whose BAM it
could not resolve -- a lookup error, a non-succeeded job, or a success
that recorded no output path -- and then merged whatever …

- *(pipelines)* give the chunked merge a sort command that exists
merge_chunked_buckets called align_runner.build_sort_command, which was
never defined there -- the only build_sort_command in the tree lives in
ivar_runner with a different signature. Every chunked al…

- *(ui)* break the QC picker's recommended-tool tie explicitly, not by dict order
For short reads both fastp and fastqc carry RECOMMENDED in the QC family,
and PipelineToolSelector auto-selected the *first* recommended tool it
found -- so which tool the QC dialog opened on depended…

- *(tests)* point parameter-set no-fields tests at hifiasm, not spades
SPAdes gained its own parameter fields, so tests hardcoding it as the
"tool with no fields" example started asserting the opposite of what
they meant. HIFIASM still has no declared fields and is the t…

- *(frontend)* drop raw reads from the align picker once their trimmed version is added
The additional-reads dropdown excluded files by object id alone, so
adding a trimmed read set left its raw parents on offer -- different
objects, same sample name, which reads as the dialog listing fi…

- *(ci)* bound Build check jobs with timeout-minutes
Backend smoke and its siblings had no job- or step-level timeout, so
a stalled apt-get mirror on "Install system build dependencies" hung
several in-flight runs for over an hour with nothing to fail t…

- *(pipelines)* decompress the reference for HISAT2, not just for STAR
build_index decompressed a gzipped reference only in the STAR branch, so
hisat2-build was handed the .fna.gz path directly. It exits 1 partway
through and deletes the .ht2 files it had already written…

- *(objects)* name the accession from metadata when remote_source is unset
Found by running the Stage 3 checks against the real database rather than
fixtures. All 55 SRA-backed objects in the live project carry their
accession in metadata.sra_run and nothing in remote_source…

- *(ui)* give system upkeep its own section on the activity page
Storage cleanup was listed among the user's own work, where it could not
be found. Verification alone posts a job a minute, so on this machine 137
of the 200 jobs the activity page fetches were sweeps…

- *(ui)* name maintenance sweeps in words, not their handler tokens
The activity page labelled a job by the file in its payload, falling back
to the job type. Maintenance sweeps act on the whole library and name no
file, so every one of them rendered as its raw handle…

- *(ui)* route a declared refusal to the card in every heavy-launcher dialog
CompletenessDialog, ScaffoldDialog, QuantifyDialog, and
DifferentialExpressionDialog launch heavy launchers this branch gave
resource-refusal details to, but their onError still fell through to a
plai…

- *(ui)* give the Actions refusal card working Edit feedback and button locking
The refusal card's Edit button always reads "Edit parameters" even for the
nine launchers with no settings dialog, where onEdit silently dismissed the
card instead. Toast the user instead of leaving a…

- *(pipelines)* refuse an over-budget differential expression run instead of stranding it

- *(pipelines)* refuse an over-budget quantify run instead of stranding it

- *(pipelines)* refuse an over-budget assembly error QC run instead of stranding it

- *(pipelines)* refuse over-budget scaffold, misassembly, and synteny runs
Groups scaffold, misassembly QC, and synteny together since all three
share the same draft+reference resolution shape and were built with
identical test coverage.

- *(pipelines)* refuse an over-budget consensus run instead of stranding it

- *(pipelines)* refuse an over-budget meryl analysis run instead of stranding it

- *(pipelines)* refuse an over-budget completeness run instead of stranding it

- *(pipelines)* refuse an over-budget variant calling run instead of stranding it

- *(pipelines)* refuse an over-budget continuity QC run instead of stranding it

- *(pipelines)* refuse an over-budget QV assessment instead of stranding it

- *(pipelines)* refuse an over-budget polish run instead of stranding it

- *(pipelines)* refuse an over-budget genome annotation instead of stranding it

- *(ui)* show the refusal card for a declared over-budget launch, not a toast

- *(api)* tag resource refusals so the frontend can route them to a card

- *(pipelines)* route SPAdes to a real progress parser instead of Flye's
Final-review fix wave for the SPAdes short-read assembler, three findings:

- *(pipelines)* register SPAdes in all_tools() so it reaches the tools panel
tools.spades() itself works correctly and returns an available Tool, but
all_tools() is a hand-maintained list of function calls and was never updated
to include it. This meant SPAdes never reached th…

- *(api)* serialize from_parameter_set on run responses
RunOut declared no from_parameter_set field, so Pydantic silently
dropped it from every GET /runs response, leaving the run-detail
"Preset" display permanently dead. Add the field to RunOut and thread…

- *(ui)* detect a real 409 status on parameter-set save conflicts

- *(ui)* let the database index use the full window width
The page carried .help-page's 760px prose measure, which is right for a
column of text but not for this page: its body is a grid of database
cards, so two 340px columns sat against the left edge with …

- *(ui)* keep the info card on screen when its marker is not
Two placement bugs found by driving the real Quality tab rather than by
reading the code, both invisible to the test suite because there is no
DOM test setup here to catch them.

- *(export)* scope GET /exports and download route by owner (#555)
* fix(export): scope GET /exports and download route by owner

- *(ops)* stop the worktree guard blocking writes that merely mention compose
The hook matched `docker` + `compose` as a substring of the whole Bash
command, so it fired on commands that could not touch the biopipe stack: a
heredoc file write whose docstring said the phrase, an…

- *(export)* include project-scoped job timings in export archives
export_service.collect() gathered JobRunTiming rows by object_id alone.
executor.py only records object_id for a job that attached to a single
object, so project-level work -- and any job whose object…

- *(queue)* check the index-build declaration before enqueueing it too
launch_alignment enqueued an unchecked build_index job -- which declares
more memory than the align job it does check -- before the align-side
refusal ran at all. An over-budget index build could stil…

- *(queue)* refuse an alignment that could never be claimed

- *(queue)* refuse an assembly that could never be claimed

- *(queue)* keep the admission budget under the kernel hard limit

- *(export)* satisfy ruff's line-length rule on a test fixture
CI's ruff check runs a rule (line length) the local worktree suite never
invokes, same class of gap CLAUDE.md already documents for import-order
checks.

- *(ui)* say API keys are never collected, not removed
The export dialog's disclosure text claimed API keys, absolute paths,
and machine names are all "removed automatically" -- accurate for
paths and machine names, but API keys are excluded by constructi…

- *(export)* redact DataObject.source.original_path and worker_id
_strip_paths only checked top-level dict keys, so DataObject.source
(a nested SourceInfo) shipped its fully-resolved original_path
verbatim for every register-in-place object -- a username and
directo…

- *(ui)* show projected export size and redaction notice in the dialog
The export dialog was missing two things the brief required: an estimate
of the archive size at the chosen threshold, and a statement of what
gets redacted. Adds both.

- *(export)* pass owner to queue.enqueue in launch_project_export
queue.enqueue's owner parameter is required and keyword-only with no
default, so every call to launch_project_export raised TypeError before
this fix -- the export launcher could never actually queue …

- *(export)* stop counting an unset machine record as cleared
A default RunMachine() dumps to a dict of all-None values, which is
truthy, so the old check counted every job_timings row as having had
machine identity cleared -- even ones that never recorded any. …

- *(metadata)* sort test_protein_headers.py imports for ruff I001
Two subagents in this branch's execution flagged this exact I001 finding
independently, and both were dismissed as unreproducible against local
checks -- CI's exact invocation (ruff check ... backend/…

- *(api)* fix byte offsets, empty-index state, and unused has_reference
Three Important findings from the whole-branch review of the protein
structure viewer, all independently verified real:

- *(api)* use two_profiles fixture instead of a new local-owner one in protein record tests
Replaces the ad-hoc local_profile fixture (which called
Profile.find_all().delete() and adopted "local") with the existing
two_profiles fixture that test_object_computations.py already uses for this
e…

- *(models)* sort protein_record imports alphabetically for ruff I001
The import block in __init__.py now correctly orders profile before
protein_record alphabetically. The test file's imports now correctly
place app.metadata before app.models. Both changes satisfy ruff…

- *(frontend)* move Other running section into In Progress column (#537)
* fix(frontend): move Other running section into In Progress column

- *(frontend)* show confirmation dialog before running QC (#532)
* fix(frontend): show confirmation dialog before running QC

- *(pipelines)* satisfy ruff on the new abyss tests
CI's ruff check caught an unused local and an unsorted import block that
the local pytest run never exercises: an unread `changed` assignment in
test_abyss_progress_reports_a_phase, and assemblers imp…

- *(pipelines)* build ABySS-shaped defaults instead of raising on short reads
default_assembly_params called mode_for_chemistry unconditionally, which
does a bare mode_flags[chemistry] lookup -- KeyError for ABySS, whose
mode_flags is empty by design (no chemistry-graded mode; …

- *(frontend)* add explanation for Remove duplicate reads checkbox (#530)
Adds subtext explaining when to use duplicate removal, similar to the
existing polyG trimming explanation.

- *(frontend)* auto-select first recommended tool in pipeline tool selector (#529)
When opening a computation modal, the tool selector now auto-selects the
first recommended option based on the file's chemistry bucket. Falls back
to the first available tool if no recommendation exis…

- *(ops)* sync frontend/package-lock.json version on release (#522)
* fix(ops): sync frontend/package-lock.json version on release

- *(api)* use pipeline_service.launch_qc in qc_all endpoint (#521)
qc_all_reads was building the run_qc payload inline with only object_id,
but the handler requires r1_sha256 or r1_path and immediately fails with
'No input given'. Replaced the inline enqueue with pip…

- *(queue)* fail jobs that exceed machine memory at enqueue time (#517)
A job requesting more memory than the machine has (e.g. 16384 MB on a
12.4 GB machine) would sit in the ready set forever because claim.lua can
never admit it. Nothing told the user why — the job appe…

- *(queue)* say why a queued job isn't running, not just that it waits (#503)
* docs(queue): spec how a waiting job explains what it is waiting on

- *(api)* include annotation_stats_dir in report dir delete and copy (#514)
Both remove_report_dirs and copy_report_dirs in object_service.py iterated
only qc_reports_dir, bam_stats_dir, and vcf_stats_dir, silently omitting
annotation_stats_dir. This leaked annotation_stats/<…

- *(queue)* forward node_id and ready_key from claim() to claim.lua (#508)
queue.claim() accepts node_id and ready_key parameters but always called
the Lua script with keys.READY and omitted the node_id from ARGV[10].
The worker._try_claim_queue() correctly passes both, so t…

- *(api)* batch the drift sweep's walk and cap findings as they arrive
The storage-drift sweep did not hold up at the few hundred thousand files
its own design doc names as the target. Two independent ceilings, both
reached well before that scale:

- *(api)* add the missing /pipelines/meryl-analysis route so both meryl cards launch
The kmer_spectra and repeat_density cards both post to
/pipelines/meryl-analysis, but no such route was ever registered --
`pipeline_service.launch_meryl_analysis` existed and was never wired to
one. …

- *(ops)* default BIOINFO_HOME to FastDataExtension, not the retired disk
`.env` is gitignored, so the compose default is what runs whenever it is
absent -- a fresh clone, or any `docker compose` invoked from a directory
without one. That default pointed at /Volumes/ModelEx…

- *(frontend)* keep the settings tab bar from shifting on the Tools page
The Tools page widened `.settings-page` itself (`.settings-page-wide`,
860px -> 1400px) to give its tool list room for two columns. Since that
div is also what centers SettingsNav above it, the wider …

- *(api)* load the drift report singleton after detectors run, not before
Narrows the window during which a long sweep holds a stale in-memory
copy of the singleton before save()'s full-document replace.

- *(frontend)* keep Launch greyed across a reload, not just this tab's launch

- *(ai)* name an exhausted token budget when a reasoning model returns no text (#497)
A reasoning model can burn its entire llm_max_tokens on reasoning_content
and come back with content: ''. Previously this collapsed into a bare
BAD_RESPONSE indistinguishable from genuine garbage. Now…

- *(ops)* reject a restore whose recorded database disagrees with MONGO_DB
mongorestore --archive restores into the database name embedded in the
dump, not $MONGO_DB -- so a restore run with a non-default MONGO_DB would
silently write into the wrong database after prompting …

- *(ops)* brace $dir before the ellipsis so restore runs under a UTF-8 locale
`log "Restoring from $dir…"` and the matching line in cmd_verify parse as
${dir…} when LC_CTYPE is UTF-8 -- bash reads the ellipsis bytes as
identifier characters -- so `set -u` killed both subcommand…

- *(ops)* address provider summary to a container name, not a compose service
write_provider_summary() hardcoded `docker compose exec -T api`, which
resolves through the compose project rather than a container name -- the
same trap CLAUDE.md documents for bare `docker compose` …

- *(frontend)* keep a suggestion card's Launch greyed until its job ends
The Actions-tab cards disabled Launch only for the duration of the POST
-- milliseconds -- so a card re-enabled the instant the job was queued
and stayed enabled for the hours it ran. Reported against…

- *(pipelines)* attribute a chunked alignment to the reads it aligns
The chunked path enqueued align_reads_chunked without object_id, unlike
the single-shot path beside it. /jobs?object_id= is how every "is
anything running on this file?" surface answers the question, …

- *(e2e)* parse MCP Streamable-HTTP SSE responses (#486)

- *(queue)* drop dead workers' heartbeats instead of listing them as nodes

- *(api)* verify a node's host key through the client, not known_hosts
`connect_with_tofu` passed its host-key callback as `known_hosts=`. asyncssh
treats a callable there as a known-hosts *lookup* -- it calls it with
`(host, addr, port)`, three strings, and expects the …

- *(e2e)* add trailing slash to MCP URL and render test descriptions

- *(frontend)* drop the pair-group spine from stage-rail cards
The RAW/TRIMMED and REFSEQ/GENBANK toggle header already brackets a
card's rows, so the vertical spine inherited from .pair-group -- meant
to bracket a bare labelled pair -- was redundant, showing a l…

- *(frontend)* date each recent run, not just its time of day
The recent-runs ledger showed "02:14:07 PM" and nothing else, so a run
from last Tuesday was indistinguishable from one an hour ago -- the
column reads as a list of times with no way to place them.

- *(api)* name the provider and reason when a prose call fails
The History tab's "Generate paragraph" reported "The model call did not
succeed." and nothing else. The `Failure` returned by `ai.complete()`
already carried both a coarse `reason` and a scrubbed upst…

- *(queue)* don't dispatch a job whose dependencies say it cannot run
`_handle_dependencies` decides three different things -- the job has a failed
dependency, the job must wait, the job is runnable -- and returned None for
all three. `enqueue` could not tell them apart…

- *(pipelines)* classify the annotation export launcher only as a node type
`pipeline_service.launch_annotation_export` was in both `NODE_TYPES` and
`EXCLUDED_LAUNCHES`, which made `TestExhaustiveness::
test_no_launcher_is_both_used_and_excluded` fail on `main`.
`test_every_l…

- *(queue)* stop a progress bar's leading CR from logging a blank line
A tool that prints a normal line and then redraws a progress bar -- `printf
'start\r\n'` followed by a `\r`-redrawn bar, which is what fasterq-dump and
prefetch do -- had a blank line reported between…

- *(services)* rename connect_with_tofu timeout param to satisfy ASYNC109
ruff's ASYNC109 flags a `timeout` parameter on an async function since
it shadows asyncio.timeout(); renaming to timeout_seconds keeps the
existing asyncio.wait_for-based deadline without restructurin…

- *(services)* pin node SSH host keys on first use instead of disabling verification
Implements trust-on-first-use (TOFU) host key verification for SSH connections
to compute nodes. Adds a connect_with_tofu helper that captures the server host
key on first connection and enforces it o…

- *(api)* generate the node compose file instead of reading /srv (#426)

- *(services)* warn when an upload chunk arrives without a client digest (#425)
Logs a warning when write_chunk is called without an expected_sha256 digest,
so the unverified path is visible in the logs rather than silently
indistinguishable from the verified one.

- *(ai)* scrub API keys from crash logs in complete() and complete_sync() (#415)
Both functions logged `str(e)` verbatim when an adapter call crashed. A
provider client library that echoes the request into its exception message --
a URL with the key in a query parameter, a header …

- *(frontend)* stack the nodes settings page so the banner spans its column
The provisioning banner's buttons overhung the box border because the banner
was ~40px wide, not because of how its contents wrapped. `.settings-body` is
the shared rail-and-detail flex *row* from the…

- *(frontend)* wrap the provisioning banner instead of overhanging its border
The result banner was a single unwrapped flex row in a narrow settings column,
so the Try Again and Close buttons crowded the rounded corner while the message
was squeezed to a few words per line. A f…

- *(api)* create the node install dir under the remote user's home, not root's
Provisioning expanded `~/.bioflow` with os.path.expanduser() inside the API
container, which resolves against that container's own HOME. The container
runs as root, so the command sent over SSH was `m…

- *(frontend)* fix UniProt downloader checkbox sizing and default selection
Row checkboxes inherited the global input reset (border, radius,
padding), squashing them into tiny rounded dots; scope a checkbox
override to .sra-table so they render normally. Also stop
pre-selecti…

- *(e2e)* copy frontend into plugin dir and surface query errors

- *(e2e)* copy backend into dashboard dir and use a flat shim

- *(launcher)* import vitest globals so tsc can compile the update tests
src/update-logic.test.ts used describe/it/expect without importing them,
unlike its three sibling test files. Vitest injects those globals at
runtime, so `npm run test` passed and nothing looked wrong…

- *(frontend)* keep the extract-sequence button disabled until it lands
extractMutation.isPending goes false the instant the job is queued, well
before the applier has actually created the reference -- so the button
re-enabled and invited a second click during that gap. T…

- *(pipelines)* classify the GenBank sequence launcher as excluded

- *(provenance)* classify extract_genbank_sequence in the step-verb registry
Registering the handler in Task 4 made test_every_registered_handler_is_classified
fail: _STEP_VERBS/_NO_NARRATIVE_STEP partition every registered handler, and this
one had no entry in either. Same re…

- *(pipelines)* classify the annotation stats and export handlers for provenance
Both were unclassified in _STEP_VERBS/_NO_NARRATIVE_STEP, the same
exhaustiveness gap fixed for the canvas registry in the prior commit.
run_annotation_stats writes facts onto the existing object with…

- *(pipelines)* exclude the annotation launchers from the canvas registry
launch_annotation_stats and launch_annotation_export both failed the
exhaustiveness test that requires every launch_* to be classified as a
canvas node type or explicitly excluded. Neither has a fixed…

- *(ui)* stop firing the export-count query on the Genes tab
The export control is only visible when view !== 'genes', but its backing
count query's enabled condition didn't include that check -- so it kept
refetching on every filter change while the user sat o…

- *(pipelines)* honor a locus jump's coordinate window in annotation export
The export preview and launch read the raw contig filter state and never
saw the Locus jump input's min/max at all, so exporting after jumping to a
specific region silently operated over the whole unb…

- *(api)* remove a duplicated export precondition and close a traversal gap
launch_annotation_export's own 'no computed results' check raised
ValidationError (422) while the identical condition is NotFoundError (404)
everywhere else, including the same route's own guard right…

- *(queue)* attach an exported annotation subset to its run
Every other ingest-creating applier in this file resolves the job's run and
records the new object as one of its outputs, so it appears in the
run/activity view. This applier ingested the subset but n…

- *(pipelines)* fail verification on an unindexed line, not just a mismatched one
write_subset's safety property -- verify every line against the index before
writing it -- previously held only when every line number in the caller's
set happened to already be a key the index record…

- *(nodes)* use AppError hierarchy in /nodes router instead of bare HTTPException
The /nodes router raised bare HTTPException at 11 call sites, producing
{"detail": ...} bodies that the frontend's client.ts cannot read — it
looks for body.message and falls back to a bare "409 Confl…

- *(nodes)* pass PEM string to import_private_key instead of StringIO
asyncssh.import_private_key expects a str or bytes, not a file-like
object. The io.StringIO wrapper caused an AttributeError at runtime
whenever key-based node provisioning was attempted, because
impo…

- *(pipelines)* classify annotation_stats only as an excluded launcher
This reverts commit bdfec631, which added a NodeTypeSpec for
annotation_stats. Two commits landed independently for #355, each a
complete fix on its own: bdfec631 added the spec, and a3f54da0 added th…

- *(pipelines)* classify launch_annotation_stats as an excluded launcher
Exhaustiveness check in test_node_types.py flagged it as unclassified.
It's a read-only Results computation over an existing GFF/GTF/BED/GenBank
object, auto-triggered at ingest and callable on demand…

- *(pipelines)* add a NodeTypeSpec for annotation_stats
launch_annotation_stats had no entry in NODE_TYPES or EXCLUDED_LAUNCHES,
failing test_every_launch_function_is_classified on every run. Its
sibling bam_stats/vcf_stats already had entries; this one wa…

- *(pipelines)* classify run_annotation_stats as a no-narrative-step handler
Same shape as run_bam_stats and run_vcf_stats: it writes statistics back
onto an existing object rather than producing a new one, so it belongs in
_NO_NARRATIVE_STEP alongside them. #257 added the han…

- *(ci)* allow workflow_dispatch to bypass the tag version-guard
The version-guard job's if condition only matched tag pushes, so a
workflow_dispatch (intended as a 'does this still build' smoke test)
skipped the guard and cascaded to skip the entire workflow. Allo…

- *(api)* add explicit strict= to the row-packing zip
CI's ruff check (B905) flags zip() without strict= -- a rule the local
worktree test run never invokes. features and rows are guaranteed the same
length here (rows is built from a list comprehension o…

- *(pipelines)* wire launch_annotation_stats to two-tier reference resolution
Task 10's browser verification found the track's "no coordinate axis" refusal
firing for every real annotation in the seeded dataset. Traced to
launch_annotation_stats still calling _reference_for_ann…

- *(pipelines)* require the reference role in accession-tier resolution
Task 7's real-database check (docs/superpowers/plans/2026-08-12-annotation-track-viewer.md)
found that resolve_annotation_reference's accession tier filtered candidates
on FormatKind.FASTA alone, not …

- *(pipelines)* enforce the reference-or-reason invariant on AnnotationReference

- *(pipelines)* prefer the reference role when resolving an annotation's genome
_reference_for_annotation took the first FASTA in derived_from with no role
check, so an annotation downloaded alongside its protein.faa could resolve to
the protein set. Matches reference_for_bam's p…

- *(pipelines)* treat a self-referencing parent as absent, not a silent drop
Correct the features_in_window docstring, which claimed two queries
when the code issues one SELECT (parents and children together) then
reassembles the tree in two Python passes.

- *(annotation)* update genbank_parse tests for the Feature.parents rename
The rebase onto main pulled in GenBank annotation support, which
constructed Feature with the pre-Task-1 parent kwarg and asserted
against the same singular field in its own tests. genbank_parse.py's
…

- *(annotation)* update owner-scoping test for Feature.parents rename
test_route_owner_scoping.py still constructed Feature with the old
`parent=None` kwarg, which Task 1 replaced with `parents: tuple[str, ...]`.
The test was failing collection/setup with a TypeError, m…

- *(annotation)* wrap views in TabPanel and use the server's echoed depth_cap

- *(annotation)* fix the third parent= kwarg site broken by the Feature.parents rename

- *(annotation)* make build_gene_table re-runnable and distinguish same-coordinate leaf types

- *(annotation)* dedupe child_count the same way descendant_count already is

- *(annotation)* keep every parent of a multi-parent GFF3 record, not just the first

- *(annotation)* keep GenBank out of the featureCounts annotation pool
_is_annotation gates more than annotation-Results eligibility -- it also
backs annotations_for_project/resolve_annotation, which feed
launch_quantify. Adding GENBANK there (Task 8's original change) m…

- *(annotation)* harden the GenBank contig-lengths ordering dependency
Two issues from Task 7's code-quality review, both about the same root
cause: nothing enforced that _genbank_rows' generator was fully drained
before the handler read its side-channel facts dict, so a…

- *(formats)* tighten GenBank sniff to a literal space, cover compressibility

- *(pipelines)* stop misreading a short continuation line as a new feature
iter_features's key-detection check only required a line to reach column 6
before testing whether column 22 held non-whitespace. A wrapped qualifier's
tail line shorter than column 22 has an empty sli…

- *(ui)* surface update-start failures and clear stale progress panels across nodes
startUpdate had no onError handler, so a 409/404 from POST /nodes/{id}/update
rejected silently with the confirmation dialog just re-enabling its button.
Also, clicking Update on a new node left a pri…

- *(services)* make run_update's own error handling failure-tolerant
The initial task lookup and _fail's own task.save() were unguarded, so a
transient Mongo hiccup during either could raise out of run_update -- a
fire-and-forget background task with no caller to catch…

- *(services)* log at node_ssh's failure points, not just raise

- *(api)* run current-version's docker probe off the event loop
_own_image_digest shells out to docker twice via subprocess.run, up to
~20s worst case, blocking the single event loop thread and stalling
every other request the primary serves (node heartbeats, job …

- *(queue)* resolve image digest through the container's own image id, not a hardcoded tag

- *(models)* use the repo's timezone-aware utcnow() in NodeUpdateTask
datetime.utcnow() is deprecated and timezone-naive; match the local
utcnow() pattern node.py already uses for this purpose.

- *(api)* register NodeProvisionTask so provisioning can write its task document
Registering the model in ALL_MODELS activated a previously dead code
path in db.index_reconcile: NodeProvisionTask.Settings.indexes held a
bare field-name string ("task_id") instead of an IndexModel, …

- *(frontend)* clarify the per-contig coverage loading state
The table showed a bare "Loading…" whenever the query had no data yet, so a
failed request left "Loading…" on screen indefinitely. Show the pending
state only while the request is in flight and a dist…

- *(frontend)* align coverage-chart titles on a shared baseline
The Insert size and Mapping quality cards sit in a flex row, but each still
carried the .section class, whose margin-top offsets every card after the
first. That pushed "Mapping quality" below and rig…

- *(agent)* await the project-context services the system prompt depends on
#290's project-context injection made _system_prompt and
_build_project_context call two async services, search_service.count_by_kind
and project_service.recent_jobs, without await. Every /ask and /re…

- *(metadata)* classify molecule_type and library_source as open/closed
Two new ENUM fields (2e073b8f) had no open/closed classification, tripping
test_schemas_open_vocabulary's tripwire: 17 distinct ENUM FieldDefs, 15
accounted for. molecule_type (DNA/RNA/Other) is a voc…

- *(api)* rewrite Docker service hostnames in URLs handed to provisioned nodes
_rewrite_host built its pattern with an f-string containing `:\\d+`, which in
an f-string is a literal backslash followed by "d" rather than the digit class
`\d`. So `mongodb://mongo:27017/db` never m…

- *(models)* register NodeProvisionTask so node provisioning can write
NodeProvisionTask was never added to ALL_MODELS, so init_beanie never
created its collection. Node provisioning from Settings -> Nodes failed at
runtime: _provision_node writes a NodeProvisionTask doc…

- *(ops)* fail a release tag whose launcher version files disagree
Tauri reads tauri.conf.json, not Cargo.toml, so a stale one publishes
bundles labelled with the wrong version while every other check passes.
That is what shipped as launcher-v0.2.0 (#335).

- *(launcher)* bump tauri.conf.json so bundles carry the release version
Tauri reads tauri.conf.json's version key in preference to Cargo.toml, and
bump_version.py never wrote it -- so launcher-v0.1.0 and launcher-v0.2.0 both
published bundles named 0.1.0. Pre-releases get…

- *(pipelines)* drop an unused Feature import in annotation_db
build_annotation_db's rows parameter is untyped, so Feature never
appears as an annotation anywhere in this file -- caught by CI's
ruff check, which flagged F401 on a pass the local pre-push check
mis…

- *(frontend)* drop a dead AskQuestionResponse import
Left behind by rebasing onto main: the conflict in client.ts's import
block had AskQuestionResponse on one side, and resolving it kept the
name without checking it still exists on the branch being reb…

- *(pipelines)* dispatch run_annotation_stats via THREAD, not SUBPROCESS
Pure in-process parsing and SQLite writes, no binary to spawn or kill
via process group -- same shape as run_transcript_qc, which already
uses THREAD for the identical reason. Also documents the attri…

- *(pipelines)* stop exon/CDS rows colliding with their transcript's feature_id
parse_gtf_line's else-branch set feature_id equal to parent
(transcript_id or gene_id) for every exon/CDS/UTR row, so siblings
under the same transcript were indistinguishable from each other and
from…

- *(ci)* move workflows from self-hosted to GitHub-hosted runners
The repo going public unlocks free hosted ubuntu-24.04-arm and macos-latest
runners, removing the constraint that put arm64 Docker builds and macOS
launcher signing on self-hosted machines. publish-im…























## [0.5.1] - 2026-08-13




### 🚀 Features

- *(frontend)* offer sequence extraction on a GenBank's Results tab

- *(frontend)* add the GenBank sequence extraction API calls

- *(api)* add endpoints to extract and look up a GenBank's sequence

- *(pipelines)* guard GenBank sequence extraction against a second run

- *(pipelines)* ingest an extracted GenBank sequence as a reference

- *(pipelines)* add the extract_genbank_sequence handler

- *(pipelines)* stream a GenBank ORIGIN block into wrapped FASTA

- *(pipelines)* parse bases out of a GenBank ORIGIN line

- *(ops)* add local commit-msg hook mirroring CI's subject check

- *(annotation)* edit and materialize annotation records

- *(e2e)* add fixtures and starter tests

- *(e2e)* add desktop plugin page

- *(e2e)* add harness backend (clients, store, loader, runner, API)

- *(e2e)* add install script

- *(e2e)* add harness config loader

- *(e2e)* scaffold harness directory and Python environment

- *(icons)* add reads-type tooltips showing Illumina/PacBio/Nanopore/Unspecified
The reads-type icons (reads_raw_illumina, reads_raw_pacbio,
reads_raw_nanopore, and their trimmed variants) in the Project Explorer
and other object surfaces had no hover tooltip — the SVG <title> chi…

- *(ui)* export the annotation table's current filter
Shows matched and exported counts together: the closure adds parents and
children, so the exported number is routinely larger, and showing only one
of the two reads as a bug.

- *(api)* add annotation subset export routes
The count route is separate from the export so the UI can show matched
against exported before anything is queued: the closure is routinely larger
than the matched count, and an unexplained difference…

- *(queue)* register an exported annotation subset as a derived object
derived_from the source annotation, so the subset's lineage is inspectable.
Carries no annotation results facts: the subset opens to a Compute results
button like any other annotation, leaving whether…

- *(queue)* add the annotation subset export handler
Receives the filter as it was applied in the table and passes it straight to
closure_lines -- it never re-derives the filter, which is what keeps the
exported subset and the displayed table from drift…

- *(pipelines)* write a verified annotation subset
Copies original source lines rather than serializing Feature, which keeps
neither the GFF source column nor phase -- a reconstructed CDS line would
silently lose its reading frame.

- *(pipelines)* compute the closure of an annotation filter
A filter matches rows, but a valid annotation needs whole trees: a gene
without its exons leaves Parent= references dangling, and exons without
their gene leave orphans. The closure is every match plu…

- *(pipelines)* store each feature's source line in the annotation index
No index on the column: subset export selects by filter and reads the line
out, it never looks a feature up by line. Rows sharing one source line
under multiple parents all record that line.

- *(pipelines)* record the source line number on every parsed feature
Set by the handler's read loop rather than the parse functions, which take
one string and cannot know where it came from. None for GenBank, whose
features span multiple lines and whose segment childre…

- *(frontend)* pair RefSeq/GenBank twins with a namespace toggle
A reference downloaded from NCBI arrives as either a GCA (GenBank) or GCF
(RefSeq) accession, and a project that needs both currently shows them as
two separate rows. They are one assembly in two name…

- *(launcher)* stage update notification for alpha/beta users
Adds a new stage-update affordance that detects when a newer alpha or beta
tag is available and offers a one-click update with confirmation dialog.

- *(launcher)* add update_to_stage Tauri command

- *(launcher)* add update_to_stage action and tests for check_stage_update

- *(launcher)* add check_stage_update and set_bioflow_tag helpers

- *(ui)* say why annotation coverage is missing instead of drawing blank bars

- *(pipelines)* backfill annotation analysis when a reference lands

- *(pipelines)* analyze an annotation when it finishes ingesting

- *(pipelines)* record whether an annotation analysis resolved a reference
Adds contig_lengths_known to the launch_annotation_stats payload and
annotation_contig_lengths_known to run_annotation_stats's facts, so the
ingest backfill (a later task) can find annotations analyze…

- *(pipelines)* add the auto-analysis eligibility rule for annotations

- *(storage)* add resolve_report_file containment helper
Both stats report endpoints hand-roll the same three-step guard with two
different spellings of the containment check. Extract it so a third result
type inherits the guard rather than re-deriving it -…

- *(ui)* show the annotation track above the feature table

- *(ui)* draw annotation features along a coordinate axis

- *(frontend)* add the annotation window client and its types

- *(api)* serve annotation features or their density for a window

- *(pipelines)* resolve an annotation's reference by provenance or accession

- *(pipelines)* assemble gene models for a coordinate window

- *(pipelines)* count annotation features per coordinate bin

- *(pipelines)* pack overlapping annotation features into rows

- *(annotation)* report unresolved parentage on the results summary

- *(annotation)* add a genes/all/unresolved view toggle to the feature table
Adds the frontend half of the parent-resolution and gene-summary work
from tasks 1-6: a three-tab toggle (Genes/All records/Unresolved) above
the annotation feature table, backed by the new /genes rou…

- *(annotation)* serve the genes view and reach unresolved records over the API
The features route gains a `view=unresolved` parameter that clears
top_level_only and filters to UNRESOLVED_STATUSES, making a dangling/
ambiguous/self/cyclic record reachable for the first time. A ne…

- *(annotation)* resolve hierarchy and build the gene table during the compute job
run_annotation_stats now classifies every feature's parent reference and
builds the genes table against the temporary database, before the atomic
rename into place -- a database is only published once…

- *(annotation)* build a per-gene summary table with de-duplicated descendant counts

- *(annotation)* store one row per parent relationship and filter by resolution status
Fixes build_annotation_db, which crashed after Feature.parent became
Feature.parents (a tuple) in the previous commit: it now writes one
row per declared parent relationship so a shared exon is reacha…

- *(annotation)* classify every parent reference as resolved, dangling, ambiguous, self, or cyclic

- *(ui)* label, icon, and results tab for GenBank files
Adds the "GenBank" format label and maps it to the existing GFF
annotation icon. DetailPanel also gated the Results tab and its
AnnotationResults branch on format kind (gff/gtf/bed) -- without
adding …

- *(annotation)* make GenBank files eligible for annotation results

- *(annotation)* build feature tables and stats from GenBank files

- *(formats)* detect GenBank by extension and LOCUS header

- *(pipelines)* stream GenBank records without loading sequence

- *(pipelines)* emit GenBank features as parent rows plus segment children

- *(pipelines)* parse GenBank qualifier blocks with wrapped values

- *(pipelines)* parse GenBank locations without flattening joins

- *(formats)* add GENBANK format kind
Adds FormatKind.GENBANK and places it in FORMAT_FIELDS alongside the
other interval/annotation formats (GFF, GTF, BED). First of an 11-task
plan for GenBank annotation support (#294); every subsequent…

- *(ui)* say what SSH key BioFlow installs and retains when provisioning

- *(ui)* show each node's version and offer an update when it is behind

- *(api)* endpoints to update a compute node and poll its progress

- *(services)* update a node's image over SSH, verified by re-enrollment

- *(api)* install a managed SSH key during node provisioning
Adds an install_key phase between write_env and pull_image in
_provision_node: generates a keypair, appends it to the node's
authorized_keys over the existing connection, verifies it authenticates
ind…

- *(services)* generate and install a managed SSH key on compute nodes

- *(api)* expose the primary's image digest as the staleness reference

- *(api)* persist and expose the backend version each node reports
Enrollment now writes image_digest/version onto the Node document
instead of only Redis, which expires when a worker stops
heartbeating, and enumerate_nodes/list_nodes surface those fields
(plus an up…

- *(queue)* worker reports its image digest and version in the heartbeat

- *(models)* record SSH connection details and reported version on Node

- *(models)* add NodeUpdateTask for tracking node image updates
Mirrors NodeProvisionTask's structure but avoids the bare-string
Settings.indexes bug found in that model: task_id and node_id are both
declared via field-level Indexed(...) annotations only, with no
…

- add project-level operations with collapsible actions accordion
Add a 'Project actions' accordion above the filter box in the left panel
that lists project-scoped operations. Clicking an action opens its form
in the right panel via the existing ?sel= URL param sys…

- *(frontend)* add optional additional reads to the align dialog
The dialog keeps the one-file default -- the primary reads row and the
paired-end auto-mate are untouched -- and adds an "Add another read
file" picker beneath them. Each added file resolves its own m…

- *(pipelines)* record additional read sets in alignment provenance
An alignment run's inputs now list every additional set member under its
own role -- extra_reads for the set's own file, extra_mate for its mate
-- instead of the set being invisible to the activity v…

- *(pipelines)* concatenate additional read set mates into the R2 stream
A paired alignment with additional read sets previously threaded every
set's R1 into the combined R1 stream and dropped the mates entirely.
Each set's mate now concatenates into the R2 stream alongsid…

- *(pipelines)* resolve and validate paired additional read sets at launch
Each additional set is now a typed pair (R1 plus optional mate) resolved
the same way as the primary: explicit link, else suggest_mate. The set's
mate is auto-included, so adding one file of a pair is…

- *(api)* accept ordered additional alignment read sets
A file-level alignment launch can now carry additional read sets as a typed
top-level request field, a sibling of mate_object_id: each set is an R1 plus
an optional mate, and the whole run shares one …

- *(ci)* publish images and launcher bundles in one release
A v* tag now builds the launcher alongside the images and attaches the
bundles to the single GitHub release it creates (#335). A launcher failure
leaves the release published with images and no bundle…

- *(ops)* constrain launcher-only releases to exceed the app version
Keeps the launcher-never-below-app invariant that lets a combined cut
overwrite the launcher version safely (#335), and rejects pre-release
launcher-only cuts, which have no images to be staged agains…

- *(ops)* bump the launcher version as part of an app release
One release, one version (#335). A combined cut overwrites whatever the
launcher declared, so a launcher-only release's drift never survives the
next app release.

- *(docs)* update GitHub Pages landing page to v2
Replaces docs/index.html with a redesigned version of the landing
page (richer layout: numbered section nav, stats sidebar), still a
self-contained static bundle with no build step.

- *(ui)* give annotation files a results view

- *(ui)* browse annotation features with filters and a locus jump

- *(ui)* chart annotation feature types, density, and coverage

- *(frontend)* add the annotation results client and its types

- *(api)* serve the annotation feature table, filtered and paged
Adds launch_annotation_stats + _check_annotation_stats_callable to
pipeline_service.py, and three routes (POST /annotationstats, GET
/annotationstats/features/{id}, GET /annotationstats/children/{id})…

- *(pipelines)* compute annotation summaries and the feature index

- *(ops)* keep annotation results artifacts in a reaped directory

- *(pipelines)* store annotation features in a queryable SQLite index

- *(pipelines)* accumulate annotation aggregates in one bounded pass

- *(pipelines)* normalize GFF3, GTF, and BED lines to one feature shape

- *(docs)* add GitHub Pages landing page
Adds the project landing page as docs/index.html, a self-contained
static bundle (fonts and assets inlined) with no build step. Once
GitHub Pages is enabled to serve from main /docs, this becomes the
…





### 🐛 Bug Fixes

- *(launcher)* import vitest globals so tsc can compile the update tests
src/update-logic.test.ts used describe/it/expect without importing them,
unlike its three sibling test files. Vitest injects those globals at
runtime, so `npm run test` passed and nothing looked wrong…

- *(frontend)* keep the extract-sequence button disabled until it lands
extractMutation.isPending goes false the instant the job is queued, well
before the applier has actually created the reference -- so the button
re-enabled and invited a second click during that gap. T…

- *(pipelines)* classify the GenBank sequence launcher as excluded

- *(provenance)* classify extract_genbank_sequence in the step-verb registry
Registering the handler in Task 4 made test_every_registered_handler_is_classified
fail: _STEP_VERBS/_NO_NARRATIVE_STEP partition every registered handler, and this
one had no entry in either. Same re…

- *(pipelines)* classify the annotation stats and export handlers for provenance
Both were unclassified in _STEP_VERBS/_NO_NARRATIVE_STEP, the same
exhaustiveness gap fixed for the canvas registry in the prior commit.
run_annotation_stats writes facts onto the existing object with…

- *(pipelines)* exclude the annotation launchers from the canvas registry
launch_annotation_stats and launch_annotation_export both failed the
exhaustiveness test that requires every launch_* to be classified as a
canvas node type or explicitly excluded. Neither has a fixed…

- *(ui)* stop firing the export-count query on the Genes tab
The export control is only visible when view !== 'genes', but its backing
count query's enabled condition didn't include that check -- so it kept
refetching on every filter change while the user sat o…

- *(pipelines)* honor a locus jump's coordinate window in annotation export
The export preview and launch read the raw contig filter state and never
saw the Locus jump input's min/max at all, so exporting after jumping to a
specific region silently operated over the whole unb…

- *(api)* remove a duplicated export precondition and close a traversal gap
launch_annotation_export's own 'no computed results' check raised
ValidationError (422) while the identical condition is NotFoundError (404)
everywhere else, including the same route's own guard right…

- *(queue)* attach an exported annotation subset to its run
Every other ingest-creating applier in this file resolves the job's run and
records the new object as one of its outputs, so it appears in the
run/activity view. This applier ingested the subset but n…

- *(pipelines)* fail verification on an unindexed line, not just a mismatched one
write_subset's safety property -- verify every line against the index before
writing it -- previously held only when every line number in the caller's
set happened to already be a key the index record…

- *(nodes)* use AppError hierarchy in /nodes router instead of bare HTTPException
The /nodes router raised bare HTTPException at 11 call sites, producing
{"detail": ...} bodies that the frontend's client.ts cannot read — it
looks for body.message and falls back to a bare "409 Confl…

- *(nodes)* pass PEM string to import_private_key instead of StringIO
asyncssh.import_private_key expects a str or bytes, not a file-like
object. The io.StringIO wrapper caused an AttributeError at runtime
whenever key-based node provisioning was attempted, because
impo…

- *(pipelines)* classify annotation_stats only as an excluded launcher
This reverts commit bdfec631, which added a NodeTypeSpec for
annotation_stats. Two commits landed independently for #355, each a
complete fix on its own: bdfec631 added the spec, and a3f54da0 added th…

- *(pipelines)* classify launch_annotation_stats as an excluded launcher
Exhaustiveness check in test_node_types.py flagged it as unclassified.
It's a read-only Results computation over an existing GFF/GTF/BED/GenBank
object, auto-triggered at ingest and callable on demand…

- *(pipelines)* add a NodeTypeSpec for annotation_stats
launch_annotation_stats had no entry in NODE_TYPES or EXCLUDED_LAUNCHES,
failing test_every_launch_function_is_classified on every run. Its
sibling bam_stats/vcf_stats already had entries; this one wa…

- *(pipelines)* classify run_annotation_stats as a no-narrative-step handler
Same shape as run_bam_stats and run_vcf_stats: it writes statistics back
onto an existing object rather than producing a new one, so it belongs in
_NO_NARRATIVE_STEP alongside them. #257 added the han…

- *(ci)* allow workflow_dispatch to bypass the tag version-guard
The version-guard job's if condition only matched tag pushes, so a
workflow_dispatch (intended as a 'does this still build' smoke test)
skipped the guard and cascaded to skip the entire workflow. Allo…

- *(api)* add explicit strict= to the row-packing zip
CI's ruff check (B905) flags zip() without strict= -- a rule the local
worktree test run never invokes. features and rows are guaranteed the same
length here (rows is built from a list comprehension o…

- *(pipelines)* wire launch_annotation_stats to two-tier reference resolution
Task 10's browser verification found the track's "no coordinate axis" refusal
firing for every real annotation in the seeded dataset. Traced to
launch_annotation_stats still calling _reference_for_ann…

- *(pipelines)* require the reference role in accession-tier resolution
Task 7's real-database check (docs/superpowers/plans/2026-08-12-annotation-track-viewer.md)
found that resolve_annotation_reference's accession tier filtered candidates
on FormatKind.FASTA alone, not …

- *(pipelines)* enforce the reference-or-reason invariant on AnnotationReference

- *(pipelines)* prefer the reference role when resolving an annotation's genome
_reference_for_annotation took the first FASTA in derived_from with no role
check, so an annotation downloaded alongside its protein.faa could resolve to
the protein set. Matches reference_for_bam's p…

- *(pipelines)* treat a self-referencing parent as absent, not a silent drop
Correct the features_in_window docstring, which claimed two queries
when the code issues one SELECT (parents and children together) then
reassembles the tree in two Python passes.

- *(annotation)* update genbank_parse tests for the Feature.parents rename
The rebase onto main pulled in GenBank annotation support, which
constructed Feature with the pre-Task-1 parent kwarg and asserted
against the same singular field in its own tests. genbank_parse.py's
…

- *(annotation)* update owner-scoping test for Feature.parents rename
test_route_owner_scoping.py still constructed Feature with the old
`parent=None` kwarg, which Task 1 replaced with `parents: tuple[str, ...]`.
The test was failing collection/setup with a TypeError, m…

- *(annotation)* wrap views in TabPanel and use the server's echoed depth_cap

- *(annotation)* fix the third parent= kwarg site broken by the Feature.parents rename

- *(annotation)* make build_gene_table re-runnable and distinguish same-coordinate leaf types

- *(annotation)* dedupe child_count the same way descendant_count already is

- *(annotation)* keep every parent of a multi-parent GFF3 record, not just the first

- *(annotation)* keep GenBank out of the featureCounts annotation pool
_is_annotation gates more than annotation-Results eligibility -- it also
backs annotations_for_project/resolve_annotation, which feed
launch_quantify. Adding GENBANK there (Task 8's original change) m…

- *(annotation)* harden the GenBank contig-lengths ordering dependency
Two issues from Task 7's code-quality review, both about the same root
cause: nothing enforced that _genbank_rows' generator was fully drained
before the handler read its side-channel facts dict, so a…

- *(formats)* tighten GenBank sniff to a literal space, cover compressibility

- *(pipelines)* stop misreading a short continuation line as a new feature
iter_features's key-detection check only required a line to reach column 6
before testing whether column 22 held non-whitespace. A wrapped qualifier's
tail line shorter than column 22 has an empty sli…

- *(ui)* surface update-start failures and clear stale progress panels across nodes
startUpdate had no onError handler, so a 409/404 from POST /nodes/{id}/update
rejected silently with the confirmation dialog just re-enabling its button.
Also, clicking Update on a new node left a pri…

- *(services)* make run_update's own error handling failure-tolerant
The initial task lookup and _fail's own task.save() were unguarded, so a
transient Mongo hiccup during either could raise out of run_update -- a
fire-and-forget background task with no caller to catch…

- *(services)* log at node_ssh's failure points, not just raise

- *(api)* run current-version's docker probe off the event loop
_own_image_digest shells out to docker twice via subprocess.run, up to
~20s worst case, blocking the single event loop thread and stalling
every other request the primary serves (node heartbeats, job …

- *(queue)* resolve image digest through the container's own image id, not a hardcoded tag

- *(models)* use the repo's timezone-aware utcnow() in NodeUpdateTask
datetime.utcnow() is deprecated and timezone-naive; match the local
utcnow() pattern node.py already uses for this purpose.

- *(api)* register NodeProvisionTask so provisioning can write its task document
Registering the model in ALL_MODELS activated a previously dead code
path in db.index_reconcile: NodeProvisionTask.Settings.indexes held a
bare field-name string ("task_id") instead of an IndexModel, …

- *(frontend)* clarify the per-contig coverage loading state
The table showed a bare "Loading…" whenever the query had no data yet, so a
failed request left "Loading…" on screen indefinitely. Show the pending
state only while the request is in flight and a dist…

- *(frontend)* align coverage-chart titles on a shared baseline
The Insert size and Mapping quality cards sit in a flex row, but each still
carried the .section class, whose margin-top offsets every card after the
first. That pushed "Mapping quality" below and rig…

- *(agent)* await the project-context services the system prompt depends on
#290's project-context injection made _system_prompt and
_build_project_context call two async services, search_service.count_by_kind
and project_service.recent_jobs, without await. Every /ask and /re…

- *(metadata)* classify molecule_type and library_source as open/closed
Two new ENUM fields (2e073b8f) had no open/closed classification, tripping
test_schemas_open_vocabulary's tripwire: 17 distinct ENUM FieldDefs, 15
accounted for. molecule_type (DNA/RNA/Other) is a voc…

- *(api)* rewrite Docker service hostnames in URLs handed to provisioned nodes
_rewrite_host built its pattern with an f-string containing `:\\d+`, which in
an f-string is a literal backslash followed by "d" rather than the digit class
`\d`. So `mongodb://mongo:27017/db` never m…

- *(models)* register NodeProvisionTask so node provisioning can write
NodeProvisionTask was never added to ALL_MODELS, so init_beanie never
created its collection. Node provisioning from Settings -> Nodes failed at
runtime: _provision_node writes a NodeProvisionTask doc…

- *(ops)* fail a release tag whose launcher version files disagree
Tauri reads tauri.conf.json, not Cargo.toml, so a stale one publishes
bundles labelled with the wrong version while every other check passes.
That is what shipped as launcher-v0.2.0 (#335).

- *(launcher)* bump tauri.conf.json so bundles carry the release version
Tauri reads tauri.conf.json's version key in preference to Cargo.toml, and
bump_version.py never wrote it -- so launcher-v0.1.0 and launcher-v0.2.0 both
published bundles named 0.1.0. Pre-releases get…

- *(pipelines)* drop an unused Feature import in annotation_db
build_annotation_db's rows parameter is untyped, so Feature never
appears as an annotation anywhere in this file -- caught by CI's
ruff check, which flagged F401 on a pass the local pre-push check
mis…

- *(frontend)* drop a dead AskQuestionResponse import
Left behind by rebasing onto main: the conflict in client.ts's import
block had AskQuestionResponse on one side, and resolving it kept the
name without checking it still exists on the branch being reb…

- *(pipelines)* dispatch run_annotation_stats via THREAD, not SUBPROCESS
Pure in-process parsing and SQLite writes, no binary to spawn or kill
via process group -- same shape as run_transcript_qc, which already
uses THREAD for the identical reason. Also documents the attri…

- *(pipelines)* stop exon/CDS rows colliding with their transcript's feature_id
parse_gtf_line's else-branch set feature_id equal to parent
(transcript_id or gene_id) for every exon/CDS/UTR row, so siblings
under the same transcript were indistinguishable from each other and
from…

- *(ci)* move workflows from self-hosted to GitHub-hosted runners
The repo going public unlocks free hosted ubuntu-24.04-arm and macos-latest
runners, removing the constraint that put arm64 Docker builds and macOS
launcher signing on self-hosted machines. publish-im…




















## [0.4.0] - 2026-08-12




### 🚀 Features

- *(ui)* tooltip on reads icons shows the platform (PacBio, Nanopore, Illumina)
When hovering over a reads file icon in the project explorer, the
tooltip now includes the platform name alongside the format — e.g.
'fastq file (PacBio)' instead of just 'fastq file'. Falls back to t…

- *(launcher)* add the Update button's affordance rule
Three states rather than a boolean: hidden, available, and suppressed
with a reason. Suppressed carries its own explanation so the button can
say why it is inert instead of vanishing.

- *(launcher)* add the rule for which modes have a checkable tag
Release tracks a moving latest and is the only mode where a digest
check answers a real question. Developer builds :local images with no
registry counterpart, and alpha/beta pin an immutable stage tag…

- *(ops)* list and prune orphaned worktree stacks
`worktree-up.sh --down` only works from inside the worktree that owns the
stack, which is the structural reason orphans accumulate: a worktree gets
deleted when its branch merges, and once the directo…

- *(agent)* merge ASK and Agent into unified AI Agent panel
Merge the Project Q&A (ASK) and AI Agent features into a single unified
Agent panel. Remove all ASK-specific backend infrastructure.

- *(launcher)* show which other BioFlow stacks are running
The launcher sees only its own Compose project, since every action runs
`--project-directory <install_dir>`. That is correct -- `biopipe` is its
boundary -- but it meant a machine with four orphaned w…

- *(icons)* draw per-platform reads marks for Illumina, PacBio and Nanopore
Adds six reads_{raw,trimmed}_{illumina,pacbio,nanopore} glyphs to the icon
set -- the same reads_raw/reads_trimmed mark with one added ink cue per
instrument (staggered mates, looped CCS gap, threaded…

- *(launcher)* add version mode selector to Settings dialog (#288)
Adds a version dropdown to the Settings dialog with four modes:
Release (stable, :latest tag), Alpha, Beta (pre-release tags from
GHCR), and Developer (local build). Selecting a mode and clicking
Appl…

- *(ui)* show per-node queue depth in the nodes table

- *(frontend)* type per-node queue statistics

- *(api)* break system load down by node on /system/load

- *(api)* report per-node queue depth and stale reservations on /nodes

- *(queue)* surface ready queues belonging to no enrolled node

- *(queue)* read per-node queue depth and reservations in one pipeline

- *(quality)* show the summary at the top of the Quality tab (#292)
The AI summary rendered below the charts and fact tables, so the overall
verdict on a file came only after the detail that justifies it. Move it
above the charts: the summary is the high-level answer,…

- *(ui)* add node provisioning form with progress and result display

- *(main)* register orphan provisioning cleanup on startup

- *(nodes)* add SSH provisioning endpoints and asyncssh executor

- *(api)* add TypeScript types and client methods for node provisioning

- *(config)* add PRIMARY_HOSTNAME setting for node provisioning

- *(history)* style the summary rail from the reference

- *(history)* structure the summary rail as citable prose

- *(nodes)* add child-node enrollment with shared-secret auth (#245)

- *(pipelines)* add blob-transfer endpoints between primary and child nodes
Adds GET /objects/blob/{digest} and PUT /objects/blob/{digest} endpoints
so compute nodes can pull/push content-addressed blobs to the primary.

- *(api)* add /nodes/connection-details endpoint for launcher auto-discovery
Returns externally-routable Mongo/Redis URLs (Docker service names
rewritten to the request host) plus a suggested node name.

- *(launcher)* add compute-node install mode with SSH remote support
Adds a mode choice on first launch: full stack install (existing) or
compute-node install (new). The node flow:

- *(ci)* build the frontend prod image on every PR into main
No workflow verified that the container images actually build before
landing on main -- publish-images.yml builds the same targets but
only on push to main/tags, by which point a broken build has alre…

- *(frontend)* add repeat-density and gene-density rings to Circos plot
The meryl pipeline (#213/#220) now produces repeat_density facts and the
Bakta pipeline (#214) produces gene_density facts, both in the same
per-window shape as gc_tracks. This adds the frontend rende…

- *(frontend)* wire node selector into non-dialog launch sites
Add NodeSelector to inline action buttons that PR #211 missed:
- DetailPanel: Run QC button
- BamResults: Compute results
- VariantResults: VCF Stats + Variant Summary (via AiSummary)
- ExpressionResu…

- *(pipelines)* add Bakta bacterial genome annotation
Adds a Bakta-powered genome annotation pipeline for bacterial and archaeal
assemblies. The pipeline:

- *(pipelines)* add meryl k-mer spectra and repeat density analysis (#220)
* docs(spec): meryl-based k-mer repeat density and frequency spectra design

- *(frontend)* add node selector to all launch dialogs
Wire NodeSelector into AlignDialog, AssembleDialog, VariantDialog,
QuantifyDialog, ScaffoldDialog, CompletenessDialog, and
DifferentialExpressionDialog. Every dialog that launches a pipeline
job now s…

- *(frontend)* add node selector to job launch dialogs
Adds a NodeSelector dropdown to PipelineSuggestions and TrimDialog
that lets the user target a job to a specific worker node. Selecting
a node appends ?target_node=xxx to the launch endpoint URL.

- *(frontend)* add nodes settings page and child-node compose file
- SettingsNodes.tsx: table showing connected nodes with status, worker
  count, running jobs, and per-node resource reservations. Fetches
  from GET /api/v1/nodes every 10s.
- SettingsNav: new "Nodes"…

- *(frontend)* plot gene body coverage and read distribution

- *(api)* launch RNA-seq transcript QC against a chosen annotation, gated by applicability
Also excludes pipeline_service.launch_transcript_qc from the pipeline
canvas's launch-function exhaustiveness check: it's an on-demand analysis
action over an existing BAM, not a graph-wireable step, …

- *(queue)* compute RNA-seq gene body coverage and feature distribution
Adds the run_transcript_qc handler, wiring Tasks 1-2's pure GTF-parsing
and BAM-classification functions into a real queue job that merges gene
body coverage and feature distribution facts onto the de…

- *(services)* infer whether RNA-seq QC applies to a BAM

- *(pipelines)* accumulate gene body coverage and feature distribution

- *(pipelines)* parse a GTF into per-gene representative transcripts

- *(pipelines)* compare an assembly to a reference as a synteny dot plot
* feat(pipelines): add minimap2 synteny alignment runner

- *(queue)* add per-node queues and worker node identity
Adds WORKER_NODE_ID config, per-node Redis ready queues
(bp:q:ready:{node}), and per-node concurrency counters
(bp:conc:*:{node}) so a second machine running BioFlow's worker
can claim and run jobs ag…

- *(pipelines)* add GC content and GC skew rings for finished genomes
A new Circos-style circular visualization for polished reference genomes:
contigs around the perimeter, GC content and GC skew as concentric rings,
so compositional anomalies and a bacterial chromosom…

- *(frontend)* plot the depth distribution and per-contig depth

- *(frontend)* type the depth histogram facts

- *(pipelines)* record the depth histogram alongside the coverage bins

- *(pipelines)* accumulate the depth histogram during bin_depth's existing pass

- *(pipelines)* count reference positions by depth into an adaptive histogram

- *(frontend)* show the assembly graph on a GFA object
Above the fact table: the shape is the finding, and the segment and link
counts only quantify it.

- *(frontend)* add assembly graph viewer component
Node area scales with segment length (square-root, so a 10x longer contig
does not swamp the canvas). Layout is capped at 400 cose iterations: the
shape a reader needs is legible well before convergen…

- *(backend)* retain assembly graph topology, not just counts
_parse_gfa already walked S and L lines and kept only the counts. It now
also emits gfa_segments and gfa_links, which is what the graph viewer
draws. Link orientation is kept: it says which end of a s…

- *(frontend)* show the Nx/NGx curve on the Reference Quality tab
NGx appears only when an expected genome size exists, which is only for
assemblies BioFlow built from reads with one supplied. Uploaded and
NCBI-downloaded assemblies get the Nx curve alone, with no e…

- *(frontend)* add Nx/NGx contiguity curve component
Log Y axis: contig lengths span orders of magnitude, and on a linear axis
every real assembly is a cliff against the axis.

- *(backend)* store the Nx contiguity curve alongside N50
A hundred (percent, length) points computed in the same walk that already
produces N50 and N90, so the three cannot disagree. Downsampled rather
than raw: a fragmented draft is hundreds of thousands o…

- *(pipelines)* add bwa-mem2 organism-type presets with advanced mode
feat(pipelines): add bwa-mem2 organism-type presets with advanced mode

- *(metrics)* list each job type's recent runs beside the summary
The Metrics page showed BioFlow's splash screen down its right side:
/metrics was missing from App.tsx's singleColumn list, so the shared
DetailPanel mounted alongside it and, with no ?sel= param ever…

- *(metrics)* expose individual job runs, failures included
GET /metrics returns one aggregate row per job type, so the Metrics page
had no way to show what any individual run did. Adds runs_for_type() and
recent_runs_by_type() behind GET /jobs/metrics/runs, w…

- add chunked launch branch and AlignDialog toggle
- launch_alignment now detects chunked=true in params, invokes bucket
  planner, writes per-bucket FASTAs, and enqueues align_reads_chunked
- AlignDialog shows Chunked toggle when envelope reports chu…

- add chunked alignment API integration points
- Align-envelope endpoint accepts chunked=true query param
- Returns chunking.supported and total_sequences from .fai
- Added _parse_fai helper for reading FASTA index files
- Core modules: bucket pla…

- add chunked alignment orchestrator, merge handler, and applier
- align_reads_chunked (ASYNC): dispatches per-bucket sub-jobs, polls completion
- merge_chunked_buckets (SUBPROCESS): samtools merge + sort + flagstat
- apply_chunked_alignment: creates BAM DataObject…

- add memory-aware bucket planner with FASTA writer and tests

- add chunking_supported field to AlignerSpec
STAR and Winnowmap are gated out — STAR's index is tied to the exact
reference FASTA, Winnowmap requires whole-reference meryl prep.

- *(pipelines)* add bwa-mem2 organism-type presets with advanced mode
Introduce a preset system for bwa-mem2 that bundles biology-tuning
parameters by organism class (Bacteria/Virus/Yeast, Large/Repetitive,
Human/Eukaryote), plus an advanced mode for full manual control…





### 🐛 Bug Fixes

- *(launcher)* explain the inert Update button instead of nagging in dev mode
Developer and pinned alpha/beta modes now render Update disabled with
the reason underneath -- 'use Rebuild in Settings' for a local build,
the pinned tag for a stage -- rather than showing a live Upd…

- *(launcher)* stop checking for updates against a tag the stack is not on
check_for_update read no settings and hard-coded latest, so developer
mode compared GHCR's published latest against locally built :local
images and pinned alpha/beta stages were compared against a tag…

- remove unused publish_event import and fix import sorting

- *(ops)* move the latest image tag on every release, not never

- *(launcher)* refuse to adopt a git worktree as the install directory
`discover_running_project_dir` returned a running `biopipe` project's
`com.docker.compose.project.working_dir` label with no check on what that
directory was, and `install_dir_str_blocking` cached it …

- *(frontend)* restore the version key dropped from package.json
`make release` aborted with:

- *(ops)* stop worktree stacks publishing mongo and redis on the host
A worktree stack could not run alongside the main stack. `docker compose up`
failed with "Bind for 0.0.0.0:27017 failed: port is already allocated", which
also broke run-worktree-tests.sh -- that scri…

- *(api)* replace deprecated Motor with the pymongo async driver
The API could not start: init_beanie raised "MotorDatabase object is not
callable" and the container never became healthy, so every service that
depends on it refused to come up.

- *(settings)* use standard styling for Save buttons (#305)
The Settings Save buttons (Resources, General, provider form) rendered as
bare browser-default buttons while the established Settings controls use
the .btn treatment -- MCP Copy Configuration is the r…

- *(actions)* render the trimmed-file warning without HTML entities (#304)
The trimmed-read suggestion card hardcoded '&amp;' in its title. The
frontend renders card titles as JSX text, which escapes entities again --
so users saw the literal '&amp;' instead of '&'. Return t…

- *(quality)* remove target-node selection from AI summaries (#303)
The Quality tab showed a target-node dropdown when generating a summary,
but summaries are produced by the configured AI provider, not executed on
a compute node -- the launch endpoints (summary, de-s…

- *(actions)* show Paired-end controls only for read files (#302)
The shared isReads conditional excluded only reference and alignment
roles, so every other non-read object type -- de_results, counts,
variants, protein, transcript, annotation, assembly_graph -- stil…

- *(actions)* make Differential expression the primary expression action (#301)
For expression files, Differential expression is the first action in the
sequence, but it rendered as a plain btn while the leading action in
comparable groups (Preprocess for reads) carries the blue …

- *(alignment)* match the Download TSV button to result actions (#300)
The alignment results tab's Download TSV was a bare text link
(btn-text), while every other action on the page -- Compute results /
Recompute results -- is a bordered btn. Switch it to btn, which the …

- *(alignment)* place coverage graphs side by side (#293)
The Fraction-of-reference cumulative coverage chart sat in the
side-by-side flex row with the depth histogram, but Mean depth per contig
was stacked in its own full-width section below. Move ContigDep…

- *(settings)* recover storage and exposure before Apply can wipe them (#287)
* fix(settings): recover storage and exposure before Apply can wipe them

- *(lint)* resolve ruff errors in provisioning code

- *(actions)* hide the target-node dropdown when only one node exists (#286)
Workers that heartbeat without a node_id are grouped by the backend into
a catch-all bucket named 'unknown'. That pseudo-node inflated the node
count, so a single real node plus the bucket (2 entries)…

- *(actions)* size the target-node dropdown to its contents (#283)
The NodeSelector renders with the 'settings-field inline' modifier, but no
CSS rule existed for it, so the select inherited width:100% and stretched
across the full page width. Add the missing rule so…

- *(suggestions)* restore trimmed-read card syntax
The main-side conflict resolution contained literal newline escapes in the card description, making the module invalid Python. Restore the intended implicit string concatenation while rebasing the CI …

- *(ci)* make backend smoke lint baseline clean
The new backend-smoke job checks all backend application sources, exposing existing style violations before the smoke tests could run. Clean the remaining Ruff findings so the workflow can enforce the…

- *(frontend)* pad the alignment preset select on Safari
The native select was excluded from the shared field styling, leaving Safari's focus treatment tight against the control edge. Match the dialog input padding and use the existing in-field accent focus…

- *(history)* always show the Provenance hint on the History tab
The hint was gated on obj.facts.sra_accession being a string, a field
that has nothing to do with whether provenance data exists. This caused
the Provenance hint to appear only for objects with an SRA…

- *(frontend)* retain sequence chart hover outside plots

- *(frontend)* render the missing node selector on DE results
ExpressionResults.tsx declared targetNode/setTargetNode and read
targetNode when launching the DE AI summary, but never rendered a
<NodeSelector> to let the user set it -- the setter was unreachable,
…

- *(frontend)* remove stray tokens breaking the web container build
client.ts, DifferentialExpressionDialog.tsx, VariantDialog.tsx,
QuantifyDialog.tsx, and ScaffoldDialog.tsx each carried a leftover
fragment from a partial targetNode/NodeSelector merge: stray
`useStat…

- *(pipelines)* pass threads= through governor pre-flight memory estimate calls (#234)
Thread-count segmentation (#8) wired threads through worker.py:_eta_model_ms
and jobs.py:get_job, but the three memory_estimate.resolve() calls inside
pipeline_service.py (declared_align_mem_mb, launc…

- *(frontend)* add missing gtf entry to ACCEPT_CHOICES (#233)
ACCEPT_CHOICES had a gff:annotation entry but no gtf:annotation entry.
FormatKind.GTF is a distinct enum value from FormatKind.GFF on the backend,
and portAccepts matches on format exactly, so a gff-t…

- *(pipelines)* orchestrator port checks use ports_for(node) instead of static spec (#232)
_is_multi_port and _advance both read spec.inputs directly instead of
calling ports_for(node), the tool-aware resolver added for STAR's annotation
port. Safe today only because the single tool-added p…

- *(pipelines)* alignment memory estimate sums extra_reads bytes (#231)
The pre-flight memory check and the queue reservation in launch_alignment
both used only obj.size to estimate input bytes, ignoring any extra_reads
files. Since the align_reads handler concatenates ev…

- *(queue)* harden gc_blobs, verify_files, and reap_uploads (#210)
Six hardening fixes from a second-pass code audit:

- *(pipelines)* drop launch_completeness from EXCLUDED_LAUNCHES
It was already registered in NODE_TYPES (95b2b61) as a canvas node
with outputs=() and run_kind=None, deliberately modeling "facts
merged onto an existing assembly, no output port to wire." A later
co…

- *(queue)* reap transcript_qc/bam_stats scratch dirs, fix inaccurate index error, test _is_gzip
Three follow-ups from reviewing the gzip/BAM-index fix:

- *(queue)* read gzip-compressed GTFs and symlink the BAM index for transcript QC
Two bugs found only by running run_transcript_qc against a real indexed BAM
with a real GTF -- neither is reachable from a unit test, since every
fixture feeds pre-parsed strings or a bare in-memory B…

- *(frontend)* render transcript QC independent of bam_stats
TranscriptQc was nested inside the hasResults conditional block, which is
gated on bam_stats_status -- an unrelated computation. A user with a bare,
unindexed RNA-seq BAM who hadn't yet run "Compute r…

- *(frontend)* default the GTF selection reactively, reuse BamStatsFacts instead of a local copy

- *(queue)* use thread mode for the in-process handler, floor per-contig sampling at one read

- *(pipelines)* make interval coverage lookup correct for any span, not just short ones

- *(pipelines)* match GTF attributes by exact key, sort exons on construction

- *(backend)* strided sampling in alignment stats for indexed BAMs
Previously `alignment_stats` capped sampling at the first 200,000 reads,
which for coordinate-sorted BAMs all come from the beginning of the first
contig. Every library-wide statistic (GC content, MAP…

- *(pipelines)* fail loudly on missing bucket sequences and malformed .fai lines (#200)
1. write_bucket_fastas silently skipped sequences not found in the
   reference FASTA. A .fai/FASTA mismatch produced an incomplete per-bucket
   file, the alignment ran against a subset of the refere…

- *(pipelines)* refuse chunked plans that exceed the memory budget (#199)
pack_buckets had two defects that produced plans it knew would OOM:

- *(pipelines)* make chunked alignment run end to end (#198)
Seven defects, each independently fatal on the first real run:

- *(frontend)* label History tab paragraph section "Summarize" before generating
The pre-generation state kept the "Methods paragraph" header even though no
paragraph existed yet, which read oddly next to the invite text and button.
Match the mockup: "Summarize" until a paragraph …

- *(frontend)* fix bwa-mem2 preset selector type errors on main
Two independent bugs, both pre-existing on main and unrelated to this
branch, surfaced by rebasing onto its latest bwa-mem2 organism-preset
commit:

- *(frontend)* add missing chunked-alignment fields to API types
AlignDialog.tsx references AlignParams.chunked and
AlignEnvelope.chunking, but neither was ever added to types.ts when the
chunked-alignment feature landed -- npm run build (tsc -b && vite build)
has …

- *(frontend)* wrap AssemblyGraph in .section for column-break protection
facts-columns uses CSS multi-column layout, and only children rooted in
.section get break-inside: avoid. Every other child of this container
(FactsTable's groups, QcReport, TrimReport) already has th…

- *(frontend)* guard AssemblyGraph's layout effect against array-identity churn
useEffect was keyed on the segments/links arrays themselves. A caller that
constructs them fresh each render (e.g. mapping over fetched data inline
rather than memoizing) would trigger a full cytoscap…

- *(backend)* restore TestFastaContiguity class boundary
TestGfaParsing's class body ran on past its own 8 tests and absorbed 16
FASTA-contiguity tests that belong to TestFastaContiguity, which lost
them via a missing class header. Restored as one merged
Te…

- *(frontend)* stop NGx curve from truncating when assembly exceeds expected genome size
ngxPoints() dropped every point past gx=100, which happens whenever the
assembly is larger than the expected genome (a plausible case: retained
duplicated haplotypes, or an underestimated genome size)…

- *(frontend)* align two-column fact group headers instead of balancing by height
column-fill's default "balance" mode splits total content evenly between
the two columns and starts column two wherever that height lands, not at
the top of a group. A short group landing first in col…

- *(metrics)* key run rows by position, not job_id or timestamp
Neither candidate is unique in a real library: job_id is null on rows
recorded before it was stored, and batch-recorded runs share a finished_at
to the second -- eight ingest_headers rows sit on one t…

- *(backend)* import field so ToolMeta can construct, unbreaking the API
`ToolMeta.recommendations` used `field(default_factory=dict, repr=False)`
but `dataclasses.field` was never imported, so importing `tools` raised
NameError at module load -- the API container failed t…























## [0.3.1-alpha] - 2026-08-12




### 🚀 Features

- *(launcher)* add version mode selector to Settings dialog (#288)
Adds a version dropdown to the Settings dialog with four modes:
Release (stable, :latest tag), Alpha, Beta (pre-release tags from
GHCR), and Developer (local build). Selecting a mode and clicking
Appl…

- *(ui)* show per-node queue depth in the nodes table

- *(frontend)* type per-node queue statistics

- *(api)* break system load down by node on /system/load

- *(api)* report per-node queue depth and stale reservations on /nodes

- *(queue)* surface ready queues belonging to no enrolled node

- *(queue)* read per-node queue depth and reservations in one pipeline

- *(quality)* show the summary at the top of the Quality tab (#292)
The AI summary rendered below the charts and fact tables, so the overall
verdict on a file came only after the detail that justifies it. Move it
above the charts: the summary is the high-level answer,…

- *(ui)* add node provisioning form with progress and result display

- *(main)* register orphan provisioning cleanup on startup

- *(nodes)* add SSH provisioning endpoints and asyncssh executor

- *(api)* add TypeScript types and client methods for node provisioning

- *(config)* add PRIMARY_HOSTNAME setting for node provisioning

- *(history)* style the summary rail from the reference

- *(history)* structure the summary rail as citable prose

- *(nodes)* add child-node enrollment with shared-secret auth (#245)

- *(pipelines)* add blob-transfer endpoints between primary and child nodes
Adds GET /objects/blob/{digest} and PUT /objects/blob/{digest} endpoints
so compute nodes can pull/push content-addressed blobs to the primary.

- *(api)* add /nodes/connection-details endpoint for launcher auto-discovery
Returns externally-routable Mongo/Redis URLs (Docker service names
rewritten to the request host) plus a suggested node name.

- *(launcher)* add compute-node install mode with SSH remote support
Adds a mode choice on first launch: full stack install (existing) or
compute-node install (new). The node flow:

- *(ci)* build the frontend prod image on every PR into main
No workflow verified that the container images actually build before
landing on main -- publish-images.yml builds the same targets but
only on push to main/tags, by which point a broken build has alre…

- *(frontend)* add repeat-density and gene-density rings to Circos plot
The meryl pipeline (#213/#220) now produces repeat_density facts and the
Bakta pipeline (#214) produces gene_density facts, both in the same
per-window shape as gc_tracks. This adds the frontend rende…

- *(frontend)* wire node selector into non-dialog launch sites
Add NodeSelector to inline action buttons that PR #211 missed:
- DetailPanel: Run QC button
- BamResults: Compute results
- VariantResults: VCF Stats + Variant Summary (via AiSummary)
- ExpressionResu…

- *(pipelines)* add Bakta bacterial genome annotation
Adds a Bakta-powered genome annotation pipeline for bacterial and archaeal
assemblies. The pipeline:

- *(pipelines)* add meryl k-mer spectra and repeat density analysis (#220)
* docs(spec): meryl-based k-mer repeat density and frequency spectra design

- *(frontend)* add node selector to all launch dialogs
Wire NodeSelector into AlignDialog, AssembleDialog, VariantDialog,
QuantifyDialog, ScaffoldDialog, CompletenessDialog, and
DifferentialExpressionDialog. Every dialog that launches a pipeline
job now s…

- *(frontend)* add node selector to job launch dialogs
Adds a NodeSelector dropdown to PipelineSuggestions and TrimDialog
that lets the user target a job to a specific worker node. Selecting
a node appends ?target_node=xxx to the launch endpoint URL.

- *(frontend)* add nodes settings page and child-node compose file
- SettingsNodes.tsx: table showing connected nodes with status, worker
  count, running jobs, and per-node resource reservations. Fetches
  from GET /api/v1/nodes every 10s.
- SettingsNav: new "Nodes"…

- *(frontend)* plot gene body coverage and read distribution

- *(api)* launch RNA-seq transcript QC against a chosen annotation, gated by applicability
Also excludes pipeline_service.launch_transcript_qc from the pipeline
canvas's launch-function exhaustiveness check: it's an on-demand analysis
action over an existing BAM, not a graph-wireable step, …

- *(queue)* compute RNA-seq gene body coverage and feature distribution
Adds the run_transcript_qc handler, wiring Tasks 1-2's pure GTF-parsing
and BAM-classification functions into a real queue job that merges gene
body coverage and feature distribution facts onto the de…

- *(services)* infer whether RNA-seq QC applies to a BAM

- *(pipelines)* accumulate gene body coverage and feature distribution

- *(pipelines)* parse a GTF into per-gene representative transcripts

- *(pipelines)* compare an assembly to a reference as a synteny dot plot
* feat(pipelines): add minimap2 synteny alignment runner

- *(queue)* add per-node queues and worker node identity
Adds WORKER_NODE_ID config, per-node Redis ready queues
(bp:q:ready:{node}), and per-node concurrency counters
(bp:conc:*:{node}) so a second machine running BioFlow's worker
can claim and run jobs ag…

- *(pipelines)* add GC content and GC skew rings for finished genomes
A new Circos-style circular visualization for polished reference genomes:
contigs around the perimeter, GC content and GC skew as concentric rings,
so compositional anomalies and a bacterial chromosom…

- *(frontend)* plot the depth distribution and per-contig depth

- *(frontend)* type the depth histogram facts

- *(pipelines)* record the depth histogram alongside the coverage bins

- *(pipelines)* accumulate the depth histogram during bin_depth's existing pass

- *(pipelines)* count reference positions by depth into an adaptive histogram

- *(frontend)* show the assembly graph on a GFA object
Above the fact table: the shape is the finding, and the segment and link
counts only quantify it.

- *(frontend)* add assembly graph viewer component
Node area scales with segment length (square-root, so a 10x longer contig
does not swamp the canvas). Layout is capped at 400 cose iterations: the
shape a reader needs is legible well before convergen…

- *(backend)* retain assembly graph topology, not just counts
_parse_gfa already walked S and L lines and kept only the counts. It now
also emits gfa_segments and gfa_links, which is what the graph viewer
draws. Link orientation is kept: it says which end of a s…

- *(frontend)* show the Nx/NGx curve on the Reference Quality tab
NGx appears only when an expected genome size exists, which is only for
assemblies BioFlow built from reads with one supplied. Uploaded and
NCBI-downloaded assemblies get the Nx curve alone, with no e…

- *(frontend)* add Nx/NGx contiguity curve component
Log Y axis: contig lengths span orders of magnitude, and on a linear axis
every real assembly is a cliff against the axis.

- *(backend)* store the Nx contiguity curve alongside N50
A hundred (percent, length) points computed in the same walk that already
produces N50 and N90, so the three cannot disagree. Downsampled rather
than raw: a fragmented draft is hundreds of thousands o…

- *(pipelines)* add bwa-mem2 organism-type presets with advanced mode
feat(pipelines): add bwa-mem2 organism-type presets with advanced mode

- *(metrics)* list each job type's recent runs beside the summary
The Metrics page showed BioFlow's splash screen down its right side:
/metrics was missing from App.tsx's singleColumn list, so the shared
DetailPanel mounted alongside it and, with no ?sel= param ever…

- *(metrics)* expose individual job runs, failures included
GET /metrics returns one aggregate row per job type, so the Metrics page
had no way to show what any individual run did. Adds runs_for_type() and
recent_runs_by_type() behind GET /jobs/metrics/runs, w…

- add chunked launch branch and AlignDialog toggle
- launch_alignment now detects chunked=true in params, invokes bucket
  planner, writes per-bucket FASTAs, and enqueues align_reads_chunked
- AlignDialog shows Chunked toggle when envelope reports chu…

- add chunked alignment API integration points
- Align-envelope endpoint accepts chunked=true query param
- Returns chunking.supported and total_sequences from .fai
- Added _parse_fai helper for reading FASTA index files
- Core modules: bucket pla…

- add chunked alignment orchestrator, merge handler, and applier
- align_reads_chunked (ASYNC): dispatches per-bucket sub-jobs, polls completion
- merge_chunked_buckets (SUBPROCESS): samtools merge + sort + flagstat
- apply_chunked_alignment: creates BAM DataObject…

- add memory-aware bucket planner with FASTA writer and tests

- add chunking_supported field to AlignerSpec
STAR and Winnowmap are gated out — STAR's index is tied to the exact
reference FASTA, Winnowmap requires whole-reference meryl prep.

- *(pipelines)* add bwa-mem2 organism-type presets with advanced mode
Introduce a preset system for bwa-mem2 that bundles biology-tuning
parameters by organism class (Bacteria/Virus/Yeast, Large/Repetitive,
Human/Eukaryote), plus an advanced mode for full manual control…





### 🐛 Bug Fixes

- *(frontend)* restore the version key dropped from package.json
`make release` aborted with:

- *(ops)* stop worktree stacks publishing mongo and redis on the host
A worktree stack could not run alongside the main stack. `docker compose up`
failed with "Bind for 0.0.0.0:27017 failed: port is already allocated", which
also broke run-worktree-tests.sh -- that scri…

- *(api)* replace deprecated Motor with the pymongo async driver
The API could not start: init_beanie raised "MotorDatabase object is not
callable" and the container never became healthy, so every service that
depends on it refused to come up.

- *(settings)* use standard styling for Save buttons (#305)
The Settings Save buttons (Resources, General, provider form) rendered as
bare browser-default buttons while the established Settings controls use
the .btn treatment -- MCP Copy Configuration is the r…

- *(actions)* render the trimmed-file warning without HTML entities (#304)
The trimmed-read suggestion card hardcoded '&amp;' in its title. The
frontend renders card titles as JSX text, which escapes entities again --
so users saw the literal '&amp;' instead of '&'. Return t…

- *(quality)* remove target-node selection from AI summaries (#303)
The Quality tab showed a target-node dropdown when generating a summary,
but summaries are produced by the configured AI provider, not executed on
a compute node -- the launch endpoints (summary, de-s…

- *(actions)* show Paired-end controls only for read files (#302)
The shared isReads conditional excluded only reference and alignment
roles, so every other non-read object type -- de_results, counts,
variants, protein, transcript, annotation, assembly_graph -- stil…

- *(actions)* make Differential expression the primary expression action (#301)
For expression files, Differential expression is the first action in the
sequence, but it rendered as a plain btn while the leading action in
comparable groups (Preprocess for reads) carries the blue …

- *(alignment)* match the Download TSV button to result actions (#300)
The alignment results tab's Download TSV was a bare text link
(btn-text), while every other action on the page -- Compute results /
Recompute results -- is a bordered btn. Switch it to btn, which the …

- *(alignment)* place coverage graphs side by side (#293)
The Fraction-of-reference cumulative coverage chart sat in the
side-by-side flex row with the depth histogram, but Mean depth per contig
was stacked in its own full-width section below. Move ContigDep…

- *(settings)* recover storage and exposure before Apply can wipe them (#287)
* fix(settings): recover storage and exposure before Apply can wipe them

- *(lint)* resolve ruff errors in provisioning code

- *(actions)* hide the target-node dropdown when only one node exists (#286)
Workers that heartbeat without a node_id are grouped by the backend into
a catch-all bucket named 'unknown'. That pseudo-node inflated the node
count, so a single real node plus the bucket (2 entries)…

- *(actions)* size the target-node dropdown to its contents (#283)
The NodeSelector renders with the 'settings-field inline' modifier, but no
CSS rule existed for it, so the select inherited width:100% and stretched
across the full page width. Add the missing rule so…

- *(suggestions)* restore trimmed-read card syntax
The main-side conflict resolution contained literal newline escapes in the card description, making the module invalid Python. Restore the intended implicit string concatenation while rebasing the CI …

- *(ci)* make backend smoke lint baseline clean
The new backend-smoke job checks all backend application sources, exposing existing style violations before the smoke tests could run. Clean the remaining Ruff findings so the workflow can enforce the…

- *(frontend)* pad the alignment preset select on Safari
The native select was excluded from the shared field styling, leaving Safari's focus treatment tight against the control edge. Match the dialog input padding and use the existing in-field accent focus…

- *(history)* always show the Provenance hint on the History tab
The hint was gated on obj.facts.sra_accession being a string, a field
that has nothing to do with whether provenance data exists. This caused
the Provenance hint to appear only for objects with an SRA…

- *(frontend)* retain sequence chart hover outside plots

- *(frontend)* render the missing node selector on DE results
ExpressionResults.tsx declared targetNode/setTargetNode and read
targetNode when launching the DE AI summary, but never rendered a
<NodeSelector> to let the user set it -- the setter was unreachable,
…

- *(frontend)* remove stray tokens breaking the web container build
client.ts, DifferentialExpressionDialog.tsx, VariantDialog.tsx,
QuantifyDialog.tsx, and ScaffoldDialog.tsx each carried a leftover
fragment from a partial targetNode/NodeSelector merge: stray
`useStat…

- *(pipelines)* pass threads= through governor pre-flight memory estimate calls (#234)
Thread-count segmentation (#8) wired threads through worker.py:_eta_model_ms
and jobs.py:get_job, but the three memory_estimate.resolve() calls inside
pipeline_service.py (declared_align_mem_mb, launc…

- *(frontend)* add missing gtf entry to ACCEPT_CHOICES (#233)
ACCEPT_CHOICES had a gff:annotation entry but no gtf:annotation entry.
FormatKind.GTF is a distinct enum value from FormatKind.GFF on the backend,
and portAccepts matches on format exactly, so a gff-t…

- *(pipelines)* orchestrator port checks use ports_for(node) instead of static spec (#232)
_is_multi_port and _advance both read spec.inputs directly instead of
calling ports_for(node), the tool-aware resolver added for STAR's annotation
port. Safe today only because the single tool-added p…

- *(pipelines)* alignment memory estimate sums extra_reads bytes (#231)
The pre-flight memory check and the queue reservation in launch_alignment
both used only obj.size to estimate input bytes, ignoring any extra_reads
files. Since the align_reads handler concatenates ev…

- *(queue)* harden gc_blobs, verify_files, and reap_uploads (#210)
Six hardening fixes from a second-pass code audit:

- *(pipelines)* drop launch_completeness from EXCLUDED_LAUNCHES
It was already registered in NODE_TYPES (95b2b61) as a canvas node
with outputs=() and run_kind=None, deliberately modeling "facts
merged onto an existing assembly, no output port to wire." A later
co…

- *(queue)* reap transcript_qc/bam_stats scratch dirs, fix inaccurate index error, test _is_gzip
Three follow-ups from reviewing the gzip/BAM-index fix:

- *(queue)* read gzip-compressed GTFs and symlink the BAM index for transcript QC
Two bugs found only by running run_transcript_qc against a real indexed BAM
with a real GTF -- neither is reachable from a unit test, since every
fixture feeds pre-parsed strings or a bare in-memory B…

- *(frontend)* render transcript QC independent of bam_stats
TranscriptQc was nested inside the hasResults conditional block, which is
gated on bam_stats_status -- an unrelated computation. A user with a bare,
unindexed RNA-seq BAM who hadn't yet run "Compute r…

- *(frontend)* default the GTF selection reactively, reuse BamStatsFacts instead of a local copy

- *(queue)* use thread mode for the in-process handler, floor per-contig sampling at one read

- *(pipelines)* make interval coverage lookup correct for any span, not just short ones

- *(pipelines)* match GTF attributes by exact key, sort exons on construction

- *(backend)* strided sampling in alignment stats for indexed BAMs
Previously `alignment_stats` capped sampling at the first 200,000 reads,
which for coordinate-sorted BAMs all come from the beginning of the first
contig. Every library-wide statistic (GC content, MAP…

- *(pipelines)* fail loudly on missing bucket sequences and malformed .fai lines (#200)
1. write_bucket_fastas silently skipped sequences not found in the
   reference FASTA. A .fai/FASTA mismatch produced an incomplete per-bucket
   file, the alignment ran against a subset of the refere…

- *(pipelines)* refuse chunked plans that exceed the memory budget (#199)
pack_buckets had two defects that produced plans it knew would OOM:

- *(pipelines)* make chunked alignment run end to end (#198)
Seven defects, each independently fatal on the first real run:

- *(frontend)* label History tab paragraph section "Summarize" before generating
The pre-generation state kept the "Methods paragraph" header even though no
paragraph existed yet, which read oddly next to the invite text and button.
Match the mockup: "Summarize" until a paragraph …

- *(frontend)* fix bwa-mem2 preset selector type errors on main
Two independent bugs, both pre-existing on main and unrelated to this
branch, surfaced by rebasing onto its latest bwa-mem2 organism-preset
commit:

- *(frontend)* add missing chunked-alignment fields to API types
AlignDialog.tsx references AlignParams.chunked and
AlignEnvelope.chunking, but neither was ever added to types.ts when the
chunked-alignment feature landed -- npm run build (tsc -b && vite build)
has …

- *(frontend)* wrap AssemblyGraph in .section for column-break protection
facts-columns uses CSS multi-column layout, and only children rooted in
.section get break-inside: avoid. Every other child of this container
(FactsTable's groups, QcReport, TrimReport) already has th…

- *(frontend)* guard AssemblyGraph's layout effect against array-identity churn
useEffect was keyed on the segments/links arrays themselves. A caller that
constructs them fresh each render (e.g. mapping over fetched data inline
rather than memoizing) would trigger a full cytoscap…

- *(backend)* restore TestFastaContiguity class boundary
TestGfaParsing's class body ran on past its own 8 tests and absorbed 16
FASTA-contiguity tests that belong to TestFastaContiguity, which lost
them via a missing class header. Restored as one merged
Te…

- *(frontend)* stop NGx curve from truncating when assembly exceeds expected genome size
ngxPoints() dropped every point past gx=100, which happens whenever the
assembly is larger than the expected genome (a plausible case: retained
duplicated haplotypes, or an underestimated genome size)…

- *(frontend)* align two-column fact group headers instead of balancing by height
column-fill's default "balance" mode splits total content evenly between
the two columns and starts column two wherever that height lands, not at
the top of a group. A short group landing first in col…

- *(metrics)* key run rows by position, not job_id or timestamp
Neither candidate is unique in a real library: job_id is null on rows
recorded before it was stored, and batch-recorded runs share a finished_at
to the second -- eight ingest_headers rows sit on one t…

- *(backend)* import field so ToolMeta can construct, unbreaking the API
`ToolMeta.recommendations` used `field(default_factory=dict, repr=False)`
but `dataclasses.field` was never imported, so importing `tools` raised
NameError at module load -- the API container failed t…























## [0.3.0-alpha] - 2026-08-10




### 🚀 Features

- *(agent)* add drive-pipelines, interpret-alignment, variant-analysis, database-access skills; sharpen debug-failed-job; bump pi-subagents pin (#83)

- *(agent)* wire datasets and fetch MCP servers into agent spawns via AGENT_EXTRA_MCP_SERVERS (#83)

- *(agent)* install pi-subagents, pi-hermes-memory, and the datasets+fetch MCP servers in the api image (#83)

- *(frontend)* add download buttons for built reference indexes (#176)
Index files are sidecar DataObjects with their own object_ids. Expose
the ids via the references API and add download links next to each
built index in the reference detail panel. The existing downloa…

- *(ops)* staged alpha/beta releases via release.sh (#107) (#172)
* docs(spec): staged alpha/beta releases via release.sh (#107)

- *(frontend)* add BUSCO completeness stacked bar chart
Render a stacked horizontal bar showing single-copy, duplicated,
fragmented, and missing percentages with a color legend. The existing
completeness facts already carry all the data; this adds the visu…

- *(ui)* add Infer-from-FASTQ button for molecule type

- *(api)* add manual molecule-type inference endpoint
Wires up Task 4's infer_molecule_type sampler behind a new user-triggered
POST /{object_id}/infer-molecule-type route so the frontend Infer button has
something to call. Runs synchronously and does no…

- *(metadata)* add manual FASTQ base-composition molecule-type inference

- *(metadata)* derive molecule type and library source from SRA

- *(metadata)* add molecule type and library source fields

- *(ui)* hide the Feedback page behind a settings toggle, off by default
Adds a Settings \xc2\xb7 General tab with a "Show the Feedback page" checkbox.
While off, Feedback is excluded from the Help menu and the /help/feedback
route redirects to /help/about, so direct navig…

- *(api)* add general settings endpoint for feedback page visibility
Adds an AppSettings singleton with a feedback_enabled flag, off by
default, plus GET/PUT /settings/general to read and update it.

- *(ui)* recommend tools by read chemistry in the tool picker (#109)
Adds a recommendations field to ToolMeta keyed on short/long read
chemistry, surfaced as a 'Recommended' badge in PipelineToolSelector
rows. The picker already receives the object's QC-inferred chemis…

- *(ui)* show the per-tile quality heatmap on the QC tab

- *(ui)* draw per-tile quality as a heatmap with two colour scales

- *(frontend)* type the per-tile quality matrix and its fetch

- *(api)* serve the per-tile quality matrix as JSON

- *(pipelines)* scan flow-cell tiles as part of short-read QC
Wires tile_scanner into _run_short_read_qc as a third optional extra
alongside FastQC: its failure is logged and swallowed, never the job's,
so an unexpected header format never costs the user the fas…

- *(pipelines)* write the tile matrix as a sidecar, not into object facts

- *(pipelines)* summarise read quality by flow-cell tile and position

- *(pipelines)* read flow-cell tile and coordinates from read headers

- *(qc)* N content chart, and mount both new charts in the QC tab
Adds NContentChart to SequenceCharts.tsx, drawing per-cycle N percent
with a dashed reference line at the 5% grading threshold (N_SPIKE_REFERENCE,
a deliberate duplicate of readQuality.ts's N_SPIKE_LI…

- *(qc)* per-sequence GC distribution chart

- *(qc)* demote a read file for a collapsed sequencing cycle

- *(qc)* expose expected GC on the object detail endpoint
Wires expected_gc.resolve() into GET /objects/{id}: queries the project's
reference-role objects, resolves against organism metadata, and returns the
result as ExpectedGc on ObjectDetail. references_f…

- *(qc)* resolve expected GC from a project reference genome

- *(qc)* cited genome GC table for the expected-GC cascade
Tier 2 of the three-tier expected-GC cascade: a small, hand-maintained
table of published GC percentages for well-known genomes, each with a
citation, plus from_organism() to resolve a normalized orga…

- *(qc)* parse per-cycle N content from fastp's content_curves
Adds qc_n_per_position to parse_qc_facts, scaled from fastp's raw
fractions to percentages. Omitted when the curve is absent or entirely
zero, matching how the rest of QcReport self-suppresses.

- *(qc)* bin per-read GC into a histogram at ingest
Replaces the per-read float list with a Counter[int] binned at integer
GC%. gc_per_read_mean is now derived from the same counts rather than
kept as a parallel accumulator, and a new gc_per_read_histo…

- *(qc)* mount contamination charts, prefer whole-file duplication

- *(qc)* adapter content and duplication level charts

- *(qc)* fact types for contamination charts

- *(qc)* run contamination scan during short-read QC
Wires scan_contamination() into _run_short_read_qc, right after the
FastQC block and before the qc_read_chemistry fact. Compression is
sniffed from magic bytes via the new compression_of() helper rath…

- *(qc)* whole-file contamination scan entry point
Ties build_probes, DuplicationTracker, and AdapterTracker into
scan_contamination(), one whole-file FASTQ pass producing qc_-prefixed
facts for the QC job. Failures short of cancellation return {} rat…

- *(qc)* per-position cumulative adapter tracker

- *(qc)* duplication tracker with frozen-dictionary correction

- *(qc)* duplication level slot binning

- *(qc)* port FastQC duplication correction formula

- *(qc)* adapter probe set for contamination scan

- *(ops)* generate CHANGELOG.md from Conventional Commits with git-cliff (#106)
ops/release.sh now bootstraps a pinned, checksum-verified git-cliff 2.13.1
(cached under ~/.cache/bioflow-tools) and regenerates CHANGELOG.md as part
of the app release commit, before the tag is creat…

- *(frontend)* render read length distribution chart in QC tab

- *(frontend)* add LengthDistributionChart component

- *(frontend)* add ReadLengthHistogramBucket type

- *(backend)* add read length histogram to alignment_stats
Adds 10bp-binned length histogram to alignment_stats output for BAM/CRAM/SAM
files, mirroring the histogram already added to fastq_stats. Unlike insert size,
read length has no ceiling to support long…

- *(backend)* add read length histogram to fastq_stats
Adds 10bp-binned length histogram to fastq_stats output, following the
pattern already used by insert_size_histogram. Unlike insert size, read length
has no ceiling to support PacBio HiFi reads exceed…

- *(frontend)* record project visits for recent-projects list

- *(frontend)* mount RecentProjects in header status strip

- *(frontend)* add RecentProjects chip-list component

- *(frontend)* add useElementWidth ResizeObserver hook

- *(frontend)* add localStorage-backed recent-projects tracker

- *(ui)* drop quality word from the row metadata line
The n/5 badge on the file icon already states the grade, with the
word available on hover/aria-label; repeating it as "Excellent" etc.
in the metadata line was redundant.

- *(ui)* move R1/R2 badge into the metadata line for paired reads
The read-number badge sat outside the row icon, ahead of the filename;
now it leads the size/type/quality line instead, so the filename gets
the full row width and no longer truncates.

- *(settings)* group Settings · Tools by pipeline step
Restructures the Tools page from one flat alphabetical card grid into
seven headed groups -- Reads & QC, Alignment, Variants, Assembly,
Expression, Archive, Utilities -- so it reads the way the pipeli…

- *(ui)* draw Files and Projects icons in the header stats strip
Replaces the words "files" and "projects" in the "150 files · 7 projects ·
76.4 GB stored" strip under the masthead with their BioIcon glyphs (variant
A: the dog-eared document and the tabbed folder),…

- *(icons)* extract round-2 Section VII icons, wire raw/trimmed reads, replace profile emoji
Adds connection_closed, database_not_found, reads_raw, reads_trimmed and
user from the second icon design system review, following the existing
a/b/c variant pattern.

- *(icons)* extract Section VII icon set and use them in the footer
Adds the six remaining glyphs from the icon design system review
(queued, running, projects, files, ask, agent, live) to BioIcon.tsx,
following the same a/b/c variant pattern as the rest of the set.
S…

- *(ui)* restructure the History tab to the 1c mockup layout
Two columns: the lineage and runs table on the left, the methods paragraph
and a "Not recorded" rail on the right. Gaps move off the front of the panel
into that rail, where they read as a list of thi…

- *(provenance)* serve the lineage as structured rows, merged and ordered
The History tab rendered `ProvenanceNarrativeOut.markdown`, so every fact it
showed had to be read back out of rendered prose. Its replacement (next
commit) renders numbered rows with inline gap marke…

- *(ui)* draw file and project icons in the explorer
Wires BioIcon into ProjectExplorer, which is what makes the set visible: the
old FileIcon/getFileIcon and the fifteen format PNGs were dead code, and file
rows carried no icon at all. Categories are n…

- *(ui)* quality badge as a serif figure instead of a color dot
Replaces the 9px green-to-red dot with the tier set as a numeral (option B
from the design review).

- *(icons)* add BioIcon set with reviewed variants and SVG assets
Lands the 48-concept icon set from the design review as a React component
plus standalone SVG files.

- *(activity)* say what each run is doing, not just what it acted on
A run's card led with its stored label, which names the operands and never the
verb: "DRR1066343 (paired) → GCF_000146045.2_R64_genomic.fna" does not say
that this is an alignment. Downloads and trims…

- *(activity)* fold workflows into the run columns
The Workflows section had its own strip above the run columns and its own
visual language -- a bordered card with a chevron head -- which put a
third way of showing "a thing the user started" on a pag…

- *(agent)* say when the drawer is resuming an existing conversation
Track whether onConnectionChange attaches to an already-running agent
with no local transcript, and show that as "Resuming an earlier
conversation" in the empty state instead of the generic first-run
…

- *(agent)* add a New session button that clears the conversation
Restart respawns the pi process but keeps its session file, so memory
survives. This new button calls POST /new-session, which deletes the
session file too -- a distinct operation from restart, with i…

- *(agent)* add new-session endpoint that clears the conversation
Distinct from /restart (which respawns against the same pi session and
keeps memory), /new-session stops the process and deletes its session
file so the next prompt starts a clean conversation. Matche…

- *(agent)* persist conversations via pi sessions per (profile, project)
Replace --no-session with --session-dir/--session-id so a pi process lost
to the idle reaper, a crash, or an api restart reloads its conversation
on respawn instead of starting fresh. Session id is st…

- *(agent)* add agent_sessions_dir config property
Derived from BIOINFO_HOME following the same pattern as objects_dir,
staging_dir, tmp_dir, and logs_dir. Kept outside tmp/ since session
files are the conversation itself, not a rebuildable cache. Fir…

- *(workflow)* bind files at the input node
A project selector on the toolbar, file pickers on input nodes, and slots
that hold several files. Bindings stay per-run -- the definition still
stores no object ids. The run-side binding dict now acc…

- *(frontend)* node detail panel with a generated parameter form
Double-click opens a node in full: tool, parameters (reusing the existing
aligner-schema endpoint AlignDialog already uses), wiring, and
continue_on_failure -- which no UI could set before. Part of #9…

- *(frontend)* choose a node's tool on the canvas
Nodes drop with the registry default, show their tool, and re-shape their
ports on a change -- reporting any wires that drop. Also fixes the stale
static-port derivation Task 6 flagged: port dots and …

- *(frontend)* per-node port resolution and multi-port wiring rules
portsFor mirrors the backend's ports_for; canConnect relaxes fan-in for
multi ports only; edgesInvalidatedBy reports what a tool change breaks.
Part of #94.

- *(api)* serve tool choices, per-tool ports, and tool schemas
Part of #94.

- *(workflow)* ports resolve per node from its chosen tool
STAR gains an annotation port; every other aligner keeps the base set.
Validator and binder both route through ports_for. Part of #94.

- *(align)* pass extra read files through to the aligner
Concatenated into the primary read file before alignment, since none of
the six aligners this runner drives take several read files positionally
or share a multi-file convention -- concatenation is th…

- *(workflow)* bind and launch multi-valued ports as lists
Scalar ports keep binding bare ids -- the union return type avoids a
no-behaviour diff across every existing consumer. Fixes _bound_inputs,
which previously overwrote rather than accumulated when two …

- *(workflow)* multi-valued input ports, and align.reads becomes one
Only the one-wire-per-port rule relaxes; type checking still applies per
wire. Part of #94.

- *(agent)* edit per-project agent instructions from the drawer (#98)

- *(agent)* restart respawns with the project's composed prompt (#98)

- *(agent)* append the project's custom prompt to the default grounding (#98)

- *(agent)* store a per-project custom system prompt (#98)

- *(agent)* persist conversation across drawer open/close (#97)
The agent drawer lost its entire conversation when closed. This adds
persistence to ProjectConversation (reusing the existing Q&A model), keyed
by (owner, project_id).

- *(activity)* show why a run failed when its ledger row is opened

- *(activity)* add RunFailureBlock for a failed run's errors and logs

- *(timing)* add per-thread-count segments to stats() diagnostics
Extends the /timing-model diagnostics stats() output with a `segments`
list per job type, showing which thread counts have qualified for their
own duration fit (>= MIN_SAMPLES same-thread rows). Each …

- *(timing)* pass known thread counts into duration/memory estimates
worker.py's _eta_model_ms and jobs.py's get_job route now pass
job.payload.get("threads") through to timing_service.estimate,
timing_service.estimate_memory, and memory_estimate.resolve -- the
three c…

- *(timing)* thread threads through memory_estimate.resolve()

- *(timing)* thread-count segmentation for estimate() and estimate_memory()

- *(timing)* add _fit_segmented grouping function

- *(agent)* add useAgentSSE hook and AgentPanel UI components (#88, #89)

- *(agent)* add agent API router with SSE stream, stop/restart, lifespan cleanup (#87)
POST /projects/{project_id}/agent/ask spawns lazily (ownership-scoped
project lookup feeds the spawn-time system prompt) and returns accepted;
everything after that arrives on GET /events, an SSE stre…

- *(agent)* add AgentService spawning a pi RPC subprocess per (profile, project) (#86)
AgentProcess speaks pi's JSONL RPC protocol over stdin/stdout and
translates events to SSE shapes for the drawer: agent_start, message_delta
(text/thinking), tool_call/tool_result (unwrapping pi-mcp-a…

- *(agent)* install pi + pi-mcp-adapter in api image, add agent config and starter skills (#85)
Task 1 of the AI agent harness epic (#30): backend config, Node 22 + pi
installation in the api container, and four starter skills.

- *(frontend)* workflow runs in the activity view
A workflow's nodes are ordinary PipelineRuns and jobs, deliberately (§1.5),
which is exactly why this section had to exist: without it a workflow was a
flat pile of unrelated rows with nothing saying …

- *(api)* read workflow runs, retry failed nodes, cancel
Nothing exposed workflow runs before this: `/workflows/{id}/runs` launched one
and handed back an id, and there was no way to ask what happened next. Adds
GET /workflows/runs and /workflows/runs/{id},…

- *(frontend)* add Local Databases section and drop dice emoji
Wires the Local Databases catalog into HelpDatabases.tsx (list + submit
modal via react-query) and removes the dice emoji from Surprise me.

- *(frontend)* add AddLocalDatabaseModal component

- *(api-client)* add local-databases types and client methods

- *(api)* add local-databases submit and list endpoints

- *(models)* add LocalDatabase document

- *(workflows)* derive a canvas from previous runs
Design §7: read a set of PipelineRuns, map each to its node type, make an INPUT
node per external RunInput, and infer edges where one run's output id appears in
another's inputs. The result is unsaved…

- *(frontend)* bind inputs and launch a workflow from the canvas
The Run dialog: choose a project, bind each input slot to a real file, start
the run. `bindableObjects` (6 vitest cases) does the filtering, and filtering is
the whole point -- an aligner's reference …

- *(frontend)* the workflow canvas
A hand-rolled SVG editor rather than a graph library: this frontend runs on six
runtime dependencies, and `NodePosition` was already modelled, so the editor is
drag, click-to-wire, and delete over pla…

- *(api)* workflow definition CRUD, node-type palette, and launch
The service layer shipped in #20 was unreachable -- no router, so a definition
could only be created from a Python shell. This is the HTTP surface the canvas
(and #80's activity view) needs.

- *(queue)* wire the orchestrator to job completions and worker startup
The orchestrator was inert: `on_node_finished` and `reconcile_workflows`
existed and were tested, but nothing called either, so a workflow would launch
its first wave and stop forever.

- *(services)* the workflow orchestrator
Launch, progressive advance, retry-in-place, cancellation, and the reconciler
the design's §10 names as a structural risk. The two decisions stay pure in
workflow_planner and workflow_binding; this is…

- *(services)* resolve a finished node's outputs onto downstream ports
Resolution is by declared output name with type as validation, per the
design's §6 -- not by type alone. Type-only matching picks arbitrarily the
moment a node produces two objects of one type, which …

- *(services)* the workflow scheduling decision, as a pure function
`runnable_nodes` and `doomed_nodes` are duals: what may start now, and what
never can. A node in neither is still waiting.

- *(queue)* honour per-edge failure tolerance in the two cascade routes
#20 landed `classify_dependencies`' `tolerate_failure_of` parameter but no
caller: both routes that fail a dependent re-read `depends_on` from the
database, and neither had anywhere to learn which edg…

- *(services)* workflow definition CRUD with version bumping
create_definition/update_definition validate before persisting, raising
InvalidGraph rather than silently saving an unrunnable graph. update_definition
always bumps version, even for no-op edits, sinc…

- *(services)* workflow graph validation

- *(pipelines)* classify every launcher as a node type or exclusion
Classifies the 19 remaining launch_* functions (variant calling,
quantification/DE, de novo and reference-guided assembly, QC/stats
checks, annotation, and the three launch_download shapes) as NodeTyp…

- *(pipelines)* node-type registry scaffolding with exhaustiveness test
Adds NODE_TYPES/EXCLUDED_LAUNCHES and the launch_function_names() inspection
helper that backs test_every_launch_function_is_classified. Only trim, align,
and qc are classified here; the remaining ~17…

- *(models)* register WorkflowRun and WorkflowNodeRun in ALL_MODELS
Task 4's own DB-backed tests need these registered with Beanie to
avoid AttributeError/no-unique-index failures -- same gap Task 3 hit
and pulled forward for WorkflowDefinition. Task 5 no longer needs…

- *(models)* WorkflowRun, WorkflowNodeRun, derived status
WorkflowStatus is derived from NodeRunState via derive_status(), never
stored, mirroring RunStatus. WorkflowNodeRun is a separate document
(link-collection pattern) rather than an array on WorkflowRun…

- *(models)* register WorkflowDefinition in ALL_MODELS early, needed for its own DB-backed test
Task 5 was going to register the workflow documents in ALL_MODELS, but
WorkflowDefinition's own insert/get round-trip test needs Beanie to know
about it now, not later. Task 5 still registers Workflow…

- *(models)* WorkflowDefinition document
Adds NodePosition, WorkflowNode, WorkflowEdge, and WorkflowDefinition to
app/models/workflow.py. A definition is a saved graph, deliberately
project-independent: no project_id, no bindings, and no por…

- *(models)* workflow port types and node kinds

- *(queue)* let a dependent tolerate named dependencies' failure

- *(frontend)* render the refusal card from both launch dialogs
AlignDialog renders ResourceRefusalCard pre-flight when its client-side
band computation hits "block", replacing the old dead-end banner (warn
still gets the plain banner). AssembleDialog has no clien…

- *(frontend)* add the four-choice resource refusal card
Task 8 of the #70 plan: shared component with props for AlignDialog's
pre-flight band computation and AssembleDialog's reactive 422 body.
Wiring happens in Task 9.

- *(frontend)* add replan types and client call
Mirrors replan_service.ReplanResult's tagged union and the /pipelines/replan
endpoint response, plus the 422 resource-refusal details shape both
launch_alignment and launch_assembly now attach. Feeds …

- *(api)* add POST /pipelines/replan for the refusal card
Alignment's refusal card (a later task) calls this directly to power its
"Auto re-plan" button. Assembly does not need it -- its replan result
already rides along in the 422 raised at enqueue time (Ta…

- *(pipelines)* honour the resource override and enrich refusal details
launch_alignment and launch_assembly now skip the enqueue-time BLOCK raise
when resource_override is set, letting the job ride through to claim.lua's
sole-occupant admission (Tasks 1-4). The refusal's…

- *(queue)* admit an overridden job when it is the sole occupant

- *(queue)* rebuild the override flag on reconcile

- *(queue)* carry the override flag into the Redis job hash
enqueue() now accepts resource_override and threads it through the
Job document into _push_to_redis's hash write, always writing "1" or
"0" rather than omitting the field -- claim.lua (Task 4) will re…

- *(queue)* persist a per-job resource override flag
Adds Job.resource_override, set when the user chooses "Launch anyway"
on the refusal card (issue #70). Defaults to False; must survive
retries the same way last_attempt_progress does, since its purpos…

- assembly proposer descends threads against the graph floor (#71)

- alignment descent moves sort buffer before threads (#71)

- alignment feasibility test refuses index-dominated jobs (#71)

- capacity clamp for thread counts above core count (#71)

- verify every replan proposal before offering it (#71)

- replan result types and empty proposer registry (#71)

- *(ui)* state the hard-limit distinction and clamp the budget

- *(api)* clamp the admission budget to the cgroup hard limit

- apply opt-in mem_limit to the worker container
Compose v5 type-checks mem_limit as a byte-size field and rejects an empty
string, so the plan's original ${BIOFLOW_HARD_MEM_LIMIT:-} fails config
validation in the default, unset state -- caught by a…

- *(launcher)* read the hard memory limit back from .env
Adds current_settings, a new Tauri command that parses
BIOFLOW_HARD_MEM_MB out of .env (None on any malformed or absent
value), registers it in lib.rs, and wires App.tsx's mount effect to
populate the…

- *(launcher)* hard memory limit field with both-state copy
Adds hardMemGb to the Settings type, wires it through applySettings
(GB string -> hard_mem_mb: number | null via parseHardMemGb), and adds
the field to the Settings dialog with copy for the none/set/i…

- *(launcher)* parse the hard memory limit field

- *(launcher)* carry hard_mem_mb through apply_settings

- *(launcher)* write hard memory limit vars to .env
CurrentSettings gains hard_mem_mb; render_env emits
BIOFLOW_HARD_MEM_LIMIT, BIOFLOW_HARD_MEM_MB, and WORKER_REPLICAS=1
only when a limit is set, so "off" means the variables are absent
rather than emp…

- recognize a running biopipe stack outside ~/.bioflow (dev-trunk compose case)
The launcher only ever looked for an install at the fixed ~/.bioflow path,
so a biopipe stack started by hand with plain compose from a repo checkout
(this repo's own dev-trunk workflow) was invisible…

- add ops/migrate-storage.sh for manual BIOINFO_HOME migration

- add storage migration dialog to the launcher UI

- add finish_storage_migration command to rewrite .env and restart after a successful migration

- wire the real migration sequence and live progress polling into start_storage_migration

- add start_storage_migration and migration_progress Tauri commands (plumbing)

- orchestrate storage migration scan/copy/validate/cleanup sequence

- add original-directory removal step for storage migration

- add count/size and hash validation for storage migration copies

- add recursive directory copy with progress callback for storage migration

- add source directory scan and free-space check for storage migration

- report estimate provenance on the job detail endpoint

- resolve assembly memory through the layered estimate
launch_assembly now routes its heuristic estimate through
memory_estimate.resolve() instead of using estimate_assembly_mb's None as
the sole "say nothing" signal. History for assemble_reads can now an…

- name the estimate source in the alignment refusal message
The alignment BLOCK check now resolves memory through the layered
memory_estimate.resolve() (heuristic or measured) instead of calling
the heuristic estimator directly, and passes the resolved provena…

- resolve the alignment reservation through the layered estimate
declared_align_mem_mb now goes through memory_estimate.resolve() instead of
only the heuristic estimator, so once align_reads has enough trustworthy
history on this machine the queue reservation becom…

- let explain() name the model its number came from

- guard the measured model on extrapolation distance and fit quality
Two independent trust checks, since neither subsumes the other: a clean
fit can still be extrapolated too far past what was observed, and a poor
fit (low r_squared) can be wrong entirely inside the ob…

- prefer the measured model over the heuristic when history exists

- add MemoryEstimate types and the UNKNOWN resolution case
Test fixture used get_motor_collection(), which doesn't exist on this
Beanie version; switched to the working drop pattern from
tests/storage/test_memory_model.py (raw motor db handle, drop before
ini…

- add measured-model trust guards for the estimate resolver

- add winnowmap as GCI's second aligner for continuity cross-checking (#73)
GCI's own FAQ recommends pairing winnowmap with minimap2 -- two aligners
cross-check better in repetitive regions. Its --hifi/--nano are nargs='+'
natively, so pairing needs no merge step: both BAMs g…





### 🐛 Bug Fixes

- *(provenance)* use job-type-aware verbs for branch merges in methods
The hardcoded 'combined two inputs' phrase was used for every branch
convergence regardless of what kind of merge happened. Record the
destination node's ID in each branch entry and add a per-job-type…

- *(backend)* prefer trimmed reads for QV assessment when raw+trimmed are the same sample
group_read_sets treats raw and trimmed versions of the same sample as
distinct read sets, causing the QV card to refuse with '2 read sets'.
When all sets are just raw+trimmed of one sample, collapse t…

- *(backend)* default sample_id from BioSample accession on SRA import
The schema's sample_id field (user-facing sample identifier) was
never populated automatically from SRA metadata. Default it from
the NCBI BioSample accession when present, so imported runs
carry at l…

- *(frontend)* hide 'Paired End' option for alignment files
isReads() excluded references and sidecars but not alignments, so BAM
files could show a 'Paired End' control that doesn't apply. Gate on
role !== 'alignment' as well. Closes #144.

- *(backend)* say 'reference genome' not 'reference assembly' in scaffold error
'Reference assembly' reads as the user's own assembly output, confusing
projects that do have a downloaded reference genome. Change to 'reference
genome to order contigs against' so it's clear what's …

- *(backend)* explain polypolish absence on arm64 instead of generic PATH error
The generic 'was not found on PATH' message prompted users to hunt
for a fix that doesn't exist when polypolish is intentionally absent
on arm64 / Apple Silicon. Provide an explicit architecture note.…

- *(frontend)* translate raw format confidence enum to human-readable labels
Raw values like 'magic' and 'extension' read as bugs or leaked
placeholders. Map them: magic → 'Identified from contents',
extension → 'Guessed from filename', user → 'Set by user',
none → 'Unknown'. …

- *(frontend)* capitalize 'Ai' to 'AI' in auto-generated fact labels
The fallback label formatter title-cased snake_case keys, producing
'Ai summary' from 'ai_summary'. Add a post-processing step to correct
the leading 'Ai' to 'AI' for keys like ai_summary, ai_summary_…

- *(frontend)* show assembly level, taxonomy ID, and release date on references
The NCBI assembly block already showed name, sequences, total bases, and
GC content. Add assembly level, taxonomy ID, and release date (formatted
as locale date) so the reference headline gives a full…

- *(api)* reject molecule-type inference for non-FASTQ objects
The frontend only shows the "Infer from FASTQ" button for FASTQ objects,
but the endpoint trusted that gating instead of enforcing it: a direct
call against a BAM, FASTA, or any other stored file woul…

- *(scripts)* narrow molecule-type backfill to run/experiment accessions
Sample/study accessions can span multiple runs with different
library_source values, and sra.lookup() silently falls back to an
arbitrary first run when given one -- dropping them from the
candidate s…

- *(ui)* surface a notification when Infer-from-FASTQ fails

- *(frontend)* add key prop to LeadStory for React identity tracking
Without a key={lead.id}, React cannot track which lead instance is
which across re-renders, so a finished lead's visual slot is not
visibly replaced by the next running activity. Closes #128.

- *(frontend)* add note that clicking a chromosome opens NCBI viewer
Chromosome bars that link to NCBI had no visual indication they were
clickable. Add an explanatory note below the diagram when chromosomes
are linkable, matching the existing note for non-linkable one…

- *(frontend)* label inherited reads metadata on alignment files
Alignment (BAM) metadata carries the sample-level fields from the source
reads, but the Custom fields section didn't indicate this -- the fields
read as if they belong to the alignment file. When role…

- *(backend)* flag trim card as unavailable for already-trimmed reads
build_preprocess_card did not check whether the object was already
a trimmed derivative, so the Actions tab showed a trim-for-trimming
suggestion on files that had already been through the pipeline.
M…

- *(frontend)* label the Copy report button with its output format
The button copies markdown but nothing indicated the format.
Change 'Copy report' to 'Copy report (Markdown)' so the user
knows what they're pasting. Closes #143.

- *(frontend)* sync stage rail toggle on cross-stage navigation
Clicking a 'Derived from' link on a trimmed file navigated to the raw
counterpart but left the Raw/Trimmed toggle on Trimmed -- the selected
raw file was invisible in the list because the sidebar stil…

- *(frontend)* wire ⓘ info bubbles to their help text
The ⓘ icons next to Sample ID, Subject/Patient ID, and Tissue/Source
metadata fields were inert -- a plain span with no click handler and no
tooltip of its own. The help text sat on the parent label's…

- *(provenance)* stop flagging params as unrecorded for downloads
Download jobs (SRA, assembly, UniProt, lineage) structurally never record
parameters -- the accession and source are facts, not knobs -- so the
History view's 'parameters not recorded' gap implied a h…

- *(ai)* report the served model, not the requested one
OpenAI-compatible local servers (mlx_lm, LM Studio) can keep serving a
previously-loaded model while accepting a new 'model' field in the
request. The adapter never read the model name back from the r…

- *(pipelines)* find the Illumina header in any token, not just the first
Verifying against real files in this app's /data surfaced a gap: every
SRA-sourced short-read FASTQ available here wraps the original machine
header as a *later* space-delimited token rather than stri…

- *(ui)* make the canvas hover tooltip match the tile it's showing
Code review found that on a large flow cell (NovaSeq scale, 1408+
tiles), where several tiles share one pixel row, the paint loop wrote
tiles in ascending order with later ones overpainting earlier on…

- *(pipelines)* bound tile extents by the same cap as the quality matrix
Code review found extents grew unconditionally in scan(), so a
malformed file with many fabricated tile values kept accumulating
coordinate boxes even after sums/counts correctly stopped at
MAX_TILES …

- *(help)* disambiguate the new N-spike bullet, correct N-chart caption claim
"A collapsed cycle" sat directly under the pre-existing "Collapsed cycles"
bullet, describing an unrelated rule (N-base spike vs. quality dropout) --
a reader had no way to tell these were two distinc…

- *(qc)* move misplaced test imports to top of file, add query type hints
test_expected_gc.py had two imports mid-file after the first test classes,
which fails ruff's E402 (module-level import not at top). Moved with the
rest. Also gave references_for_project explicit para…

- *(qc)* key the E. coli GC table entry to its strain, not the bare species
GC genuinely varies by E. coli strain, and normalize_organism's own
docstring gives K-12 vs O157:H7 as the reason strain suffixes are kept in
the key rather than stripped. The bare "escherichia coli" …

- *(test)* correct GC% docstring in gc histogram test

- *(qc)* prefer whole-file duplication number in FileHeadline too
Task 10's spec updated QcReport.tsx's Duplication row to prefer
qc_percent_unique over fastp's sampled qc_duplication_rate, but missed
that FileHeadline.tsx reads qc_duplication_rate directly for the …

- *(qc)* keep contamination scan's progress phase monotonic

- *(qc)* bound adapter fill and denominator by each read's own length

- *(frontend)* handle narrow-range and single-length edge cases in axis ticks
Final whole-feature review caught two gaps the per-task reviews missed:
a long-read sample entirely under 100bp produced zero log-scale ticks
(unlabelled axis), and a uniform-length sample repeated th…

- *(frontend)* compute per-bar width from actual pixel spacing in LengthDistributionChart

- *(ci)* point the CLA action at the branch that holds the signatures
Every PR was failing with "Branch master not found. Make sure the branch
where signatures are stored is NOT protected."

- *(ci)* make release notes actually contain the changes
`gh release create` was passed both --generate-notes and --notes-file.
--notes-file overrides --generate-notes rather than appending to it, so every
release through v0.2.6 published the static image-p…

- *(provenance)* redirect sidecar walk targets to their parent
walk() skipped sidecars when they appeared as parents but did nothing
special when a sidecar was the walk's own target -- walking a .bai or
.fai directly produced a lineage whose last row read "proces…

- *(frontend)* hide RECENT block entirely when zero chips fit
chipCount reaching 0 from a narrow header (not from zero visited
projects, which already returned null earlier) still rendered an
empty div, leaving an orphaned border-left divider with no content
nex…

- *(frontend)* measure header-right, not RecentProjects's own div, for chip width budget
RecentProjects measured its own root div, which renders empty until
chipCount is decided -- a self-referential measurement that always saw
~0px and produced chipBudget < 0, so the RECENT block never r…

- *(frontend)* record a project visit once per navigation, not per refetch

- *(frontend)* align RecentProjects query key with ProjectExplorer's cache

- *(frontend)* correct useElementWidth's RefObject return type
The tuple return type annotated React.RefObject<T | null>, which is not
assignable to a JSX ref prop (RefObject<T> is expected) despite the
underlying useRef<T | null>(null) call being the standard pa…

- *(ui)* remove stale BioIcon import from Header
The icon-sheet branch moved the Files/Projects icons from the header
stats strip to the footer but left the now-unused import behind,
breaking tsc --noEmit.

- *(ui)* stop file-size units wrapping in the row metadata line
.row-sub items had no white-space guard, so a narrow row broke
"415.4 MB" across the space between number and unit. Each chip now
stays on one line; the whole line wraps between chips instead.

- *(ui)* import BioIcon in Header
Header.tsx used <BioIcon> for the files/projects stats-strip icons
without importing it, throwing ReferenceError on every page load and
blocking the app entirely.

- *(header)* import BioIcon, which Header.tsx used without importing
The icon-sheet merge (d42b2b1) added two <BioIcon> usages to the header
stats strip but no import, so `tsc --noEmit` failed with TS2304 on both
lines. Vite's dev server transforms without typechecking…

- *(ui)* drop the user icon next to profile names entirely
Neither the drawn icon nor the emoji it replaced looked right beside
the username -- just the plain username reads cleaner. Removes the
BioIcon usage from the header menu, profile picker tiles, and th…

- *(icons)* use variant A for the footer's Ask and Agent glyphs
The b variants (circle+dot marks) were picked by mistake -- the
speech-bubble (ask.a) and robot-head (agent.a) enclosures are the
recognisable shapes for these two, unlike the rest of Section VII.
Dro…

- *(ui)* align Results-tab Download TSV links with app link style
VariantTable and ContigTable rendered their "Download TSV" link as a
bare <a> with an inline 11px font-size instead of the shared
.btn-text class used by "Copy report" (History) and "Re-ingest"
(Actio…

- *(ops)* give each worktree stack its own ports automatically
WT_WEB_PORT/WT_API_PORT were fixed at 5273/8100, so the second concurrent
worktree stack died on "Bind for 0.0.0.0:8100 failed: port is already
allocated" unless the user set them by hand. Eight workt…

- *(ui)* move Provenance to History tab, style Generate prose as a button
- Provenance report + prose now render under History, below the
  computation-run table, instead of on Actions.
- The "Provenance" tab hint moves from Metadata to History, matching
  where the section…

- *(ops)* initiate the replica set with a member name that can work
ops/worktree-up.sh failed roughly one run in three with "container
biopipe-wt-*-mongo-1 is unhealthy". The cause was not a timeout: mongo was
healthy seconds later, and the replica set was being initi…

- *(queue)* advance workflow nodes on every terminal path, not just complete()
A workflow run sat on "running · 0/3 nodes" forever after its alignment
failed. The align job was correctly FAILED with `dependency_failed` -- its
build_index was OOM-killed -- but the WorkflowNodeRun…

- *(ui)* lay out Settings > Tools as a responsive multi-column grid
Replaced the single <table> with card-based rows in a CSS grid
(minmax(360px, 1fr)) so the list packs 2-3 columns on wide viewports
and falls back to 1 on narrow ones, instead of one long single-colum…

- *(ui)* put provenance prose in a second column, not below
Reuses the existing .detail-columns grid (already used for the
Overview/Project-metadata pair) so the generated prose sits beside
the structured report when there's width for it, stacking below on
nar…

- *(ui)* match header menu button weight to nav link weight
.theme-broadsheet button sets a bold heading font for real buttons
(.btn), which the header-menu override didn't undo for the File/
Reference/Help/profile dropdown triggers -- so they rendered bolder
…

- *(ui)* put param checkboxes beside their labels, not above
`.trim-check` asks for a flex row, but `.trim-fields label` (0,1,1) outranks
it (0,1,0) and forces `flex-direction: column`, so every checkbox inside a
`.trim-fields` grid stacked above its own label …

- *(align)* size references by measured bases, not compressed file size
`reference_bases` was `reference.size`, justified by a FASTA carrying about
one byte per base. That holds uncompressed and is wrong by ~6x for the
gzipped references NCBI downloads produce by default.

- *(align)* size bwa-mem2 index builds from its published 28N figure
bwa-mem2's README states indexing "Requires 28N GB memory where N is the
size of the reference sequence". The model had 2.0 bytes/base with a 2.0
build multiplier -- 4 effective, 7x under. It also car…

- *(provenance)* render the report as markdown, not raw text
The provenance report is generated as markdown by the backend
(services/provenance_report.py) but ProvenanceNarrative rendered
data.markdown inside a <pre class="mono">, so the panel showed literal
`#…

- *(queue)* classify SIGKILL as -9, not just 137
run_subprocess returns Popen.returncode unchanged, and Python reports a
signal death as the negative signal number -- SIGKILL is -9. 137 is the
shell's 128+signal convention, which nothing in this pro…

- *(ui)* stop generic .modal rules from breaking NCBI download checkboxes
.modal label and .modal input both had higher specificity than
.assembly-component and .assembly-component input, so inside the NCBI
download dialog the assembly-component checkbox rows fell back to
d…

- *(agent)* don't show 'resuming' before the agent's real status arrives

- tighten suppressResumedRef comment for accuracy
The flag is cleared by a fixed timeout, not by observing messages --
correct the comment left over from an earlier draft of the fix.

- *(agent)* confirm before starting a new session
The 🗑 New session button permanently clears the agent's conversation
history with no confirmation, unlike every other delete-class action
in this codebase (project/object delete, provider delete, tool…

- *(frontend)* move agent.css @import to the top of styles.css
CSS requires @import to precede all other rules; PostCSS silently
dropped it at end-of-file, so .agent-drawer never got position: fixed
and the backdrop overlay swallowed every click meant for the dra…

- *(agent)* render the user's prompt and stream replies into a bubble
ask.onSuccess built the user message with content: "" (the typed prompt
never rendered) and never appended an assistant placeholder with
isStreaming: true (the delta/tool handlers all bail without one…

- *(agent)* surface provider errors from turn_end instead of reporting success
A provider auth failure (e.g. bad API key) arrives in message.errorMessage
on the turn_end RPC event, which _translate didn't handle at all -- the
drawer saw agent_start -> done with no text, reading …

- *(workflow-canvas)* keep tool-name text off the first port dot
The tool-name <text> under a node's label sat at a fixed y+34, which
overlaps the first port dot for align's tool-choice node: 3-port tools
(minimap2, bwa-mem2, bowtie2, HISAT2, Winnowmap) put that do…

- *(workflow)* derive tool-configured ports via ports_for, not the static spec
workflow_derive.py's _port_for_role and the output-port lookup read
NODE_TYPES[node_type].inputs/.outputs directly, so they never saw a
reconstructed node's actual params. A real STAR alignment run's
…

- *(align)* reject self-referential/duplicate extra reads, dedupe blob resolution
Code review on the extra-reads-for-alignment change (issue #94 Task 3) found
two gaps:

- *(agent)* respawn on prompt change and stop dropping it on restart (#98)

- *(config)* correct pi_path to /usr/bin/pi (npm global prefix is /usr on Debian)

- *(timing)* score segment r_squared/range against the segment's own samples

- *(pipelines)* give reference-guided assembly launchers a PipelineRun (#91)
launch_consensus, launch_polish, and launch_scaffold are reference-guided
assembly work by RunKind's own comment, but none created a PipelineRun --
so a finished consensus/polish/scaffold had no run t…

- *(ci)* stop CLA Assistant from requiring a PAT it doesn't have
remote-organization-name/remote-repository-name pointed at this same
repo, which pushes the action down its "remote repository" storage
path and makes it require PERSONAL_ACCESS_TOKEN (repo scope) ins…

- *(queue)* skip re-ingesting an SRA accession the project already has
The job-level dedup_key in sra_service.launch_download only collapses a
second download request while the first job is still in flight. Once
that job finishes, a second request for the same accession …

- *(services)* bind mate pairs, and reconcile workflows periodically
Three bugs, all found by running a real trim -> qc workflow against the live
stack. The 3997-test suite was green throughout; none of them are reachable
from a fixture.

- *(services)* exclude sidecars and order mates when collecting node outputs
Both found by checking `_outputs_of` against the real database rather than
fixtures, exactly as CLAUDE.md prescribes -- the unit tests were green and
wrong. Of 70 real jobs with objects attributed to …

- *(tests)* read the Lua scripts outside the async fixture

- *(mcp)* give each guide resource a unique registered name
_make_guide_reader's inner closure was always named _read, and
MCPServer.resource() falls back to the wrapped function's __name__
when no name= is given -- so every one of the six guide resources
regi…

- run-level progress bar not moving for single-job runs
LeadStory's top bar computed pct purely as settled-jobs / total-jobs,
so a run with one long-running job (the common shape for an SRA
download) stayed at a literal 0% for the entire transfer and only
…

- *(services)* give InvalidGraph a real place in the AppError hierarchy

- *(models)* assert derive_status's FAILED precondition instead of a dead fallthrough
The prior fix wired _UNSUCCESSFUL_NODE_STATES into an if-branch but left
the original bare return as an unreachable trailing statement -- the
same class of dead code the review flagged, just moved dow…

- *(models)* make derive_status's FAILED branch assert its precondition

- *(frontend)* send reference_bases/building_index to the replan endpoint
AlignDialog's replan query sent only AlignParams (aligner/threads/
sort_memory_mb/mark_duplicates), but replan_service's align proposer reads
reference_bases/building_index directly with no default --…

- *(frontend)* guard Launch anyway against double-submit
ResourceRefusalCard's "Launch anyway" button had no pending guard,
unlike the primary Launch button in both dialogs -- a double-click
before the modal closed could enqueue the job twice. Add an option…

- *(pipelines)* document the replan params deviation and cover assembly's refusal path
Two follow-ups from code review on the resource-override change:

- *(api)* treat BIOFLOW_HARD_MEM_MB='' as unset, not an int-parse error
docker-compose.yml always sets this env var on the api container, resolving
to an empty string whenever the launcher has not configured a hard limit --
the default, off state. Without this fix, pydant…

- *(ui)* drop role=alert, style the hard-limit warning as a real banner
role=alert had no precedent in this codebase and is the wrong ARIA severity
for a hint that updates on every keystroke -- it would re-announce
assertively each time the typed budget crosses the hard-l…

- *(worker)* make exit 137 terminal under a cgroup hard limit

- *(launcher)* use install_dir_str in current_settings, not the raw mutex
current_settings read app.install_dir directly, which starts None on every
process start and is only populated by status()'s poll via install_dir_str's
disk fallback. Since both effects fire on App mo…

- SRA download progress bar freezing during transfer
fasterq-dump and prefetch redraw their progress bars with a bare `\r`
rather than printing a new `\n`-terminated line each tick. The
subprocess reader (_run_streaming) iterated stdout in text mode, wh…

- *(mcp)* drop the doubled /mcp/mcp segment from the server's mount path
streamable_http_app() defaults its own endpoint path to /mcp, which
stacked with the /api/v1/mcp mount point to make the only reachable
URL /api/v1/mcp/mcp -- a bare /api/v1/mcp 404'd. Pass
streamable…

- show storage location and port as plain text (not editable inputs) in Settings while the stack is Running

- use rsync flags portable to BSD/openrsync, and stop grep-miss silently short-circuiting under pipefail in migrate-storage.sh

- sync in-memory port state after finish_storage_migration, matching apply_settings

- decompress gzip reference/GTF before STAR index build
STAR's genomeGenerate reads --genomeFastaFiles and --sjdbGTFfile as
plain text, unlike every other aligner materialize() feeds, which
accepts gzip transparently. NCBI assemblies are stored gzip-compre…

- close claim.lua's stale mem_free cross-worker admission race (#74)
claim.lua compared a candidate's demand against a free-capacity value the
caller computed one Redis round trip earlier. Redis serializes concurrent
claim.lua executions, but that snapshot argument doe…




















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
