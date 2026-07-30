"""Pipeline endpoints: launching runs and reporting tool availability."""

from pathlib import Path, PurePosixPath

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.v1.jobs import JobOut
from app.config import settings
from app.errors import NotFoundError
from app.models import DataObject, ObjectStatus
from app.pipelines import align_runner, aligner_registry, bam_stats_runner, tools
from app.pipelines.aligners import Aligner
from app.services import pipeline_service

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
async def list_suggestions(object_id: PydanticObjectId) -> dict:
    """Pipelines worth offering for this file, with the reason for each.

    Advisory: a card is a pre-answered instance of an operation the
    Computations section also offers with a picker in front of it. Nothing
    here launches anything -- the cards carry the same payloads the dialogs
    post.
    """
    from app.services import suggestion_service

    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")

    return {"suggestions": await suggestion_service.suggestions_for(obj)}


@router.get("/defaults")
async def trim_defaults(tool: str = "fastp") -> dict:
    """Default trim parameters for the given tool, owned by the server so the
    form does not encode its own copy."""
    return {
        "params": pipeline_service.default_params(tool),
        "max_threads": settings.pipeline_default_threads,
    }


@router.get("/mate/{object_id}", response_model=MateSuggestion | None)
async def detect_mate(object_id: PydanticObjectId) -> MateSuggestion | None:
    """The file this one would be trimmed alongside, if any."""
    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")

    mate = await pipeline_service.suggest_mate(obj)
    if mate is None:
        return None

    from app.pipelines import pairing

    return MateSuggestion(
        object_id=str(mate.id), name=mate.name, mate=pairing.mate_of(mate.name)
    )


@router.post("/trim", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_trim(body: TrimRequest) -> JobOut:
    """Queue an adapter-trimming run over a FASTQ file or an R1/R2 pair."""
    job = await pipeline_service.launch_trim(
        object_id=body.object_id,
        mate_object_id=body.mate_object_id,
        params=body.params,
        paired=body.paired,
        tool=body.tool,
    )
    return JobOut.of(job)


class QCRequest(BaseModel):
    object_id: PydanticObjectId


@router.post("/qc", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_qc(body: QCRequest) -> JobOut:
    """Queue a QC run over a FASTQ file. Read-only: produces a report."""
    job = await pipeline_service.launch_qc(object_id=body.object_id)
    return JobOut.of(job)


@router.get("/qc/report/{object_id}/{report_path:path}")
async def get_qc_report(object_id: PydanticObjectId, report_path: str) -> FileResponse:
    """Serve a generated QC report (FastQC or fastp HTML).

    Reports are not content-addressed objects -- they are regenerable
    derivatives -- so they live under qc_reports/ and are served from here
    rather than through the blob routes.

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
    """
    # Rejected outright rather than resolved away. The ASGI layer collapses
    # `..` before routing, so a path that reaches here still containing one is
    # not a browser fetching a report -- and relying on that normalization
    # would be relying on a layer whose job is not security. Note what the
    # collapsing does on its own: `/report/AAA/../BBB/x.html` arrives with
    # object_id already rewritten to BBB, so the id in the URL is not by itself
    # evidence of which directory is being read.
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

    return FileResponse(
        target,
        headers={
            "Content-Security-Policy": (
                "sandbox; default-src 'none'; "
                # FastQC's plots are inlined images and its layout is inline
                # CSS, so the report is blank without these two. Neither can
                # execute, which is what the sandbox is there to prevent.
                "img-src 'self' data:; style-src 'unsafe-inline'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


class BamStatsRequest(BaseModel):
    object_id: PydanticObjectId


@router.post("/bamstats", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_bam_stats(body: BamStatsRequest) -> JobOut:
    """Queue the Results computation for a BAM: coverage, per-contig table,
    binned depth. Read-only: produces facts and one TSV report."""
    job = await pipeline_service.launch_bam_stats(object_id=body.object_id)
    return JobOut.of(job)


@router.get("/bamstats/report/{object_id}/{report_path:path}")
async def get_bam_stats_report(
    object_id: PydanticObjectId,
    report_path: str,
    download: bool = False,
    offset: int = 0,
    limit: int = 100,
):
    """Serve the per-contig BAM stats report.

    Same containment rules as get_qc_report -- `..` and absolute paths are
    rejected outright, then the resolved path is re-checked against the report
    root. Unlike a QC report, this file is generated by this app from numeric
    samtools output rather than embedding read-derived strings, and it is
    never rendered as a document -- so the sandboxed CSP that HTML report
    serving needs does not apply here.

    Two modes: `?download=1` returns the whole TSV as an attachment; the
    default paginates it as JSON, which is what the Results tab's contig table
    reads from.
    """
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


class VariantRequest(BaseModel):
    bam_id: PydanticObjectId
    # Normally resolved from the BAM's provenance. Supplied only for an
    # uploaded BAM, which carries no record of what it was aligned against.
    reference_id: PydanticObjectId | None = None
    caller: str | None = None
    params: dict = Field(default_factory=dict)


@router.get("/align/defaults/{object_id}")
async def align_defaults(object_id: PydanticObjectId) -> dict:
    """Defaults for the alignment dialog, including the read group.

    Read-group fields come from the reads' own metadata, so the dialog is
    usually a confirmation rather than data entry -- and the aligner defaults
    to one that is actually installed.
    """
    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")

    return {
        "params": pipeline_service.default_align_params(obj),
        "read_group": pipeline_service.default_read_group(obj),
        "aligners": [
            {
                "name": a.value,
                "available": (
                    tools.bwa_mem2() if a.value == "bwa-mem2" else tools.minimap2()
                ).available,
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
    """
    try:
        parsed = Aligner(aligner)
    except ValueError:
        raise NotFoundError(f"Unknown aligner: {aligner}") from None
    return aligner_registry.schema_for(parsed)


@router.get("/align-envelope")
async def align_envelope(object_id: PydanticObjectId, reference_id: PydanticObjectId) -> dict:
    """Everything the dialog needs to estimate memory without a round trip.

    Sent once when the dialog opens; the client then evaluates the same
    arithmetic locally as sliders move. The formula stays in Python -- only
    the coefficients ship -- so there is no second implementation to drift,
    and `launch_alignment` re-runs the authoritative check regardless.
    """
    return await pipeline_service.align_envelope(
        object_id=object_id, reference_id=reference_id
    )


@router.get("/references/{project_id}")
async def list_references(project_id: PydanticObjectId) -> dict:
    """Candidate references in a project, each with its index status.

    Index status rides along so the dialog can say "this will build an index
    first" rather than surprising the user with a long job.
    """
    from app.services import object_service

    objects = await object_service.list_objects(project_id, limit=500)
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


@router.post("/index", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def build_index(body: BuildIndexRequest) -> JobOut:
    """Build an aligner index for a reference, eagerly.

    The same job the alignment path queues when an index is missing, so there
    is no second code path to keep correct.
    """
    job = await pipeline_service.launch_build_index(
        reference_id=body.reference_id, aligner=body.aligner
    )
    return JobOut.of(job)


@router.post("/align", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def launch_alignment(body: AlignRequest) -> JobOut:
    """Queue an alignment, building the reference index first if needed."""
    job = await pipeline_service.launch_alignment(
        object_id=body.object_id,
        reference_id=body.reference_id,
        mate_object_id=body.mate_object_id,
        read_group=body.read_group,
        params=body.params,
        paired=body.paired,
    )
    return JobOut.of(job)


@router.get("/variants/defaults/{bam_id}")
async def variant_defaults(bam_id: PydanticObjectId) -> dict:
    """Defaults for the variant calling dialog.

    Reports the inferred chemistry and the caller it implies, plus whether the
    reference could be resolved -- so the dialog knows to ask for one rather
    than discovering at submit time that it has to.
    """
    obj = await DataObject.get(bam_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {bam_id}")

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
async def launch_variant_calling(body: VariantRequest) -> JobOut:
    """Queue a variant calling run over an aligned, indexed BAM."""
    job = await pipeline_service.launch_variant_calling(
        bam_id=body.bam_id,
        reference_id=body.reference_id,
        caller=body.caller,
        params=body.params,
    )
    return JobOut.of(job)
