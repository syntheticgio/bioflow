"""Downloading a published assembly from NCBI.

Sibling to `sra_handlers` rather than a branch inside it, for the reason that
module gives for its own existence: the operational shape differs. One
accession here yields one job producing up to four files with no QC chained,
where a run yields FASTQ pairs that always chain QC. What they share --
shelling out, log capture, failure classification -- is factored into
`run_subprocess` and `download_failures`.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import json
import shutil
import zipfile
from pathlib import Path

from app.config import settings
from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.metadata import assembly_components
from app.models import IoClass, JobClass, JobResources
from app.pipelines import tools
from app.queue import download_failures
from app.queue.executor import run_subprocess
from app.queue.pipeline_handlers import _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# The zip holds already-compressed text and is extracted beside itself, so the
# peak requirement is roughly download + extraction. Deliberately not SRA's
# 4.0, which exists to guess at a compressed archive expanding into plain
# FASTQ; here the post-extraction size is known exactly from the catalog, but
# only after the download, so this stands in for it beforehand.
EXTRACTION_FACTOR = 2.5

# What the CLI names the package. Fixed rather than derived from the accession
# so the extraction step has one path to look for.
PACKAGE_NAME = "package.zip"


@handler(
    "download_assembly",
    mode=HandlerMode.SUBPROCESS,
    # USER_INTERACTIVE for the same reason as the SRA download: someone
    # clicked and is watching for the file, and the work waits on NCBI rather
    # than competing with alignments for CPU.
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
    # Matches download_sra_run: a failed download is usually the network, and
    # the third attempt genuinely succeeds often enough to be worth it.
    max_attempts=3,
)
def download_assembly(ctx: JobContext) -> dict:
    """Fetch one assembly's components. The ingest happens in the applier.

    Synchronous: SUBPROCESS runs this off the event loop, so the body must not
    await and cannot touch the database. It stages files under tmp/ and
    returns a description for `_apply_assembly_download` to persist.

    Idempotent by construction -- each attempt gets a fresh scratch directory,
    so a retry after a partial transfer starts clean rather than extracting a
    truncated zip.
    """
    datasets = tools.require(tools.datasets())

    accession = (ctx.payload.get("accession") or "").strip().upper()
    if not accession:
        raise PermanentError("download_assembly requires an 'accession'")

    project_id = ctx.payload.get("project_id")
    if not project_id:
        raise PermanentError("download_assembly requires a 'project_id'")

    components = ctx.payload.get("components") or ["genome"]
    include = assembly_components.include_argument(components)

    work = _prepare_workdir(ctx, kind="assembly_download")

    log_path = settings.logs_dir / f"{ctx.job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Checked before the transfer: discovering the disk is full once the files
    # exist is too late, and the partial output has to be reaped anyway.
    ctx.progress(phase="preview", pct=0.0, message=f"checking {accession}")
    _check_disk_space(work, ctx.payload.get("bytes_estimate"), accession)

    ctx.check_cancel()

    # A large assembly is a long transfer with no output for minutes at a
    # time, which would otherwise let the lease expire and the reaper
    # double-run the job.
    ctx.extend_lease(3600)

    ctx.progress(phase="downloading", pct=0.05, message=f"downloading {accession}")
    package = work / PACKAGE_NAME
    _download(ctx, datasets.path, accession, include, package, log_path)

    ctx.check_cancel()

    ctx.progress(phase="verifying", pct=0.7, message="verifying checksums")
    _verify(package, accession)

    ctx.progress(phase="extracting", pct=0.8, message="extracting")
    _extract(package, work, accession)

    staged = _label_components(work, accession)
    if not staged:
        # A zero exit whose package held nothing we recognize. Better caught
        # here than as an ingest of nothing several steps later.
        raise RetryableError(
            f"{accession} downloaded but contained no recognizable components"
        )

    # The zip is large and already extracted; keeping it would double the
    # footprint until the scratch reaper runs.
    package.unlink(missing_ok=True)

    ctx.progress(phase="done", pct=1.0, message=f"downloaded {accession}")
    log.info(
        "assembly_download_finished",
        job_id=ctx.job_id,
        accession=accession,
        components=[s["component"] for s in staged],
    )

    return {
        "accession": accession,
        "staged": staged,
        "metadata": ctx.payload.get("metadata") or {},
        "facts": ctx.payload.get("facts") or {},
        "project_id": project_id,
        "job_id": ctx.job_id,
        "staging_dir": str(work),
    }


def _check_disk_space(work: Path, estimate: int | None, accession: str) -> None:
    """Refuse a download that cannot fit before spending an hour on it.

    Silent when no estimate was supplied: a missing figure is not evidence of
    a problem, and refusing on it would block downloads NCBI has no size for.
    """
    if not estimate:
        return

    free = shutil.disk_usage(work).free
    needed = estimate * EXTRACTION_FACTOR

    if needed > free * 0.9:
        raise PermanentError(
            f"Not enough disk space for {accession}: needs roughly "
            f"{needed / 1e9:.1f} GB (package {estimate / 1e9:.1f} GB plus "
            f"extraction), only {free / 1e9:.1f} GB free.",
            details={
                "accession": accession,
                "needed_bytes": int(needed),
                "free_bytes": free,
            },
        )


def _download(
    ctx: JobContext,
    datasets_path: str,
    accession: str,
    include: str,
    package: Path,
    log_path: Path,
) -> None:
    """Fetch the package zip.

    No progress parsing: `--no-progressbar` suppresses the CLI's own bar, and
    its ANSI cursor-up output is not worth reconstructing a percentage from.
    Phases are reported instead, which is honest about a job that is mostly
    one opaque transfer.
    """
    cmd = [
        datasets_path,
        "download",
        "genome",
        "accession",
        accession,
        "--include",
        include,
        # Mandatory: without it the CLI emits an ANSI progress bar that floods
        # the log and makes the tail useless for diagnosing a failure.
        "--no-progressbar",
        "--filename",
        str(package),
    ]

    log.info(
        "assembly_download_started",
        job_id=ctx.job_id,
        accession=accession,
        include=include,
    )

    code = run_subprocess(ctx, cmd, log_path=str(log_path))
    if code != 0:
        raise download_failures.classify_failure(
            code, log_path, accession, tool="datasets"
        )
    if not package.exists():
        raise RetryableError(
            f"datasets exited 0 but wrote no package for {accession}"
        )


def _verify(package: Path, accession: str) -> None:
    """Check the package against its own md5 manifest.

    Cheap, and worth it: a truncated transfer that exits 0 is otherwise
    indistinguishable from a good one until an aligner fails on a malformed
    FASTA hours later. A missing manifest is not fatal -- it is a bonus NCBI
    provides, not a guarantee.
    """
    import hashlib

    try:
        with zipfile.ZipFile(package) as zf:
            names = set(zf.namelist())
            if "md5sum.txt" not in names:
                log.info("assembly_no_manifest", accession=accession)
                return
            manifest = zf.read("md5sum.txt").decode("utf-8", "replace")
            for line in manifest.splitlines():
                parts = line.split()
                if len(parts) != 2:
                    continue
                expected, rel = parts
                member = f"ncbi_dataset/data/{rel}" if rel not in names else rel
                if member not in names:
                    continue
                digest = hashlib.md5(zf.read(member)).hexdigest()
                if digest != expected:
                    raise RetryableError(
                        f"Checksum mismatch for {rel} in {accession}: the "
                        "download was corrupted or truncated."
                    )
    except zipfile.BadZipFile as e:
        raise RetryableError(
            f"{accession} downloaded a corrupt zip: {e}"
        ) from e


def _extract(package: Path, work: Path, accession: str) -> None:
    """Unpack the zip beside itself.

    Members are checked for path traversal before extraction: the archive is
    from NCBI and trustworthy, but a zip that writes outside its target
    directory is the kind of thing that must fail loudly rather than
    silently overwrite something on the host.
    """
    try:
        with zipfile.ZipFile(package) as zf:
            for member in zf.namelist():
                target = (work / member).resolve()
                if not str(target).startswith(str(work.resolve())):
                    raise PermanentError(
                        f"{accession}'s package contains an unsafe path: {member}"
                    )
            zf.extractall(work)
    except zipfile.BadZipFile as e:
        raise RetryableError(f"{accession} downloaded a corrupt zip: {e}") from e


def _label_components(work: Path, accession: str) -> list[dict]:
    """Which file is which component, and what role each becomes.

    Reads `dataset_catalog.json`'s explicit `fileType` rather than matching
    filenames, because the genome FASTA and the CDS FASTA are *both* `.fna` in
    the same directory:

        GCF_000002445.2_ASM244v1_genomic.fna   <- genome
        cds_from_genomic.fna                   <- CDS

    Labeling those by extension roles a CDS file as a reference genome, which
    puts it in the aligner's reference picker where selecting it produces
    silently wrong alignments rather than an error. The filename fallback
    below exists for a catalog NCBI stops shipping, and matches
    `cds_from_genomic` *first* for exactly this reason.
    """
    data_dir = work / "ncbi_dataset" / "data"
    if not data_dir.is_dir():
        log.warning("assembly_no_data_dir", accession=accession)
        return []

    by_type = {spec.file_type: spec for spec in assembly_components.COMPONENTS.values()}

    staged: list[dict] = []
    catalog = data_dir / "dataset_catalog.json"
    if catalog.is_file():
        try:
            payload = json.loads(catalog.read_text())
            if not isinstance(payload, dict):
                # Valid JSON (e.g. a bare `null`) but not the object shape we
                # expect -- same "can't trust this catalog" case as a parse
                # failure, so fall back the same way rather than raising.
                raise ValueError("dataset_catalog.json did not decode to an object")
            for group in payload.get("assemblies") or []:
                for entry in group.get("files") or []:
                    spec = by_type.get(entry.get("fileType"))
                    if spec is None:
                        # DATA_REPORT and anything new: metadata about the
                        # package, not a file the user asked for.
                        continue
                    path = data_dir / entry.get("filePath", "")
                    if not path.is_file():
                        continue
                    staged.append(_entry(path, spec))
        except (ValueError, OSError, TypeError) as e:
            log.warning("assembly_catalog_unreadable", accession=accession, error=str(e))
            staged = []

    if staged:
        return staged

    return _label_by_filename(data_dir, accession)


def _label_by_filename(data_dir: Path, accession: str) -> list[dict]:
    """The fallback when the catalog is missing or unreadable.

    Order is load-bearing: `cds_from_genomic.fna` also ends with
    `_genomic.fna`, so the CDS test must come first or a CDS file is labeled
    as the genome.
    """
    staged: list[dict] = []
    for path in sorted(data_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name == "cds_from_genomic.fna":
            key = "cds"
        elif name == "protein.faa":
            key = "protein"
        elif name.endswith(".gff") or name.endswith(".gff3"):
            key = "gff3"
        elif name.endswith("_genomic.fna"):
            key = "genome"
        else:
            continue
        staged.append(_entry(path, assembly_components.COMPONENTS[key]))

    if not staged:
        log.warning("assembly_nothing_labeled", accession=accession)
    return staged


def _entry(path: Path, spec: assembly_components.ComponentSpec) -> dict:
    return {
        "path": str(path.resolve()),
        "name": path.name,
        "component": spec.key,
        "role": spec.role,
    }
