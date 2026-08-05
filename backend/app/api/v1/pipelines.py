"""Pipeline endpoints: launching runs and reporting tool availability."""

from pathlib import Path, PurePosixPath

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import LinkableOwnerDep, OwnerDep
from app.api.v1.jobs import JobOut
from app.config import settings
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import BlobStorage, ObjectRole, ObjectStatus
from app.pipelines import (
    align_runner,
    aligner_registry,
    assembler_registry,
    assembly_qc_registry,
    bam_stats_runner,
    counts_runner,
    de_runner,
    lineage_inference,
    tools,
    variant_db,
)
from app.pipelines.aligners import Aligner
from app.pipelines.assemblers import Assembler
from app.services import object_service, pipeline_service, structure_lookup
from app.storage.paths import blob_path

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


class TrimRequest(BaseModel):
    object_id: PydanticObjectId
    # Omitted means "use the detected mate"; paired=False forces single-end
    # even when one is known, which is the escape hatch for a pair that should
    # not be trimmed together.
    mate_object_id: PydanticObjectId | None = None
    paired: bool = True
    params: dict = Field(default_factory=dict)
    tool: str = "fastp"


class MateSuggestion(BaseModel):
    object_id: str
    name: str
    mate: str | None


@router.get("/tools")
async def list_tools() -> dict:
    """Resolved paths and versions for the external tools.

    Deliberately *not* owner-scoped. This probes the container's filesystem for
    installed binaries -- it describes the image, not anybody's library, and
    the answer is byte-identical for every profile. Requiring a header would
    buy no isolation and would break the launch dialog's availability check for
    a client that has not resolved a profile yet.

    Lets the launch dialog say "fastp is not installed" before a user commits
    to a run, rather than surfacing it as a job that dies minutes later.

    Each entry carries its static description alongside the probe result, so
    the tool selector can explain what a tool is for without a second request.

    `all_available` spans every probed tool, including the optional trimmers.
    That is deliberately coarse and is reported rather than acted on -- no
    caller gates behaviour on it, and a per-pipeline readiness flag should be
    derived from the `pipelines` field rather than added here.
    """
    tools_list = tools.all_tools_with_meta()
    return {
        "tools": tools_list,
        "all_available": all(t["available"] for t in tools_list),
    }


@router.get("/suggestions/{object_id}")
async def list_suggestions(object_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """Pipelines worth offering for this file, with the reason for each.

    Advisory: a card is a pre-answered instance of an operation the
    Computations section also offers with a picker in front of it. Nothing
    here launches anything -- the cards carry the same payloads the dialogs
    post.
    """
    from app.services import suggestion_service

    obj = await object_service.get_object(object_id, owner=owner)

    return {"suggestions": await suggestion_service.suggestions_for(obj)}


@router.get("/defaults")
async def trim_defaults(tool: str = "fastp") -> dict:
    """Default trim parameters for the given tool, owned by the server so the
    form does not encode its own copy.

    Deliberately *not* owner-scoped, like `/tools` above: these are constants
    the runner module declares, keyed by tool name and touching no collection.
    Every profile gets the same numbers because they are a property of the
    code rather than of anyone's library.
    """
    return {
        "params": pipeline_service.default_params(tool),
        "max_threads": settings.pipeline_default_threads,
    }


@router.get("/mate/{object_id}", response_model=MateSuggestion | None)
async def detect_mate(object_id: PydanticObjectId, owner: OwnerDep) -> MateSuggestion | None:
    """The file this one would be trimmed alongside, if any."""
    obj = await object_service.get_object(object_id, owner=owner)

    mate = await pipeline_service.suggest_mate(obj)
    if mate is None:
        return None

    from app.pipelines import pairing

    return MateSuggestion(
        object_id=str(mate.id), name=mate.name, mate=pairing.mate_of(mate.name)
    )


@router.post("/trim", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_trim(body: TrimRequest, owner: OwnerDep) -> JobOut:
    """Queue an adapter-trimming run over a FASTQ file or an R1/R2 pair."""
    job = await pipeline_service.launch_trim(
        object_id=body.object_id,
        owner=owner,
        mate_object_id=body.mate_object_id,
        params=body.params,
        paired=body.paired,
        tool=body.tool,
    )
    return JobOut.of(job)


class QCRequest(BaseModel):
    object_id: PydanticObjectId


@router.post("/qc", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_qc(body: QCRequest, owner: OwnerDep) -> JobOut:
    """Queue a QC run over a FASTQ file. Read-only: produces a report."""
    job = await pipeline_service.launch_qc(object_id=body.object_id, owner=owner)
    return JobOut.of(job)


class SummaryRequest(BaseModel):
    object_id: PydanticObjectId
    # The regenerate button sets this: the numbers have not changed, but the
    # user has asked for another pass anyway.
    force: bool = True


def _is_local(base_url: str) -> bool:
    """Whether this base URL points at something on this machine.

    Governs whether availability is probed live or remembered. `host.docker.
    internal` counts: from inside these containers that *is* the host.
    """
    return any(
        h in base_url
        for h in ("localhost", "127.0.0.1", "host.docker.internal", "0.0.0.0")
    )


def _probe_local(provider) -> bool:
    """Cheap liveness check against a local server: can it list models?

    `/v1/models` rather than the LM Studio-specific `/health` the old client
    used -- one fewer non-standard dependency, and it is the same call the
    settings page's fetch makes.
    """
    from app.services.ai.adapters import Failure, adapter_for

    adapter = adapter_for(
        provider.kind, base_url=provider.base_url, api_key=provider.api_key
    )
    return not isinstance(adapter.list_models(), Failure)


@router.get("/summary/status")
async def summary_status() -> dict:
    """Whether narrative summaries can be produced right now.

    Exists so the UI can hide an affordance that would only fail. The
    availability check differs by provider kind -- see the local/hosted split
    below.

    Deliberately *not* owner-scoped: this reports on a configuration flag and
    the provider routed to FILE_SUMMARY. There is one such routing for the
    whole machine, so the answer cannot differ by profile, and gating it
    behind a header would hide the summarize button from a client that has
    not resolved one rather than telling it the truth.
    """
    import asyncio

    from app.models.ai import TaskSlot
    from app.services.ai import provider_service
    from app.services.ai import router as ai_router

    if not settings.llm_summaries_enabled:
        return {"available": False, "reason": "disabled"}

    provider = await ai_router.resolve(TaskSlot.FILE_SUMMARY)
    if provider is None:
        return {"available": False, "reason": "no_provider"}

    # Local servers are probed live; hosted ones are not. A local model server
    # is a process the user starts and stops by hand, so a remembered answer is
    # the one most likely to be wrong. A hosted provider does not go down --
    # it rejects a stale key -- and that is not worth a network round trip on
    # every page load, nor a billable request.
    if _is_local(provider.base_url):
        alive = await asyncio.to_thread(_probe_local, provider)
        if not alive:
            return {"available": False, "reason": "server_unavailable"}
    else:
        stored = await provider_service.get(provider.provider_id)
        if stored is not None and stored.status == "failed":
            return {
                "available": False,
                "reason": str(stored.status_reason) if stored.status_reason else "failed",
                "provider_name": provider.name,
            }

    return {
        "available": True,
        "model": provider.model or (provider.models_cache[0] if provider.models_cache else None),
        "provider_name": provider.name,
    }


@router.post("/summary", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_summary(body: SummaryRequest, owner: OwnerDep) -> JobOut:
    """Queue a narrative summary of a file's QC data and metadata."""
    job = await pipeline_service.launch_summary(
        object_id=body.object_id, owner=owner, force=body.force
    )
    if job is None:
        # Only reachable on the explicit user path, where returning nothing
        # would read as a dead button. The service's other "no" -- an existing
        # summary of unchanged inputs -- cannot occur here, since the button
        # always forces.
        raise ConflictError(
            "Summaries are disabled or this file has nothing to summarize",
            details={"object_id": str(body.object_id)},
        )
    return JobOut.of(job)


class OrganismBlurbOut(BaseModel):
    organism: str
    text: str
    model: str | None = None


@router.get("/organism/{organism}", response_model=OrganismBlurbOut | None)
async def get_organism_blurb(organism: str, refresh: bool = False) -> OrganismBlurbOut | None:
    """Background prose about a species, from cache or freshly generated.

    Returns null rather than 404 when there is nothing to say -- an unknown
    organism, a disabled feature and a model server that is not running are all
    ordinary states for a decorative field, and none of them is an error the
    client should handle differently.

    Reads are cheap after the first: the text is cached per species, so every
    file of one organism after the first is an indexed document read.

    Deliberately *not* owner-scoped. The cache is keyed on the species name and
    holds generated encyclopedia prose about *E. coli*, not anything the user
    stored -- two profiles looking up the same organism should get the same
    paragraph and share the one generation rather than each paying for their
    own copy of a public fact. The organism name arrives in the path from a
    file the caller already read through a scoped route, so this leaks no
    knowledge of which profiles hold what.
    """
    from app.services import organism_service

    blurb = await organism_service.get_or_generate(organism, force=refresh)
    if blurb is None:
        return None
    return OrganismBlurbOut(organism=blurb.organism, text=blurb.text, model=blurb.model)


@router.get("/qc/report/{object_id}/{report_path:path}")
async def get_qc_report(
    object_id: PydanticObjectId, report_path: str, owner: LinkableOwnerDep
) -> FileResponse:
    """Serve a generated QC report (FastQC or fastp HTML).

    Reports are not content-addressed objects -- they are regenerable
    derivatives -- so they live under qc_reports/ and are served from here
    rather than through the blob routes.

    Takes `LinkableOwnerDep` rather than `OwnerDep`: the frontend opens this
    URL as a plain `<a href target="_blank">`, which never runs the JS that
    attaches `X-BioFlow-Profile`, so the route also accepts `?profile=` --
    see `get_current_owner_linkable`.

    The ownership check is a database lookup even though the bytes come from
    disk. Report directories are named by object id and nothing else, so
    without it the filesystem layout *is* the access rule: any caller holding
    an id could read any profile's report. The object read below is discarded
    -- it is there to make the 404 happen.

    **These pages are not trusted.** FastQC embeds overrepresented sequences
    taken verbatim from the reads, so a crafted FASTQ can put attacker-chosen
    bytes into the HTML. Two things follow, and both are load-bearing:

    - `sandbox` in the CSP drops the page into a unique opaque origin with
      scripting disabled, so it cannot reach this API's session even though it
      is served from this API's origin. `default-src 'none'` stops it fetching
      anything at all. fastp's charts are scripted and will not render under
      this; that is the accepted cost, and the numbers the UI charts come from
      facts rather than from this page.
    - The frontend opens it in a new tab rather than an inline iframe, so the
      report never shares a document with the application.

    NanoPlot is the deliberate exception to the "no scripting" rule above.
    Its report has no static-image fallback at all -- every plot is a Plotly
    figure. The library itself loads from `<script src="https://cdn.plot.ly/...">`,
    but each individual plot is drawn by its own inline `<script>` block that
    calls `Plotly.newPlot(...)` with the plot's data embedded literally --
    there is no nonce or hash on those blocks, so `script-src` needs
    `'unsafe-inline'` too or the library loads but every plot silently stays
    blank (confirmed against a real report: 0 of 7 plots rendered without it,
    all 7 rendered with it). `sandbox` disables scripting outright regardless
    of `script-src`, so there is no way to allow the CDN script and the inline
    calls while keeping `sandbox`; NanoPlot reports drop `sandbox` and add
    those two script allowances instead, same content-derived XSS exposure
    fastp already accepts, just without the erasure. NanoPlot's inline data is
    numeric read-length/quality stats rather than verbatim read sequences, so
    it does not carry the same attacker-chosen-bytes risk as FastQC's
    overrepresented-sequences table.
    """
    # Rejected outright rather than resolved away. The ASGI layer collapses
    # `..` before routing, so a path that reaches here still containing one is
    # not a browser fetching a report -- and relying on that normalization
    # would be relying on a layer whose job is not security. Note what the
    # collapsing does on its own: `/report/AAA/../BBB/x.html` arrives with
    # object_id already rewritten to BBB, so the id in the URL is not by itself
    # evidence of which directory is being read.
    await object_service.get_object(object_id, owner=owner)

    parts = PurePosixPath(report_path).parts
    if any(p in ("..", "") for p in parts) or PurePosixPath(report_path).is_absolute():
        raise NotFoundError(f"No such QC report: {report_path}")

    root = (settings.qc_reports_dir / str(object_id)).resolve()

    # Belt and braces: resolved and re-checked against the root, so a symlink
    # inside the report tree cannot point out of it either. FastQC does not
    # create symlinks, but the check costs a stat and does not depend on that
    # staying true.
    target = (root / report_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise NotFoundError(f"No such QC report: {report_path}")

    if parts[0] == "nanoplot":
        # No `sandbox` here -- see the docstring above. The report itself
        # pins the CDN script with a Subresource Integrity hash and
        # `crossorigin="anonymous"`, so an attacker who compromises the CDN
        # response but not the hash still can't execute; we don't duplicate
        # that hash here because CSP has no equivalent of "require SRI",
        # only "allow this origin". `'unsafe-inline'` is also required in
        # `script-src`: it's what lets each plot's own inline `Plotly.newPlot`
        # call run at all, not just the shared library.
        csp = (
            "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
            "script-src https://cdn.plot.ly 'unsafe-inline'"
        )
    else:
        csp = (
            "sandbox; default-src 'none'; "
            # FastQC's plots are inlined images and its layout is inline
            # CSS, so the report is blank without these two. Neither can
            # execute, which is what the sandbox is there to prevent.
            "img-src 'self' data:; style-src 'unsafe-inline'"
        )

    return FileResponse(
        target,
        headers={
            "Content-Security-Policy": csp,
            "X-Content-Type-Options": "nosniff",
        },
    )


class BamStatsRequest(BaseModel):
    object_id: PydanticObjectId


@router.post("/bamstats", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_bam_stats(body: BamStatsRequest, owner: OwnerDep) -> JobOut:
    """Queue the Results computation for a BAM: coverage, per-contig table,
    binned depth. Read-only: produces facts and one TSV report."""
    job = await pipeline_service.launch_bam_stats(object_id=body.object_id, owner=owner)
    return JobOut.of(job)


@router.get("/bamstats/report/{object_id}/{report_path:path}")
async def get_bam_stats_report(
    object_id: PydanticObjectId,
    report_path: str,
    owner: LinkableOwnerDep,
    download: bool = False,
    offset: int = 0,
    limit: int = 100,
):
    """Serve the per-contig BAM stats report.

    Same containment rules as get_qc_report -- `..` and absolute paths are
    rejected outright, then the resolved path is re-checked against the report
    root, and the object is resolved under the caller's profile first so a
    directory named by object id is not itself the access rule. Unlike a QC
    report, this file is generated by this app from numeric samtools output
    rather than embedding read-derived strings, and it is never rendered as a
    document -- so the sandboxed CSP that HTML report serving needs does not
    apply here.

    Two modes: `?download=1` returns the whole TSV as an attachment; the
    default paginates it as JSON, which is what the Results tab's contig table
    reads from. The paginated mode is fetched by app code and always carries
    the header; `?download=1` is a plain link, hence `LinkableOwnerDep` rather
    than `OwnerDep` -- see `get_current_owner_linkable`.
    """
    await object_service.get_object(object_id, owner=owner)

    parts = PurePosixPath(report_path).parts
    if any(p in ("..", "") for p in parts) or PurePosixPath(report_path).is_absolute():
        raise NotFoundError(f"No such report: {report_path}")

    root = (settings.bam_stats_dir / str(object_id)).resolve()
    target = (root / report_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise NotFoundError(f"No such report: {report_path}")

    if download:
        return FileResponse(
            target,
            media_type="text/tab-separated-values",
            filename=Path(report_path).name,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    text = target.read_text(errors="replace")
    lines = text.splitlines()
    if not lines:
        return {"total": 0, "rows": []}

    header = lines[0].split("\t")
    data_lines = lines[1:]
    total = len(data_lines)
    page = data_lines[offset : offset + limit]

    rows = []
    for line in page:
        values = line.split("\t")
        row: dict = {}
        for col, value in zip(header, values):
            row[col] = bam_stats_runner.coerce_tsv_value(col, value)
        rows.append(row)

    return {"total": total, "rows": rows}


class VcfStatsRequest(BaseModel):
    object_id: PydanticObjectId


@router.post("/vcfstats", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_vcf_stats(body: VcfStatsRequest, owner: OwnerDep) -> JobOut:
    """Queue the Results computation for a VCF: call-set summary statistics
    and the per-variant table. Read-only."""
    job = await pipeline_service.launch_vcf_stats(object_id=body.object_id, owner=owner)
    return JobOut.of(job)


class AnnotateRequest(BaseModel):
    object_id: PydanticObjectId


@router.post("/annotate", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_annotate(body: AnnotateRequest, owner: OwnerDep) -> JobOut:
    """Queue consequence annotation for a called VCF."""
    job = await pipeline_service.launch_annotation(object_id=body.object_id, owner=owner)
    return JobOut.of(job)


@router.get("/vcfstats/variants/{object_id}")
async def get_vcf_stats_variants(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    offset: int = 0,
    limit: int = 100,
    contig: str | None = None,
    pos_min: int | None = None,
    pos_max: int | None = None,
    filter_value: str | None = None,
    variant_type: str | None = None,
    min_qual: float | None = None,
    consequence: str | None = None,
    skip_count: bool = False,
) -> dict:
    """A page of the variant table, filtered.

    `total` is the count *after* filtering, so pagination stays correct. It is
    omitted when `skip_count` is set: a combined qual+filter predicate costs
    ~400ms at 5M rows and cannot use a single index, so the client sends this
    when only the page number changed and the previous total still holds.

    Unlike the BAM per-contig route this reads a database rather than slicing
    a TSV -- a plant VCF holds millions of rows, where read-the-whole-file
    costs ~440 MB of RSS per request.

    The ownership check runs before the path is built, for the same reason the
    report routes do it: `vcf_stats_dir` is laid out by object id alone, so the
    only thing standing between one profile and another profile's called
    variants is this lookup.
    """
    await object_service.get_object(object_id, owner=owner)

    db_path = settings.vcf_stats_dir / str(object_id) / "variants.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    filters = variant_db.VariantFilters(
        contig=contig,
        pos_min=pos_min,
        pos_max=pos_max,
        filter_value=filter_value,
        variant_type=variant_type,
        min_qual=min_qual,
        consequence=consequence,
    )

    rows = variant_db.query_variants(
        db_path=db_path, filters=filters, offset=offset, limit=limit
    )
    total = (
        None
        if skip_count
        else variant_db.count_variants(db_path=db_path, filters=filters)
    )
    return {"total": total, "rows": rows}


@router.get("/vcfstats/structure/{object_id}")
async def get_variant_structure(
    object_id: PydanticObjectId, gene: str, owner: OwnerDep
) -> dict:
    """The protein structure for one gene's variants, if there is one.

    Takes the object rather than a taxid so the VCF -> reference -> organism
    walk stays on the server, where the provenance lives: the client knows the
    gene, not the species it belongs to.

    Never an error for a gene with no structure. Roughly two thirds of resolved
    genes have none, an unknown symbol is indistinguishable to the user from a
    UniProt outage, and all three reach the UI as the same sentence -- so every
    one of them is a 200 with a null accession rather than a status code the
    caller has to branch on.

    Resolved on click rather than per row, which is why the response covers one
    gene: most rows would resolve to nothing, and pre-resolving a page would
    spend dozens of round trips to render buttons that mostly do not fire.
    """
    obj = await object_service.get_object(object_id, owner=owner)

    db_path = settings.vcf_stats_dir / str(object_id) / "variants.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    empty = {"gene": gene, "accession": None, "pdb_ids": [], "length": None}

    # Both of these mean "there is nothing a structure view could show", and
    # neither is worth a request: without a residue the resolver's length guard
    # has nothing to check, and without an organism the query would not be
    # species-scoped, which returns a confidently wrong protein rather than a
    # broader set of right ones.
    max_aa_pos = variant_db.max_residue_for_gene(db_path=db_path, gene=gene)
    if max_aa_pos is None:
        return empty

    taxid = await pipeline_service.taxid_for_vcf(obj)
    if taxid is None:
        return empty

    hit = await structure_lookup.resolve_structure(
        gene=gene, taxid=taxid, max_aa_pos=max_aa_pos
    )
    if hit is None:
        return empty

    return {
        "gene": gene,
        "accession": hit.accession,
        "pdb_ids": hit.pdb_ids,
        "length": hit.length,
    }


@router.get("/vcfstats/report/{object_id}/{report_path:path}")
async def get_vcf_stats_report(
    object_id: PydanticObjectId, report_path: str, owner: LinkableOwnerDep
) -> FileResponse:
    """Serve the downloadable variants TSV.

    Same containment rules as get_bam_stats_report -- the object is resolved
    under the caller's profile, then `..` and absolute paths are rejected
    outright, then the resolved path is re-checked against the report root.

    Always reached via a plain link, never `fetch` -- `LinkableOwnerDep`
    accepts `?profile=` for exactly that reason; see
    `get_current_owner_linkable`.
    """
    await object_service.get_object(object_id, owner=owner)

    parts = PurePosixPath(report_path).parts
    if any(p in ("..", "") for p in parts) or PurePosixPath(report_path).is_absolute():
        raise NotFoundError(f"No such report: {report_path}")

    root = (settings.vcf_stats_dir / str(object_id)).resolve()
    target = (root / report_path).resolve()
    if not target.is_file() or root not in target.parents:
        raise NotFoundError(f"No such report: {report_path}")

    return FileResponse(
        target,
        media_type="text/tab-separated-values",
        filename=Path(report_path).name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


class AlignRequest(BaseModel):
    object_id: PydanticObjectId
    reference_id: PydanticObjectId
    mate_object_id: PydanticObjectId | None = None
    paired: bool = True
    read_group: dict = Field(default_factory=dict)
    params: dict = Field(default_factory=dict)


class BuildIndexRequest(BaseModel):
    reference_id: PydanticObjectId
    aligner: str = "minimap2"
    # STAR-only. Building against a GTF improves splice-junction sensitivity
    # over STAR's own de novo detection; every other aligner has no
    # annotation concept and `launch_build_index` refuses this when set.
    annotation_id: PydanticObjectId | None = None


class VariantRequest(BaseModel):
    bam_id: PydanticObjectId
    # Normally resolved from the BAM's provenance. Supplied only for an
    # uploaded BAM, which carries no record of what it was aligned against.
    reference_id: PydanticObjectId | None = None
    caller: str | None = None
    params: dict = Field(default_factory=dict)


@router.get("/align/defaults/{object_id}")
async def align_defaults(object_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """Defaults for the alignment dialog, including the read group.

    Read-group fields come from the reads' own metadata, so the dialog is
    usually a confirmation rather than data entry -- and the aligner defaults
    to one that is actually installed.

    Scoped despite being a read of mostly-static defaults: the read group is
    built from this file's sample name and platform, which are the user's own
    metadata and not the server's constants.
    """
    obj = await object_service.get_object(object_id, owner=owner)

    return {
        "params": pipeline_service.default_align_params(obj),
        "read_group": pipeline_service.default_read_group(obj),
        # Availability comes from the registry, which is the one place an
        # aligner is declared. The ternary this replaced answered "is minimap2
        # installed" for every aligner that was not bwa-mem2 -- so bowtie2,
        # HISAT2 and STAR were all reported available whenever minimap2 was,
        # and the dialog offered a tool the launch then failed on. The same
        # bug `align_handlers._aligner_tool` was already fixed for; this call
        # site was missed.
        "aligners": [
            {
                "name": a.value,
                "available": aligner_registry.spec_for(a).tool().available,
            }
            for a in Aligner
        ],
        "presets": list(align_runner.Preset.ALL),
    }


@router.get("/aligners/{aligner}/schema")
async def aligner_schema(aligner: str) -> dict:
    """The parameter fields for one aligner, for the dialog to render.

    Served from the registry rather than duplicated in the frontend: two
    copies of a tool's knobs drift, and the frontend copy is the one nobody
    updates when a flag is added.

    Deliberately *not* owner-scoped: the registry is a static description of
    minimap2's and bwa-mem2's flags. It is the same schema for everyone, which
    is exactly what makes it safe to serve without a profile.
    """
    try:
        parsed = Aligner(aligner)
    except ValueError:
        raise NotFoundError(f"Unknown aligner: {aligner}") from None
    return aligner_registry.schema_for(parsed)


class AssembleRequest(BaseModel):
    object_id: PydanticObjectId
    # Omitted by the Actions card, which launches with whatever the server
    # decides; supplied by the dialog when the user has changed something.
    params: dict | None = None


@router.post("/assemble", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_assemble(body: AssembleRequest, owner: OwnerDep) -> JobOut:
    """Queue a de novo assembly of one long-read FASTQ."""
    job = await pipeline_service.launch_assembly(
        object_id=body.object_id, owner=owner, params=body.params
    )
    return JobOut.of(job)


@router.get("/assemble/defaults/{object_id}")
async def assemble_defaults(object_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """What the assemble dialog should open with for these reads.

    Owner-scoped, unlike the assembler schema below, because this reads the
    object *and* walks the project looking for a genome size -- both of which
    are one profile's data.
    """
    from app.services import object_service

    obj = await object_service.get_object(object_id, owner=owner)
    return await pipeline_service.default_assembly_params(obj)


@router.get("/assemblers/{assembler}/schema")
async def assembler_schema(assembler: str) -> dict:
    """The parameter fields for one assembler, for the dialog to render.

    Same reasoning and same non-scoping as `aligner_schema`: a static
    description of a tool's knobs is the same for every profile.
    """
    try:
        parsed = Assembler(assembler)
    except ValueError:
        raise NotFoundError(f"Unknown assembler: {assembler}") from None
    return assembler_registry.schema_for(parsed)


class CompletenessRequest(BaseModel):
    object_id: PydanticObjectId
    # Omitted by the Actions card, which lets the server infer from organism
    # metadata; supplied once the dialog's lineage picker has a value, either
    # the inferred one confirmed or a user override.
    lineage: str | None = None
    odb: str | None = None


@router.post("/completeness", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_completeness_route(body: CompletenessRequest, owner: OwnerDep) -> JobOut:
    """Queue compleasm against one assembly. Read-only: produces facts, no
    derived object."""
    job = await pipeline_service.launch_completeness(
        object_id=body.object_id, owner=owner, lineage=body.lineage, odb=body.odb
    )
    return JobOut.of(job)


@router.get("/completeness/defaults/{object_id}")
async def completeness_defaults(object_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """What the completeness dialog should open with: the lineage inferred
    from organism metadata, and whether that inference is specific or just a
    domain-level guess -- the same "inferred, labelled as inferred,
    overridable" shape the assemble dialog uses for genome size.
    """
    from app.services import object_service

    obj = await object_service.get_object(object_id, owner=owner)
    organism = obj.metadata.get("organism") if obj.metadata else None
    lineage = lineage_inference.infer_lineage(organism)
    return {
        "organism": organism,
        "lineage": lineage,
        "odb": assembly_qc_registry.COMPLEASM_SPEC.odb,
        "specific": lineage_inference.is_specific(lineage) if lineage else False,
    }


class LineageDownloadRequest(BaseModel):
    lineage: str
    odb: str | None = None


@router.post(
    "/completeness/lineage", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def download_lineage_route(body: LineageDownloadRequest, owner: OwnerDep) -> JobOut:
    """Queue fetching one compleasm lineage dataset, a dependency of
    `/completeness` rather than something it fetches inline."""
    job = await pipeline_service.launch_lineage_download(
        lineage=body.lineage, odb=body.odb, owner=owner
    )
    return JobOut.of(job)


@router.get("/completeness/lineage-status")
async def lineage_status(lineage: str, odb: str | None = None) -> dict:
    """Whether a lineage dataset is already downloaded, for the dialog to
    decide whether to show a Download button before Score."""
    from app.queue.lineage_handlers import lineage_present

    odb = odb or assembly_qc_registry.COMPLEASM_SPEC.odb
    present = lineage_present(settings.lineages_dir, lineage, odb)
    return {"lineage": lineage, "odb": odb, "present": present}


class ConsensusRequest(BaseModel):
    bam_object_id: PydanticObjectId
    # Optional: consensus without primer trimming is a legitimate workflow
    # for non-amplicon viral alignments (metagenomic, bait-capture). The
    # reference is never supplied here -- launch_consensus resolves it from
    # the BAM's own provenance, the foundation's (#21) explicit rule.
    primer_bed_object_id: PydanticObjectId | None = None
    min_quality: int | None = None
    min_freq: float | None = None
    min_depth: int | None = None


@router.post("/consensus", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_consensus_route(body: ConsensusRequest, owner: OwnerDep) -> JobOut:
    """Queue an iVar consensus run: primer trimming (if a BED is supplied)
    then consensus calling, against the reference the BAM was aligned to."""
    job = await pipeline_service.launch_consensus(
        bam_object_id=body.bam_object_id,
        primer_bed_object_id=body.primer_bed_object_id,
        owner=owner,
        min_quality=body.min_quality,
        min_freq=body.min_freq,
        min_depth=body.min_depth,
    )
    return JobOut.of(job)


class PolishRequest(BaseModel):
    draft_object_id: PydanticObjectId
    # Optional: omitted, the launch resolves the project's one short-read set
    # and refuses when there is more than one, rather than picking. Polishing
    # with another sample's reads is a silent corruption, not a wrong-looking
    # result, so the ambiguous case has to fail loudly.
    reads_object_id: PydanticObjectId | None = None
    mate_object_id: PydanticObjectId | None = None


@router.post("/polish", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_polish_route(body: PolishRequest, owner: OwnerDep) -> JobOut:
    """Queue a Polypolish run: short reads correcting a draft assembly.

    No alignment is supplied. Polypolish needs every location each read maps
    to, which `align_reads` does not produce, so the job aligns the reads to
    the draft itself."""
    job = await pipeline_service.launch_polish(
        draft_object_id=body.draft_object_id,
        reads_object_id=body.reads_object_id,
        mate_object_id=body.mate_object_id,
        owner=owner,
    )
    return JobOut.of(job)


class ScaffoldRequest(BaseModel):
    draft_object_id: PydanticObjectId
    # Optional, but unlike PolishRequest's reads the ambiguous case is the
    # common one here (a project frequently carries more than one
    # reference-role assembly), so the frontend dialog is expected to always
    # pass this rather than lean on the launch's own fallback.
    reference_object_id: PydanticObjectId | None = None
    divergence: str | None = None


@router.post("/scaffold", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_scaffold_route(body: ScaffoldRequest, owner: OwnerDep) -> JobOut:
    """Queue a RagTag run: order and orient a draft assembly's contigs
    against a reference.

    No alignment is supplied. RagTag invokes minimap2 itself against the
    draft and the reference directly."""
    job = await pipeline_service.launch_scaffold(
        draft_object_id=body.draft_object_id,
        reference_object_id=body.reference_object_id,
        divergence=body.divergence,
        owner=owner,
    )
    return JobOut.of(job)


@router.get("/align-envelope")
async def align_envelope(
    object_id: PydanticObjectId, reference_id: PydanticObjectId, owner: OwnerDep
) -> dict:
    """Everything the dialog needs to estimate memory without a round trip.

    Sent once when the dialog opens; the client then evaluates the same
    arithmetic locally as sliders move. The formula stays in Python -- only
    the coefficients ship -- so there is no second implementation to drift,
    and `launch_alignment` re-runs the authoritative check regardless.

    The host budgets in the response are global, but the input sizes are not:
    reporting a reference's size back to a caller who cannot otherwise see it
    would confirm both that the id exists and roughly how big that genome is.
    """
    return await pipeline_service.align_envelope(
        object_id=object_id, reference_id=reference_id, owner=owner
    )


@router.get("/references/{project_id}")
async def list_references(project_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """Candidate references in a project, each with its index status.

    Index status rides along so the dialog can say "this will build an index
    first" rather than surprising the user with a long job.
    """
    objects = await object_service.list_objects(project_id, owner=owner, limit=500)
    references = [
        o
        for o in objects
        if o.format.kind in pipeline_service.REFERENCE_KINDS
        and o.status is ObjectStatus.READY
    ]

    return {
        "references": [
            {
                "object_id": str(o.id),
                "name": o.name,
                "size": o.size,
                "role": o.role.value if o.role else None,
                "indexes": await pipeline_service.reference_index_status(o),
            }
            for o in references
        ]
    }


@router.get("/annotations/{project_id}")
async def list_annotations(project_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """A project's gene annotations, for the STAR annotated-index option.

    A narrower sibling of `quantify_defaults`, which needs a BAM this dialog
    does not have yet -- the whole point of building an annotated index is
    to have one ready before an alignment exists.
    """
    annotations = await pipeline_service.annotations_for_project(
        project_id, owner=owner
    )
    return {
        "annotations": [
            {"object_id": str(a.id), "name": a.name} for a in annotations
        ]
    }


@router.post("/index", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def build_index(body: BuildIndexRequest, owner: OwnerDep) -> JobOut:
    """Build an aligner index for a reference, eagerly.

    The same job the alignment path queues when an index is missing, so there
    is no second code path to keep correct.
    """
    job = await pipeline_service.launch_build_index(
        reference_id=body.reference_id,
        owner=owner,
        aligner=body.aligner,
        annotation_id=body.annotation_id,
    )
    return JobOut.of(job)


@router.post("/align", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_alignment(body: AlignRequest, owner: OwnerDep) -> JobOut:
    """Queue an alignment, building the reference index first if needed."""
    job = await pipeline_service.launch_alignment(
        object_id=body.object_id,
        reference_id=body.reference_id,
        owner=owner,
        mate_object_id=body.mate_object_id,
        read_group=body.read_group,
        params=body.params,
        paired=body.paired,
    )
    return JobOut.of(job)


@router.get("/variants/defaults/{bam_id}")
async def variant_defaults(bam_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """Defaults for the variant calling dialog.

    Reports the inferred chemistry and the caller it implies, plus whether the
    reference could be resolved -- so the dialog knows to ask for one rather
    than discovering at submit time that it has to.

    Scoped: `reference_name` in the response is the name of a file in the
    caller's library, resolved by walking this BAM's provenance. Serving that
    for a BAM the caller does not own would name another profile's genome.
    """
    obj = await object_service.get_object(bam_id, owner=owner)

    # Resolved once each: the chemistry lookup may read the BAM's parent, and
    # calling it twice to fill two response fields would double that.
    params = await pipeline_service.default_variant_params(obj)
    chemistry = await pipeline_service.read_chemistry_for_alignment(obj)
    reference = await pipeline_service.reference_for_bam(obj)

    return {
        "params": params,
        "caller": params.get("caller"),
        "chemistry": chemistry.value if chemistry else None,
        "reference_id": str(reference.id) if reference else None,
        "reference_name": reference.name if reference else None,
        "needs_reference": reference is None,
        "callers": [
            {
                "name": name,
                "available": probe().available,
            }
            for name, probe in (("clair3", tools.clair3), ("bcftools", tools.bcftools))
        ],
        "max_threads": settings.pipeline_default_threads,
    }


@router.post("/variants", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_variant_calling(body: VariantRequest, owner: OwnerDep) -> JobOut:
    """Queue a variant calling run over an aligned, indexed BAM."""
    job = await pipeline_service.launch_variant_calling(
        bam_id=body.bam_id,
        owner=owner,
        reference_id=body.reference_id,
        caller=body.caller,
        params=body.params,
    )
    return JobOut.of(job)


class QuantifyRequest(BaseModel):
    bam_id: PydanticObjectId
    # Normally resolved from the project. Supplied when a project holds more
    # than one assembly's annotation, which is the case resolve_annotation
    # refuses to guess at.
    annotation_id: PydanticObjectId | None = None
    params: dict = Field(default_factory=dict)


class DifferentialExpressionRequest(BaseModel):
    project_id: PydanticObjectId
    # counts object id -> condition name. A mapping rather than two parallel
    # lists so a malformed request cannot silently pair the wrong sample with
    # the wrong group.
    design: dict[str, str]
    # {"test": "treated", "reference": "control"}. Named rather than ordered
    # because which way round a contrast runs decides the sign of every fold
    # change in the output, and a positional pair gets reversed eventually.
    contrast: dict
    threads: int | None = None


@router.get("/quantify/defaults/{bam_id}")
async def quantify_defaults(bam_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """Defaults for the quantification dialog.

    Both derived values are reported with enough context for the dialog to
    explain itself: `strandedness_source` says whether the value came off the
    alignment or is just the default, because "we read this from your HISAT2
    run" and "we guessed unstranded" deserve different confidence from the
    user, and getting it wrong returns near-zero counts either way.
    """
    obj = await object_service.get_object(bam_id, owner=owner)

    annotations = await pipeline_service.annotations_for_project(
        obj.project_id, owner=owner
    )

    annotation = None
    params: dict = {}
    if annotations:
        try:
            annotation = await pipeline_service.resolve_annotation(
                obj.project_id, None, owner=owner
            )
        except ValidationError:
            # More than one distinct assembly's annotation, which is a choice
            # for the dialog rather than an error to surface here. The list
            # below is what lets it offer them.
            annotation = None

        # Params are computed either way, against the first candidate when the
        # annotation itself is ambiguous. Returning `{}` in that case looked
        # harmless and was not: the dialog rendered its own fallbacks as though
        # they were derived facts, and told the user "this alignment looks
        # single-end" about a BAM with 1.9M properly-paired reads. The server
        # still counted it as paired at launch, so the screen disagreed with
        # the behaviour.
        #
        # Safe to use a candidate the user has not chosen yet, because the two
        # values that come from the *alignment* -- strandedness and pairing --
        # do not depend on which annotation is picked. Only `feature_type` and
        # `attribute` do, and the candidates are GTF-first, so the common pair
        # is what the dialog shows; picking a GFF3 afterwards is re-resolved
        # server-side at launch.
        params = await pipeline_service.default_count_params(
            obj, annotation or annotations[0]
        )

    align_params = (obj.facts or {}).get("align_params") or {}
    inferred = counts_runner.strandedness_for_align_params(align_params)

    return {
        "params": params,
        "annotation_id": str(annotation.id) if annotation else None,
        "annotation_name": annotation.name if annotation else None,
        "needs_annotation": annotation is None,
        "annotations": [
            {"id": str(a.id), "name": a.name, "kind": str(a.format.kind)}
            for a in annotations
        ],
        "strandedness_source": "alignment" if inferred is not None else "default",
        "paired_source": (
            "flagstat"
            if counts_runner.paired_from_facts(obj.facts) is not None
            else "alignment"
        ),
        "available": tools.featurecounts().available,
        "max_threads": settings.pipeline_default_threads,
    }


@router.post("/quantify", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_quantify(body: QuantifyRequest, owner: OwnerDep) -> JobOut:
    """Queue a per-gene count over one aligned BAM."""
    job = await pipeline_service.launch_quantify(
        bam_id=body.bam_id,
        owner=owner,
        annotation_id=body.annotation_id,
        params=body.params,
    )
    return JobOut.of(job)


@router.get("/differential-expression/defaults/{project_id}")
async def differential_expression_defaults(
    project_id: PydanticObjectId, owner: OwnerDep
) -> dict:
    """The samples, conditions and contrast the DE dialog opens with.

    Project-scoped rather than object-scoped, which makes it the only defaults
    endpoint here that is. That is the shape of the operation: differential
    expression is not an action on a file, which is also why it has no
    suggestion card and is launched from the Computations section instead.
    """
    return await pipeline_service.differential_expression_defaults(
        project_id, owner=owner
    )


@router.post(
    "/differential-expression",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
)
async def launch_differential_expression(
    body: DifferentialExpressionRequest, owner: OwnerDep
) -> JobOut:
    """Queue a differential expression test across a project's counts files."""
    job = await pipeline_service.launch_differential_expression(
        project_id=body.project_id,
        owner=owner,
        design=body.design,
        contrast=body.contrast,
        threads=body.threads,
    )
    return JobOut.of(job)


@router.get("/de/results/{object_id}")
async def get_de_results(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    offset: int = 0,
    limit: int = 100,
    sort: str = "padj",
    direction: str = "asc",
    search: str | None = None,
    max_padj: float | None = None,
) -> dict:
    """A page of a differential expression results table.

    Reads and slices the TSV rather than querying a database, unlike the
    variant table next door. The two are not comparable in size: a VCF holds
    millions of rows, where read-the-whole-file costs ~440 MB per request,
    while a DE table holds one row per gene in the annotation -- 6k for yeast,
    ~60k for human -- which is a few megabytes and a few milliseconds. Adding
    a SQLite build step for that would be machinery in search of a problem.

    The ownership check runs first, as in the report routes: it is the only
    thing between one profile and another profile's results.
    """
    obj, blob = await object_service.object_with_blob(object_id, owner=owner)
    if obj.role is not ObjectRole.DE_RESULTS:
        raise NotFoundError("This file is not a differential expression result")
    if blob is None or not obj.blob_sha256:
        raise NotFoundError("Object has no stored content")

    target = (
        Path(blob.external_path)
        if blob.storage is BlobStorage.EXTERNAL and blob.external_path
        else blob_path(obj.blob_sha256)
    )
    if not target.is_file():
        raise NotFoundError(f"Stored content is not available: {obj.name}")

    rows = de_runner.read_results(target)

    if search:
        needle = search.strip().lower()
        rows = [r for r in rows if needle in str(r.get("gene", "")).lower()]
    if max_padj is not None:
        # Genes with no padj are excluded by a threshold rather than kept:
        # "show me significant genes" should not return the ones DESeq2
        # declined to test.
        rows = [
            r for r in rows if r.get("padj") is not None and r["padj"] <= max_padj
        ]

    rows = de_runner.sort_rows(rows, sort, direction)

    return {
        "total": len(rows),
        "rows": rows[offset : offset + limit],
        "offset": offset,
        "limit": limit,
    }
