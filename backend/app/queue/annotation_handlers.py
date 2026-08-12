"""Annotation Results: the feature table and its summary.

Read-only, like run_vcf_stats and run_bam_stats: derives no objects except
the regenerable SQLite database. The bounded summary returns as facts for
`_apply_run_annotation_stats` to merge; the per-feature detail goes to
settings.annotation_stats_dir and is queried by the table's routes.

One pass. The file is read once and every line reaches both the aggregate
accumulator and the database builder, because a 3M-line GFF3 read twice is
two minutes of I/O for numbers that could have been counted the first time.
"""

import gzip
from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import annotation_db, annotation_parse, annotation_stats
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# How many leading comment lines are kept for provenance. The directives
# worth reading are always in the first few; a file whose header runs longer
# than this is listing sequence-regions, which is not what we display.
_HEADER_SCAN_LINES = 50

_PARSERS = {
    "gff": annotation_parse.parse_gff_line,
    "gtf": annotation_parse.parse_gtf_line,
    "bed": annotation_parse.parse_bed_line,
}


def _open_text(path: Path):
    """Gzip-aware line reader.

    Sniffed by magic bytes rather than extension: an annotation downloaded
    from NCBI is gzipped whether or not whoever renamed it kept the suffix.
    """
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


@handler(
    "run_annotation_stats",
    # THREAD, not SUBPROCESS: parsing and the SQLite write run in this
    # process -- there is no binary to spawn or kill via process group.
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=2048, io=IoClass.HEAVY),
)
def run_annotation_stats(ctx: JobContext) -> dict:
    """Summarize an annotation file and build its feature table."""
    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("run_annotation_stats requires an 'object_id'")

    fmt = ctx.payload.get("format_kind")
    parse_line = _PARSERS.get(fmt)
    if parse_line is None:
        raise PermanentError(f"run_annotation_stats cannot read format {fmt!r}")

    source = Path(ctx.payload["annotation_path"])
    if not source.exists():
        raise PermanentError(f"annotation file is missing: {source}")

    # Contig lengths come from the payload -- the ingest parser already read
    # them from the reference, and the handler cannot query for them.
    contig_lengths = {
        name: int(length)
        for name, length in (ctx.payload.get("contig_lengths") or [])
    }

    acc = annotation_stats.AnnotationAccumulator(contig_lengths=contig_lengths)
    header: list[str] = []

    ctx.progress(phase="parse", pct=0.1, message="reading features")

    def _rows():
        """One pass: every line reaches the accumulator and the database.

        Malformed lines are counted and skipped rather than raised -- a file
        that is half garbage should say so in its provenance line, not fail a
        job that could have summarized the good half.
        """
        with _open_text(source) as fh:
            for i, line in enumerate(fh):
                if i % 100_000 == 0:
                    ctx.check_cancel()
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    if len(header) < _HEADER_SCAN_LINES:
                        header.append(stripped)
                    continue
                feature = parse_line(stripped)
                if feature is None:
                    acc.add_malformed()
                    continue
                acc.add(feature)
                if feature.attributes:
                    # Re-parses the attribute column that parse_line already
                    # parsed once internally -- parse_gff_line/parse_gtf_line
                    # keep only the fields they extract (name, feature_id,
                    # parent, biotype) and the raw string, not the full key
                    # set. Cheap in-memory string work, not a second file
                    # read, so it doesn't defeat this function's one-pass-
                    # over-the-file claim; a known, minor duplication of
                    # work rather than a bug. Fixing it cleanly would mean
                    # having Feature retain the parsed dict/key list, which
                    # touches Task 1's annotation_parse.py -- left as a
                    # documented follow-up rather than reopening that file.
                    if fmt == "gff":
                        keys = annotation_parse.parse_gff_attributes(
                            feature.attributes
                        ).keys()
                    else:
                        keys = annotation_parse.parse_gtf_attributes(
                            feature.attributes
                        ).keys()
                    acc.add_attribute_keys(keys)
                yield feature

    report_dir = settings.annotation_stats_dir / str(object_id)
    report_dir.mkdir(parents=True, exist_ok=True)

    # Built at a temporary path and renamed into place, so a failed recompute
    # leaves the previous working database rather than a half-built one the
    # table would query.
    tmp_db = report_dir / "features.db.tmp"
    total = annotation_db.build_annotation_db(rows=_rows(), db_path=tmp_db)
    tmp_db.replace(report_dir / "features.db")

    ctx.progress(phase="summarize", pct=0.9, message="summarizing")

    facts = {
        "annotation_stats_status": "ok",
        **acc.finish(),
        **annotation_stats.parse_header_directives(header),
    }

    log.info("annotation_stats_built", object_id=str(object_id), features=total)
    return {"object_id": str(object_id), "facts": facts}
