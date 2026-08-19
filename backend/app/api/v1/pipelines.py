"""Pipeline endpoints: launching runs and reporting tool availability."""

import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import LinkableOwnerDep, OwnerDep
from app.api.v1.jobs import JobOut
from app.config import settings
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import BlobStorage, ObjectRole, ObjectStatus
from app.models.run import AppliedParameterSet
from app.pipelines import (
    align_runner,
    aligner_registry,
    annotation_db,
    annotation_export,
    annotation_hierarchy,
    annotation_window,
    assembler_registry,
    assembly_qc_registry,
    bam_stats_runner,
    counts_runner,
    de_runner,
    lineage_inference,
    sv_db,
    tile_scanner,
    tools,
    variant_db,
)
from app.pipelines.aligners import Aligner
from app.pipelines.assemblers import Assembler
from app.services import object_service, pipeline_service, structure_lookup
from app.storage.paths import blob_path, resolve_report_file

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


@router.post(
    "/tools/{name}/install", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def install_tool(name: str, owner: OwnerDep) -> JobOut:
    """Queue a pull of an on-demand tool's image.

    Owner-scoped, unlike `/tools` above: the job this creates is visible on
    someone's Activity tab, and enqueue's dedup key folds the owner in the
    same way every other launch endpoint's does. The tool itself is not
    profile-scoped -- an install performed by one profile is visible to
    every other, since the image lands in the one Docker daemon they all
    share -- only the job record is.
    """
    from app.services import tool_install_service

    job = await tool_install_service.install(tool_name=name, owner=owner)
    return JobOut.of(job)


@router.delete(
    "/tools/{name}/install", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def uninstall_tool(name: str, owner: OwnerDep) -> JobOut:
    """Queue removal of an on-demand tool's image.

    Offered by the UI wherever Install was, per the design doc's symmetry
    rule -- enforced here, not just in the frontend, so a client cannot
    uninstall a bundled tool or one that was never pulled by calling the
    endpoint directly.
    """
    from app.services import tool_install_service

    job = await tool_install_service.uninstall(tool_name=name, owner=owner)
    return JobOut.of(job)


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


@router.get("/de-summary/status")
async def de_summary_status() -> dict:
    """Whether DE narrative summaries can be produced right now.

    Mirrors /pipelines/summary/status exactly, routed to DE_SUMMARY. See
    that endpoint's docstring for why this is not owner-scoped.
    """
    import asyncio

    from app.models.ai import TaskSlot
    from app.services.ai import provider_service
    from app.services.ai import router as ai_router

    if not settings.llm_summaries_enabled:
        return {"available": False, "reason": "disabled"}

    provider = await ai_router.resolve(TaskSlot.DE_SUMMARY)
    if provider is None:
        return {"available": False, "reason": "no_provider"}

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


@router.post("/de-summary", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_de_summary(body: SummaryRequest, owner: OwnerDep) -> JobOut:
    """Queue a narrative summary of a differential-expression result."""
    job = await pipeline_service.launch_de_summary(
        object_id=body.object_id, owner=owner, force=body.force
    )
    if job is None:
        raise ConflictError(
            "Summaries are disabled or this file has nothing to summarize",
            details={"object_id": str(body.object_id)},
        )
    return JobOut.of(job)


@router.get("/variant-summary/status")
async def variant_summary_status() -> dict:
    """Whether variant-call narrative summaries can be produced right now.

    Mirrors /pipelines/summary/status, routed to VARIANT_SUMMARY.
    """
    import asyncio

    from app.models.ai import TaskSlot
    from app.services.ai import provider_service
    from app.services.ai import router as ai_router

    if not settings.llm_summaries_enabled:
        return {"available": False, "reason": "disabled"}

    provider = await ai_router.resolve(TaskSlot.VARIANT_SUMMARY)
    if provider is None:
        return {"available": False, "reason": "no_provider"}

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


@router.post(
    "/variant-summary", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_variant_summary(body: SummaryRequest, owner: OwnerDep) -> JobOut:
    """Queue a narrative summary of a VCF's call-set statistics."""
    job = await pipeline_service.launch_variant_summary(
        object_id=body.object_id, owner=owner, force=body.force
    )
    if job is None:
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


class FailureExplanationOut(BaseModel):
    text: str
    model: str | None = None


@router.get("/failure-explanation", response_model=FailureExplanationOut | None)
async def get_failure_explanation(code: str, message: str) -> FailureExplanationOut | None:
    """A plain-language explanation of a job error, from cache or freshly
    generated.

    Mirrors get_organism_blurb exactly: returns null rather than 404 when
    there is nothing to say -- no provider configured and a model that
    produced nothing are both ordinary states for this decorative field.

    Deliberately *not* owner-scoped, same reasoning as get_organism_blurb:
    there is one provider routing for the whole machine, and the
    explanation depends only on the error text, not on who is looking at
    it -- two profiles hitting the identical tool crash should share the
    one generation.

    GET with query params rather than the POST-with-body shape
    /pipelines/summary uses: this is a read (cache lookup, generating only
    on a miss) with no side effect the caller directs, matching
    /pipelines/organism/{organism}'s shape more closely than the job-launch
    endpoints'.
    """
    from app.services import failure_explanation_service

    explanation = await failure_explanation_service.get_or_generate(code, message)
    if explanation is None:
        return None
    return FailureExplanationOut(text=explanation.text, model=explanation.model)


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

    QUAST is the second scripting exception, and it needs one more argument
    than NanoPlot's because dropping `sandbox` is exactly what makes an XSS
    exploitable, and this report's data is not numeric like NanoPlot's --
    it is assembly and reference names. QUAST's report has no static
    fallback either: every number lives in a JSON blob inside
    `<div id='total-report-json'>`, rendered into tables by inline script,
    and it renders as a blank page under the FastQC-shaped CSP above
    (confirmed against a real report). Three things make this defensible
    rather than a repeat of the FastQC risk:

    1. **Contig names are sanitized by QUAST itself**
       (`qutils.correct_name`, `re.sub(r'[^\\w\\._\\-]', '_', ...)`) --
       verified against `>ctg_");alert(1);//`, which QUAST rewrites to
       `ctg____alert_1____` before it ever reaches an HTML page.
    2. **The assembly label is not sanitized by QUAST**
       (`qutils.correct_asm_label` only strips and truncates) and is
       otherwise taken from the input filename -- the one gap QUAST leaves
       open, verified by exploiting it: an input named
       `ev<img src=x onerror=alert(7)>.fasta` puts that tag verbatim and
       unescaped into `report.html`. This is closed upstream of this route
       entirely, at the handler: `assess_misassemblies` always links its
       input under a fixed filename and always passes a fixed `-l` label,
       never the object's own name (`app/queue/assembly_qc_handlers.py`).
       That handler-side fix is what this route's safety actually rests on
       -- this docstring records the reasoning, not a second enforcement of
       it, since the object's name is not information this route has any
       way to check against.
    3. **No external origin is permitted at all**, unlike NanoPlot's
       `cdn.plot.ly` allowance -- QUAST inlines everything (verified: the
       only outbound `href` in a real report is a link to QUAST's own
       homepage), so this CSP is strictly tighter than NanoPlot's despite
       both dropping `sandbox`.
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
    elif parts[0] == "quast":
        # No `sandbox` here either -- see the docstring above for the three
        # reasons this is defensible rather than a repeat of the FastQC
        # risk. No external origin at all, unlike NanoPlot: QUAST inlines
        # everything, so this is strictly tighter than the NanoPlot branch
        # above despite both dropping `sandbox`.
        csp = (
            "default-src 'none'; img-src 'self' data:; "
            "style-src 'unsafe-inline'; script-src 'unsafe-inline'"
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


@router.get("/qc/tiles/{object_id}")
async def get_qc_tile_matrix(object_id: PydanticObjectId, owner: OwnerDep) -> dict:
    """Serve the per-tile quality matrix for an object.

    Deliberately not routed through `get_qc_report`, despite serving from the
    same directory. That route wraps its response in `sandbox` +
    `default-src 'none'` because FastQC's HTML embeds sequence bytes taken
    verbatim from the reads -- and that same header set would block the
    `fetch` this endpoint exists to answer. Here the payload is JSON the
    application parses rather than a document the browser renders, so the CSP
    is unnecessary and actively harmful.

    `OwnerDep`, not `LinkableOwnerDep`: this is fetched by the app's own code
    with the profile header attached, never opened as a bare link.

    The object read is discarded -- it is there to make the 404 happen.
    Report directories are named by object id and nothing else, so without it
    any caller holding an id could read any profile's matrix.
    """
    await object_service.get_object(object_id, owner=owner)

    root = (settings.qc_reports_dir / str(object_id)).resolve()
    target = (root / tile_scanner.TILE_MATRIX_FILENAME).resolve()

    # The filename is a module constant rather than user input, so traversal
    # is not reachable through it -- but the resolve-and-recheck costs a stat
    # and does not depend on that staying true.
    if not target.is_relative_to(root) or not target.is_file():
        raise NotFoundError(f"No tile matrix for object {object_id}")

    return json.loads(target.read_text())


class BamStatsRequest(BaseModel):
    object_id: PydanticObjectId


@router.post("/bamstats", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_bam_stats(body: BamStatsRequest, owner: OwnerDep) -> JobOut:
    """Queue the Results computation for a BAM: coverage, per-contig table,
    binned depth. Read-only: produces facts and one TSV report."""
    job = await pipeline_service.launch_bam_stats(object_id=body.object_id, owner=owner)
    return JobOut.of(job)


class TranscriptQcRequest(BaseModel):
    object_id: PydanticObjectId
    gtf_object_id: PydanticObjectId


@router.post(
    "/transcript-qc", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_transcript_qc(body: TranscriptQcRequest, owner: OwnerDep) -> JobOut:
    """Queue RNA-seq transcript QC: gene body coverage and feature
    distribution. Read-only: produces facts only."""
    job = await pipeline_service.launch_transcript_qc(
        object_id=body.object_id, gtf_object_id=body.gtf_object_id, owner=owner
    )
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

    Containment is `resolve_report_file`'s: `..` and absolute paths are
    rejected textually, then the resolved path is re-checked against the report
    root. The object is resolved under the caller's profile first, so a
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

    root = settings.bam_stats_dir / str(object_id)
    target = resolve_report_file(root, report_path)

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
        for col, value in zip(header, values, strict=False):
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

    Containment is `resolve_report_file`'s, the same helper the BAM report
    route uses -- the object is resolved under the caller's profile, then `..`
    and absolute paths are rejected textually, then the resolved path is
    re-checked against the report root.

    Always reached via a plain link, never `fetch` -- `LinkableOwnerDep`
    accepts `?profile=` for exactly that reason; see
    `get_current_owner_linkable`.
    """
    await object_service.get_object(object_id, owner=owner)

    root = settings.vcf_stats_dir / str(object_id)
    target = resolve_report_file(root, report_path)

    return FileResponse(
        target,
        media_type="text/tab-separated-values",
        filename=Path(report_path).name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


class AnnotationStatsRequest(BaseModel):
    object_id: PydanticObjectId


@router.post(
    "/annotationstats", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_annotation_stats(
    body: AnnotationStatsRequest, owner: OwnerDep
) -> JobOut:
    """Queue the Results computation for a GFF/GTF/BED: feature summary and
    the searchable feature table. Read-only."""
    job = await pipeline_service.launch_annotation_stats(
        object_id=body.object_id, owner=owner
    )
    return JobOut.of(job)


class AnnotationExportRequest(BaseModel):
    object_id: PydanticObjectId
    contig: str | None = None
    start_min: int | None = None
    start_max: int | None = None
    feature_type: str | None = None
    biotype: str | None = None
    name_query: str | None = None
    strand: str | None = None
    unresolved: bool = False
    output_name: str | None = None


@router.get("/annotationstats/export-count/{object_id}")
async def get_annotation_export_count(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    contig: str | None = None,
    start_min: int | None = None,
    start_max: int | None = None,
    feature_type: str | None = None,
    biotype: str | None = None,
    name_query: str | None = None,
    strand: str | None = None,
    unresolved: bool = False,
) -> dict:
    """How many features a subset export would contain.

    Separate from the export itself so the UI can show matched-vs-exported
    before anything is queued -- the closure is routinely larger than the
    matched count, and an unexplained difference reads as a bug.
    """
    await object_service.get_object(object_id, owner=owner)

    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    filters = annotation_db.FeatureFilters(
        contig=contig,
        start_min=start_min,
        start_max=start_max,
        feature_type=feature_type,
        biotype=biotype,
        name_query=name_query,
        strand=strand,
        top_level_only=False,
        parent_status=(
            annotation_hierarchy.UNRESOLVED_STATUSES if unresolved else None
        ),
    )
    return {
        "matched": annotation_db.count_features(db_path=db_path, filters=filters),
        "exported": len(
            annotation_export.closure_lines(db_path=db_path, filters=filters)
        ),
    }


class GenBankSequenceRequest(BaseModel):
    object_id: PydanticObjectId


@router.get("/genbanksequence/{object_id}")
async def get_extracted_sequence(
    object_id: PydanticObjectId, owner: OwnerDep
) -> dict:
    """The reference already extracted from this GenBank, or nulls.

    The same query the launcher's guard runs, exposed so the Results tab's
    control and the launcher cannot disagree about whether extraction has
    happened (GS-25).
    """
    await object_service.get_object(object_id, owner=owner)
    existing = await pipeline_service.existing_extracted_sequence(object_id)
    if existing is None:
        return {"reference_id": None, "reference_name": None}
    return {
        "reference_id": str(existing.id),
        "reference_name": existing.name,
    }


@router.post(
    "/genbanksequence", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_extract_genbank_sequence(
    body: GenBankSequenceRequest, owner: OwnerDep
) -> JobOut:
    """Queue extraction of a GenBank's ORIGIN sequence into a FASTA reference."""
    job = await pipeline_service.launch_extract_genbank_sequence(
        object_id=body.object_id, owner=owner
    )
    return JobOut.of(job)


@router.post(
    "/annotationstats/export", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_annotation_export(
    body: AnnotationExportRequest, owner: OwnerDep
) -> JobOut:
    """Queue a subset export using the filters the table is displaying."""
    if body.output_name and (
        body.output_name in {".", ".."}
        or Path(body.output_name).name != body.output_name
    ):
        raise ValidationError(
            "output_name must be a bare filename, not a path",
            details={"output_name": body.output_name},
        )

    db_path = settings.annotation_stats_dir / str(body.object_id) / "features.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    filters = {
        "contig": body.contig,
        "start_min": body.start_min,
        "start_max": body.start_max,
        "feature_type": body.feature_type,
        "biotype": body.biotype,
        "name_query": body.name_query,
        "strand": body.strand,
    }
    if body.unresolved:
        filters["parent_status"] = list(annotation_hierarchy.UNRESOLVED_STATUSES)

    job = await pipeline_service.launch_annotation_export(
        object_id=body.object_id,
        owner=owner,
        filters=filters,
        output_name=body.output_name or f"{body.object_id}.subset.gff3",
    )
    return JobOut.of(job)


@router.get("/annotationstats/features/{object_id}")
async def get_annotation_features(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    offset: int = 0,
    limit: int = 100,
    contig: str | None = None,
    start_min: int | None = None,
    start_max: int | None = None,
    feature_type: str | None = None,
    biotype: str | None = None,
    name_query: str | None = None,
    strand: str | None = None,
    skip_count: bool = False,
    view: Literal["all", "unresolved"] = "all",
) -> dict:
    """A page of the feature table, filtered.

    Rows are top-level features by default -- a GFF3's genes rather than its
    three million exons -- and children are fetched per-parent by the sibling
    route when a row is expanded.

    `total` is the count *after* filtering, so pagination stays correct. It is
    omitted when `skip_count` is set, the same trade the variant route
    documents: a combined predicate cannot use a single index, so the client
    sends this when only the page number changed.

    The ownership check runs before the path is built, for the same reason the
    variant routes do it: `annotation_stats_dir` is laid out by object id
    alone, so the only thing standing between one profile and another
    profile's annotations is this lookup.
    """
    await object_service.get_object(object_id, owner=owner)

    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    # The Unresolved view is the one place a record whose Parent named
    # nothing is reachable -- it is excluded from the default page by
    # definition, since its parent is not NULL.
    unresolved = view == "unresolved"
    filters = annotation_db.FeatureFilters(
        contig=contig,
        start_min=start_min,
        start_max=start_max,
        feature_type=feature_type,
        biotype=biotype,
        name_query=name_query,
        strand=strand,
        # A type filter must search the whole file: every exon has a parent,
        # so leaving top_level_only set would return an empty table on a
        # valid GFF3. The Unresolved view clears it for the same reason.
        top_level_only=feature_type is None and not unresolved,
        parent_status=(
            annotation_hierarchy.UNRESOLVED_STATUSES if unresolved else None
        ),
    )

    rows = annotation_db.query_features(
        db_path=db_path, filters=filters, offset=offset, limit=limit
    )
    total = (
        None
        if skip_count
        else annotation_db.count_features(db_path=db_path, filters=filters)
    )
    return {"total": total, "rows": rows}


@router.get("/annotationstats/genes/{object_id}")
async def get_annotation_genes(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    offset: int = 0,
    limit: int = 100,
    skip_count: bool = False,
) -> dict:
    """A page of the Genes view.

    Its own route rather than a third `view` value: genes page over a
    different table with a different row shape (child and descendant counts,
    a span), so folding it into the features route would mean one endpoint
    returning two row types.

    `mode` tells the client whether these are gene-typed features or the
    root fallback, which the UI states rather than leaving implied.
    """
    await object_service.get_object(object_id, owner=owner)

    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    rows = annotation_hierarchy.query_genes(
        db_path=db_path, offset=offset, limit=limit
    )
    total = (
        None if skip_count else annotation_hierarchy.count_genes(db_path=db_path)
    )
    return {
        "total": total,
        "rows": rows,
        "mode": annotation_hierarchy.gene_mode(db_path=db_path),
    }


@router.get("/annotationstats/children/{object_id}")
async def get_annotation_children(
    object_id: PydanticObjectId, parent_id: str, owner: OwnerDep
) -> dict:
    """Every child of one feature. Unpaged: a transcript has tens of exons."""
    await object_service.get_object(object_id, owner=owner)

    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    return {
        "rows": annotation_db.children_of(db_path=db_path, parent_id=parent_id),
        "depth_cap": annotation_hierarchy.DEPTH_CAP,
    }


class AdditionalReadSetIn(BaseModel):
    """One additional read set: an R1 and, when paired, its mate.

    A set's pairing is decided by the run's primary pair: in a paired run
    every set must carry a mate (or have one resolvable by the service), and
    in a single-end run no set may declare one. See launch_alignment.
    """

    object_id: PydanticObjectId
    mate_object_id: PydanticObjectId | None = None


# Top-level features in a window above which the viewer draws a density band
# instead of individual features. A legibility judgement, not a performance
# limit: at a typical section width this is ~2px per feature, below which
# boxes stop being separable. Expected to be tuned against a real GFF3.
ANNOTATION_DENSITY_THRESHOLD = 500

# A client asking for a million bins must not make the server build a million
# rows; 10 is below any width worth drawing.
_MIN_BINS, _MAX_BINS = 10, 1000


@router.get("/annotationstats/window/{object_id}")
async def get_annotation_window(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    contig: str,
    start: int,
    end: int,
    bins: int = 600,
    feature_type: str | None = None,
    biotype: str | None = None,
    strand: str | None = None,
) -> dict:
    """Features overlapping a coordinate window, or their density.

    Two response shapes behind one route, chosen server-side by a count: a
    client cannot know how dense a region is before asking, and making it
    ask twice would double the round trips on every pan.

    `mode` distinguishes them rather than which key is present, so an empty
    region cannot be mistaken for a dense one. The window is echoed back
    because panning issues overlapping requests that can return out of
    order -- a response with no record of what it answers cannot be matched
    to the current viewport.

    Ownership is checked before the path is built, for the same reason the
    sibling routes do it: annotation_stats_dir is laid out by object id
    alone, so this lookup is the only thing separating one profile's
    annotations from another's.
    """
    await object_service.get_object(object_id, owner=owner)

    if end < start:
        raise ValidationError(
            "end must not be before start",
            details={"start": start, "end": end},
        )

    db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    common = {"contig": contig, "start": start, "end": end}
    total = annotation_db.count_in_window(db_path=db_path, **common)

    if total >= ANNOTATION_DENSITY_THRESHOLD:
        counts = annotation_db.bin_counts(
            db_path=db_path,
            bins=max(_MIN_BINS, min(int(bins), _MAX_BINS)),
            **common,
        )
        # Derived from the returned array rather than recomputed, so the
        # width reported to the client cannot disagree with the binning that
        # actually happened.
        span = max(1, end - start)
        return {
            "mode": "binned",
            **common,
            "bin_bases": -(-span // max(1, len(counts))),
            "counts": counts,
            "total": total,
        }

    features = annotation_db.features_in_window(
        db_path=db_path,
        feature_type=feature_type,
        biotype=biotype,
        strand=strand,
        **common,
    )
    rows = annotation_window.pack_rows(
        [(f["start"], f["end"]) for f in features]
    )
    for feature, row in zip(features, rows, strict=True):
        feature["row"] = row

    return {
        "mode": "features",
        **common,
        "features": [f for f in features if f["row"] is not None],
        "truncated_rows": sum(1 for r in rows if r is None),
        "total": total,
    }


class AlignRequest(BaseModel):
    object_id: PydanticObjectId
    reference_id: PydanticObjectId
    mate_object_id: PydanticObjectId | None = None
    # Ordered additional read sets, each a sibling of the primary pair rather
    # than a flat list: a set is an R1 and optionally its mate, and the whole
    # run shares one pairing mode.
    additional_read_sets: list[AdditionalReadSetIn] = Field(default_factory=list)
    paired: bool = True
    read_group: dict = Field(default_factory=dict)
    params: dict = Field(default_factory=dict)
    # "Launch anyway" from the refusal card. Skips the enqueue-time BLOCK and
    # persists on the job, where claim.lua admits it only as sole occupant.
    resource_override: bool = False
    from_parameter_set: AppliedParameterSet | None = None


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
    # Consent to a multi-gigabyte on-demand-tool download. False by default,
    # so a first request against a not-yet-installed optional caller refuses
    # naming the size rather than silently starting a pull nobody agreed to
    # pay for; the dialog re-posts with this set once the user has actually
    # seen and accepted that number.
    install_optional: bool = False
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


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
    # "Launch anyway" from the refusal card. Skips the enqueue-time BLOCK and
    # persists on the job, where claim.lua admits it only as sole occupant.
    resource_override: bool = False
    from_parameter_set: AppliedParameterSet | None = None


@router.post("/assemble", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_assemble(body: AssembleRequest, owner: OwnerDep) -> JobOut:
    """Queue a de novo assembly of one long-read FASTQ."""
    job = await pipeline_service.launch_assembly(
        object_id=body.object_id,
        owner=owner,
        params=body.params,
        resource_override=body.resource_override,
        from_parameter_set=body.from_parameter_set,
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
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


@router.post("/completeness", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_completeness_route(body: CompletenessRequest, owner: OwnerDep) -> JobOut:
    """Queue compleasm against one assembly. Read-only: produces facts, no
    derived object."""
    job = await pipeline_service.launch_completeness(
        object_id=body.object_id,
        owner=owner,
        lineage=body.lineage,
        odb=body.odb,
        resource_override=body.resource_override,
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


class GcTracksRequest(BaseModel):
    object_id: PydanticObjectId


@router.post("/gc-tracks", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_gc_tracks_route(body: GcTracksRequest, owner: OwnerDep) -> JobOut:
    """Queue GC content and skew track computation for one assembly.
    Read-only: produces facts, no derived object."""
    job = await pipeline_service.launch_gc_tracks(
        object_id=body.object_id, owner=owner,
    )
    return JobOut.of(job)


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
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


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
        resource_override=body.resource_override,
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
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


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
        resource_override=body.resource_override,
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
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


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
        resource_override=body.resource_override,
    )
    return JobOut.of(job)


class MisassemblyQcRequest(BaseModel):
    draft_object_id: PydanticObjectId
    # Optional, same reasoning ScaffoldRequest gives: a project holding more
    # than one reference-role assembly is the ordinary case, so the frontend
    # dialog is expected to always pass this rather than lean on the
    # launch's own single-candidate fallback. The Actions card never needs
    # to supply it -- it only offers this pipeline when exactly one
    # candidate exists.
    reference_object_id: PydanticObjectId | None = None
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


@router.post("/misassemblies", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_misassembly_qc_route(
    body: MisassemblyQcRequest, owner: OwnerDep
) -> JobOut:
    """Queue a QUAST run: reference-based misassembly QC for one assembly.

    Read-only, like /completeness: produces facts, no derived object."""
    job = await pipeline_service.launch_misassembly_qc(
        draft_object_id=body.draft_object_id,
        reference_object_id=body.reference_object_id,
        owner=owner,
        resource_override=body.resource_override,
    )
    return JobOut.of(job)


class SyntenyRequest(BaseModel):
    draft_object_id: PydanticObjectId
    # Optional, same reasoning MisassemblyQcRequest's reference_object_id
    # gives: a project holding more than one reference-role assembly is the
    # ordinary case, so the frontend dialog is expected to always pass this
    # rather than lean on the launch's own single-candidate fallback.
    reference_object_id: PydanticObjectId | None = None
    divergence: str | None = None
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


@router.post("/synteny", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_synteny_route(body: SyntenyRequest, owner: OwnerDep) -> JobOut:
    """Queue a minimap2 run: whole-genome synteny alignment of a draft
    assembly against a reference, for a synteny dot-plot.

    Read-only, like /misassemblies: produces facts, no derived object."""
    job = await pipeline_service.launch_synteny(
        draft_object_id=body.draft_object_id,
        reference_object_id=body.reference_object_id,
        divergence=body.divergence,
        owner=owner,
        resource_override=body.resource_override,
    )
    return JobOut.of(job)


class AssemblyErrorRequest(BaseModel):
    object_id: PydanticObjectId
    # Both optional, same reasoning MisassemblyQcRequest's reference_object_id
    # gives: the Actions card only fires when auto-pairing is unambiguous
    # (at most one short-read and one long-read BAM against this assembly),
    # so it never needs to supply these. A dialog handling the ambiguous
    # case names them explicitly.
    ngs_bam_id: PydanticObjectId | None = None
    sms_bam_id: PydanticObjectId | None = None
    break_chimera: bool = False
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


@router.post("/assembly-errors", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_assembly_error_qc_route(
    body: AssemblyErrorRequest, owner: OwnerDep
) -> JobOut:
    """Queue a CRAQ run: reference-free assembly error detection.

    Read-only unless `break_chimera` is set, like /misassemblies produces no
    derived object by default."""
    job = await pipeline_service.launch_assembly_error_qc(
        object_id=body.object_id,
        owner=owner,
        ngs_bam_id=body.ngs_bam_id,
        sms_bam_id=body.sms_bam_id,
        break_chimera=body.break_chimera,
        resource_override=body.resource_override,
    )
    return JobOut.of(job)


class AssemblyQvRequest(BaseModel):
    object_id: PydanticObjectId
    # Both optional, same reasoning AssemblyErrorRequest gives: the Actions
    # card only fires when exactly one read set exists in the project, so it
    # never needs to supply these. A dialog handling the ambiguous case (or a
    # user wanting a non-default k) names them explicitly.
    read_object_id: PydanticObjectId | None = None
    k: int | None = None
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


@router.post("/assembly-qv", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_assembly_qv_route(body: AssemblyQvRequest, owner: OwnerDep) -> JobOut:
    """Queue a Merqury run: reference-free k-mer base accuracy (QV) for one
    assembly, scored against the reads it came from.

    Read-only: produces facts on the assembly plus a cached k-mer database on
    the read object, no derived object -- like /assembly-errors and
    /misassemblies."""
    job = await pipeline_service.launch_qv_qc(
        body.object_id,
        owner=owner,
        read_object_id=body.read_object_id,
        k=body.k,
        resource_override=body.resource_override,
    )
    return JobOut.of(job)


class MerylAnalysisRequest(BaseModel):
    object_id: PydanticObjectId
    # Both optional, same reasoning AssemblyQvRequest gives above: the
    # kmer_spectra card names the read set because it only fires when one is
    # unambiguous, and the repeat_density card sends neither -- it asks about
    # the assembly alone and lets the service auto-pick.
    read_object_id: PydanticObjectId | None = None
    k: int | None = None
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


@router.post(
    "/meryl-analysis", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_meryl_analysis_route(
    body: MerylAnalysisRequest, owner: OwnerDep
) -> JobOut:
    """Queue meryl k-mer spectra and repeat-density analysis for one assembly.

    Read-only, like /gc-tracks and /assembly-qv: produces facts on the
    assembly, no derived object. One job covers both the kmer_spectra and
    repeat_density cards, since the handler runs both analyses together."""
    job = await pipeline_service.launch_meryl_analysis(
        object_id=body.object_id,
        owner=owner,
        read_object_id=body.read_object_id,
        k=body.k,
        resource_override=body.resource_override,
    )
    return JobOut.of(job)


class AssemblyContinuityRequest(BaseModel):
    object_id: PydanticObjectId
    # Both default empty, same reasoning as AssemblyErrorRequest's ngs_bam_id
    # / sms_bam_id: the Actions card only fires when auto-pairing is
    # unambiguous, so it never needs to supply these. Lists, not single ids,
    # because two aligners (minimap2 and, when installed, winnowmap) make
    # two BAMs in one slot the routine case for GCI's own cross-check
    # recommendation -- see pipeline_service.launch_continuity_qc. A dialog
    # handling the genuinely ambiguous case (two BAMs from the *same*
    # aligner), or a CLR-only project, names them explicitly.
    hifi_bam_ids: list[PydanticObjectId] = []
    nano_bam_ids: list[PydanticObjectId] = []
    map_qual: int | None = None
    plot: bool | None = None
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


@router.post("/assembly-continuity", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_assembly_continuity_route(
    body: AssemblyContinuityRequest, owner: OwnerDep
) -> JobOut:
    """Queue a GCI run: long-read assembly continuity inspection.

    Read-only, like /assembly-errors -- produces facts, no derived object."""
    job = await pipeline_service.launch_continuity_qc(
        object_id=body.object_id,
        owner=owner,
        hifi_bam_ids=body.hifi_bam_ids,
        nano_bam_ids=body.nano_bam_ids,
        map_qual=body.map_qual,
        plot=body.plot,
        resource_override=body.resource_override,
    )
    return JobOut.of(job)


class AnnotateGenomeRequest(BaseModel):
    object_id: PydanticObjectId
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


@router.post("/annotate-genome", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_annotate_genome_route(
    body: AnnotateGenomeRequest, owner: OwnerDep
) -> JobOut:
    """Queue a Bakta genome annotation for one bacterial assembly.

    Read-only, like /gc-tracks and /meryl-tracks — produces facts (gene
    density) merged onto the assembly, no derived object.  The GFF3 and
    GenBank files are stored as pipeline artifacts alongside the job log."""
    job = await pipeline_service.launch_annotate_genome(
        object_id=body.object_id,
        owner=owner,
        resource_override=body.resource_override,
    )
    return JobOut.of(job)


@router.get("/align-envelope")
async def align_envelope(
    object_id: PydanticObjectId, reference_id: PydanticObjectId, owner: OwnerDep,
    chunked: bool = False,
) -> dict:
    """Everything the dialog needs to estimate memory without a round trip.

    Sent once when the dialog opens; the client then evaluates the same
    arithmetic locally as sliders move. The formula stays in Python -- only
    the coefficients ship -- so there is no second implementation to drift,
    and `launch_alignment` re-runs the authoritative check regardless.

    When `chunked=true`, includes bucket-planning preview for the chunked
    alignment path.
    """
    return await pipeline_service.align_envelope(
        object_id=object_id, reference_id=reference_id, owner=owner,
        chunked=chunked,
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
                # So the dialog can warn that picking this reference means a
                # download first, rather than surprising the user with a job
                # that sits waiting on gigabytes of transfer.
                "locality": o.locality.value,
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
        # Services don't import API models; the route converts its validated
        # entries into the service's tuple contract.
        additional_read_sets=[
            (s.object_id, s.mate_object_id) for s in body.additional_read_sets
        ],
        read_group=body.read_group,
        params=body.params,
        paired=body.paired,
        resource_override=body.resource_override,
        from_parameter_set=body.from_parameter_set,
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
        install_optional=body.install_optional,
        resource_override=body.resource_override,
    )
    return JobOut.of(job)


class StructuralVariantRequest(BaseModel):
    # Keyed on bam_id, matching /pipelines/variants -- both take an
    # alignment rather than a generic object.
    bam_id: PydanticObjectId
    params: dict = {}
    # "Launch anyway" from the refusal card. Skips the enqueue-time BLOCK and
    # persists on the job, where claim.lua admits it only as sole occupant.
    resource_override: bool = False


class MergeStructuralVariantsRequest(BaseModel):
    snf_object_ids: list[PydanticObjectId]
    output_name: str | None = None
    resource_override: bool = False


@router.post(
    "/structural_variants", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_structural_variant_calling(
    body: StructuralVariantRequest, owner: OwnerDep
) -> JobOut:
    """Queue a Sniffles2 structural variant calling run over an aligned,
    indexed long-read BAM."""
    job = await pipeline_service.launch_structural_variant_calling(
        bam_id=body.bam_id,
        params=body.params,
        owner=owner,
        resource_override=body.resource_override,
    )
    return JobOut.of(job)


@router.post(
    "/merge_structural_variants", response_model=JobOut, status_code=status.HTTP_201_CREATED
)
async def launch_merge_structural_variants(
    body: MergeStructuralVariantsRequest, owner: OwnerDep
) -> JobOut:
    """Queue a Sniffles2 --combine run to merge per-sample .snf callsets into a joint VCF."""
    job = await pipeline_service.launch_merge_structural_variants(
        snf_object_ids=body.snf_object_ids,
        owner=owner,
        output_name=body.output_name,
        resource_override=body.resource_override,
    )
    return JobOut.of(job)


@router.get("/structural_variants/svs/{object_id}")
async def get_structural_variants(
    object_id: PydanticObjectId,
    owner: OwnerDep,
    offset: int = 0,
    limit: int = 100,
    contig: str | None = None,
    pos_min: int | None = None,
    pos_max: int | None = None,
    svtype: str | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    filter_value: str | None = None,
    min_qual: float | None = None,
    skip_count: bool = False,
) -> dict:
    """A page of the SV table, filtered.

    Mirrors `get_vcf_stats_variants`: `object_id` is the SV VCF's own id, and
    the database it reads is keyed the same way `vcf_stats_dir` keys its
    variants.db -- `sv_stats_dir/<object_id>/sv.db` -- not by the source BAM.
    `total` is the count *after* filtering, and is omitted when `skip_count`
    is set so a page turn need not re-run the count query.
    """
    await object_service.get_object(object_id, owner=owner)

    db_path = settings.sv_stats_dir / str(object_id) / "sv.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    filters = sv_db.SvFilters(
        contig=contig,
        pos_min=pos_min,
        pos_max=pos_max,
        svtype=svtype,
        min_length=min_length,
        max_length=max_length,
        filter_value=filter_value,
        min_qual=min_qual,
    )

    rows = sv_db.query_svs(db_path, filters, limit=limit, offset=offset)
    total = None if skip_count else sv_db.count_svs(db_path, filters)
    return {"total": total, "rows": rows}


@router.get("/structural_variants/summary/{object_id}")
async def get_structural_variant_summary(
    object_id: PydanticObjectId, owner: OwnerDep
) -> dict:
    """The SV type breakdown and the log-binned length histogram for one SV
    VCF's callset -- the two summary views the results page charts, computed
    over the whole callset rather than the current page of filtered rows."""
    await object_service.get_object(object_id, owner=owner)

    db_path = settings.sv_stats_dir / str(object_id) / "sv.db"
    if not db_path.exists():
        raise NotFoundError(
            "No computed results for this file. Compute results first."
        )

    return {
        "type_counts": sv_db.type_counts(db_path),
        "length_histogram": sv_db.length_histogram(db_path),
        "samples": sv_db.sample_names(db_path),
    }



class QuantifyRequest(BaseModel):
    bam_id: PydanticObjectId
    # Normally resolved from the project. Supplied when a project holds more
    # than one assembly's annotation, which is the case resolve_annotation
    # refuses to guess at.
    annotation_id: PydanticObjectId | None = None
    params: dict = Field(default_factory=dict)
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


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
    # "Launch anyway" from the refusal card. Skips the declared-budget refusal
    # and persists on the job, where claim.lua admits it as sole occupant.
    resource_override: bool = False


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
        resource_override=body.resource_override,
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
        resource_override=body.resource_override,
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
    object_service.check_local(obj, verb="read")
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


# ---------------------------------------------------------------------------
# Annotation edit routes — issue #297
# ---------------------------------------------------------------------------

# Editable columns for GFF3 and GTF annotations.
# 0-based column index for each editable field in a GFF/GTF line (9 columns,
# tab-delimited). Only GFF and GTF are supported; BED and GenBank are
# excluded from the editing affordance.
_FIELD_TO_COL: dict[str, int] = {
    "source": 1,
    "type": 2,
    "start": 3,
    "end": 4,
    "attributes": 8,
}

_EDITABLE_FIELDS: set[str] = set(_FIELD_TO_COL)

# Identity keys whose values must not change on an attributes edit.
# ED-3: changing ID or Parent (GFF) / gene_id or transcript_id (GTF) would
# break the hierarchy, so these are locked.
_GFF_ID_KEYS: tuple[str, ...] = ("ID", "Parent")
_GTF_ID_KEYS: tuple[str, ...] = ("gene_id", "transcript_id")


def _validate_edit_value(
    *,
    new_value: str,
    field: str,
    fmt: str,
    old_attributes_line: str | None,
) -> str | None:
    """Validate an edit value before saving. Returns error string or None.

    Validates basic constraints (no tab/newline), field-specific rules
    (positive integer for coordinates, non-empty type, valid attribute syntax),
    and identity-key protection for attributes.
    """
    if "\t" in new_value or "\n" in new_value:
        return "Tab and newline characters are not allowed in annotation fields"

    if field == "start" or field == "end":
        try:
            n = int(new_value)
        except ValueError:
            return f"{field} must be a positive integer"
        if n <= 0:
            return f"{field} must be a positive integer"

    elif field == "type":
        if not new_value.strip():
            return "type must not be empty"

    elif field == "attributes":
        attr_err = _check_attributes(new_value, fmt)
        if attr_err:
            return attr_err
        # ED-3: identity keys must not change.
        id_err = _check_identity_keys(new_value, old_attributes_line or ".", fmt)
        if id_err:
            return id_err

    # source: no special validation beyond the tab/newline check above.

    return None


def _check_attributes(attr_str: str, fmt: str) -> str | None:
    """Return an error string if the attribute value is malformed, else None.

    The lenient parsers (parse_gff_attributes/parse_gtf_attributes) skip bad
    pairs rather than raising, so they cannot be reused as a validity check --
    "not a valid attribute string" would slip through as `{}`. This checks
    that every non-empty ;-separated pair carries the format's separator
    (`=` for GFF, a space before the value for GTF) with a non-empty key.
    """
    if not attr_str or attr_str == ".":
        return None
    for pair in attr_str.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        if fmt == "gff":
            if "=" not in pair or not pair.split("=", 1)[0].strip():
                return "Attribute value must be valid GFF3 (key=value;…) syntax"
        else:  # gtf
            if " " not in pair or not pair.split(" ", 1)[0].strip():
                return 'Attribute value must be valid GTF (key "value";…) syntax'
    return None


def _check_identity_keys(new_attrs: str, old_attrs: str, fmt: str) -> str | None:
    """Return error if identity keys changed between old and new attributes."""
    from app.pipelines.annotation_parse import (
        parse_gff_attributes,
        parse_gtf_attributes,
    )

    keys = _GFF_ID_KEYS if fmt == "gff" else _GTF_ID_KEYS
    parser = parse_gff_attributes if fmt == "gff" else parse_gtf_attributes

    old_parsed = parser(old_attrs) if old_attrs and old_attrs != "." else {}
    new_parsed = parser(new_attrs) if new_attrs and new_attrs != "." else {}

    for key in keys:
        old_val = old_parsed.get(key)
        new_val = new_parsed.get(key)
        if old_val != new_val:
            return (
                f"Cannot change the '{key}' attribute — it is an identity key "
                f"that would break the annotation hierarchy"
            )
    return None


async def _resolve_annotation_path(ann) -> str:
    """Resolve an annotation object's file path for reading."""
    from app.services.pipeline_service import _resolve_readable
    from app.storage.paths import blob_path

    digest, path = await _resolve_readable(ann)
    return path or str(blob_path(digest))


def _read_source_column(source_path: str, line_no: int, field: str) -> str:
    """Read the current value of `field` at `line_no` from the source file.

    Only works for GFF/GTF (tab-delimited). line_no is 1-based.
    """
    from app.queue.annotation_handlers import _open_text

    col_idx = _FIELD_TO_COL[field]
    with _open_text(Path(source_path)) as fh:
        for i, raw in enumerate(fh, start=1):
            if i == line_no:
                stripped = raw.rstrip("\n")
                columns = stripped.split("\t")
                if col_idx >= len(columns):
                    raise ValidationError(
                        f"line {line_no} has fewer columns than expected; "
                        f"it may not be a valid annotation line"
                    )
                return columns[col_idx]
            if i > line_no:
                break
    raise NotFoundError(f"line {line_no} not found in the source file")


async def _effective_coord(
    db_path: Path, object_id: PydanticObjectId, line: int, coord_field: str
) -> int:
    """Get the effective coordinate value, accounting for pending edits."""
    from app.models.annotation_edit import AnnotationEdit

    # Check pending edit first.
    edit = await AnnotationEdit.find_one(
        AnnotationEdit.object_id == object_id,
        AnnotationEdit.line == line,
        AnnotationEdit.field == coord_field,
    )
    if edit:
        return int(edit.new_value)

    # Fall back to the index.
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            f"SELECT {coord_field} FROM features WHERE line = ? LIMIT 1",
            (line,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"line {line} not found in the annotation index")
        return row[0]
    finally:
        con.close()


class AnnotationEditRequest(BaseModel):
    line: int
    field: str
    new_value: str


class AnnotationEditOut(BaseModel):
    line: int
    field: str
    old_value: str | None
    new_value: str


class AnnotationMaterializeRequest(BaseModel):
    object_id: PydanticObjectId


@router.get("/annotationstats/edits/{object_id}")
async def get_annotation_edits(
    object_id: PydanticObjectId, owner: OwnerDep
) -> list[AnnotationEditOut]:
    """List pending edits for an annotation object."""
    await object_service.get_object(object_id, owner=owner)

    from app.models.annotation_edit import AnnotationEdit

    edits = await AnnotationEdit.find(
        AnnotationEdit.object_id == object_id
    ).sort([("+line",), ("+field",)]).to_list()

    return [
        AnnotationEditOut(
            line=e.line,
            field=e.field,
            old_value=e.old_value,
            new_value=e.new_value,
        )
        for e in edits
    ]


@router.put("/annotationstats/edits/{object_id}")
async def save_annotation_edit(
    object_id: PydanticObjectId, body: AnnotationEditRequest, owner: OwnerDep
) -> AnnotationEditOut:
    """Save or update one column edit. Reverts (deletes) when new == old."""
    ann = await object_service.get_object(object_id, owner=owner)

    fmt = ann.format.kind.value if ann.format and ann.format.kind else None
    if fmt not in ("gff", "gtf"):
        raise ValidationError(
            "Editing is only supported for GFF3 and GTF annotations"
        )

    if body.field not in _EDITABLE_FIELDS:
        raise ValidationError(
            f"Unknown field {body.field!r}; editable fields are: "
            + ", ".join(sorted(_EDITABLE_FIELDS))
        )

    source_path = await _resolve_annotation_path(ann)
    old_value = _read_source_column(source_path, body.line, body.field)

    # Get old attributes for identity-key validation.
    old_attributes = None
    if body.field == "attributes":
        old_attributes = _read_source_column(source_path, body.line, "attributes")
    elif body.field in ("start", "end"):
        # For coordinate validation, get the other coordinate's current value
        # (for the effective pair check).
        old_attributes = None

    err = _validate_edit_value(
        new_value=body.new_value,
        field=body.field,
        fmt=fmt,
        old_attributes_line=old_attributes,
    )
    if err:
        raise ValidationError(err)

    # Coordinate pair validation (ED-2).
    if body.field in ("start", "end"):
        db_path = settings.annotation_stats_dir / str(object_id) / "features.db"
        if not db_path.exists():
            raise NotFoundError(
                "No computed results for this file. Compute results first."
            )
        other_field = "end" if body.field == "start" else "start"
        other_int = await _effective_coord(
            db_path=db_path,
            object_id=object_id,
            line=body.line,
            coord_field=other_field,
        )
        this_int = int(body.new_value)
        if body.field == "start" and this_int > other_int:
            raise ValidationError(f"start ({this_int}) must be <= end ({other_int})")
        if body.field == "end" and this_int < other_int:
            raise ValidationError(f"end ({this_int}) must be >= start ({other_int})")

    from app.models.annotation_edit import AnnotationEdit

    # Revert: new value equals original — delete the edit.
    if body.new_value == old_value:
        await AnnotationEdit.find_one(
            AnnotationEdit.object_id == object_id,
            AnnotationEdit.line == body.line,
            AnnotationEdit.field == body.field,
        ).delete()
        return AnnotationEditOut(
            line=body.line,
            field=body.field,
            old_value=None,
            new_value=body.new_value,
        )

    now = datetime.now(UTC)
    existing = await AnnotationEdit.find_one(
        AnnotationEdit.object_id == object_id,
        AnnotationEdit.line == body.line,
        AnnotationEdit.field == body.field,
    )
    if existing:
        existing.new_value = body.new_value
        existing.old_value = old_value
        existing.owner = owner
        existing.updated_at = now
        await existing.save()
    else:
        await AnnotationEdit(
            object_id=object_id,
            line=body.line,
            field=body.field,
            old_value=old_value,
            new_value=body.new_value,
            owner=owner,
            created_at=now,
            updated_at=now,
        ).insert()

    return AnnotationEditOut(
        line=body.line,
        field=body.field,
        old_value=old_value,
        new_value=body.new_value,
    )


@router.delete("/annotationstats/edits/{object_id}")
async def delete_annotation_edit(
    object_id: PydanticObjectId,
    line: int,
    field: str,
    owner: OwnerDep,
) -> dict:
    """Remove one pending edit."""
    await object_service.get_object(object_id, owner=owner)

    from app.models.annotation_edit import AnnotationEdit

    result = await AnnotationEdit.find_one(
        AnnotationEdit.object_id == object_id,
        AnnotationEdit.line == line,
        AnnotationEdit.field == field,
    ).delete()
    return {"deleted": bool(result and result.deleted_count)}


@router.post(
    "/annotationstats/materialize",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
)
async def launch_materialize_annotation_edits(
    body: AnnotationMaterializeRequest, owner: OwnerDep
) -> JobOut:
    """Queue a materialization job for the pending edits on this annotation."""
    from app.models.annotation_edit import AnnotationEdit

    ann = await object_service.get_object(body.object_id, owner=owner)

    fmt = ann.format.kind.value if ann.format and ann.format.kind else None
    if fmt not in ("gff", "gtf"):
        raise ValidationError(
            "Materialization is only supported for GFF3 and GTF annotations"
        )

    # Must have at least one edit.
    count = await AnnotationEdit.find(
        AnnotationEdit.object_id == body.object_id
    ).count()
    if count == 0:
        raise ValidationError("No pending edits to materialize")

    job = await pipeline_service.launch_materialize_annotation_edits(
        object_id=body.object_id, owner=owner
    )
    return JobOut.of(job)

