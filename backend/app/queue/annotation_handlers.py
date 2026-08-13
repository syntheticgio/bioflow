"""Annotation Results: the feature table and its summary.

Read-only, like run_vcf_stats and run_bam_stats: derives no objects except
the regenerable SQLite database. The bounded summary returns as facts for
`_apply_run_annotation_stats` to merge; the per-feature detail goes to
settings.annotation_stats_dir and is queried by the table's routes.

One pass. The file is read once and every line reaches both the aggregate
accumulator and the database builder, because a 3M-line GFF3 read twice is
two minutes of I/O for numbers that could have been counted the first time.
"""

import dataclasses
import gzip
from pathlib import Path

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.pipelines import (
    annotation_db,
    annotation_export,
    annotation_hierarchy,
    annotation_parse,
    annotation_stats,
    genbank_parse,
    genbank_reader,
    genbank_sequence,
)
from app.queue.pipeline_handlers import _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# How many leading comment lines are kept for provenance. The directives
# worth reading are always in the first few; a file whose header runs longer
# than this is listing sequence-regions, which is not what we display.
_HEADER_SCAN_LINES = 50


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


def _line_rows(parse_line, source: Path, ctx, acc, header: list[str], fmt: str):
    """Row iterator for the line-oriented formats.

    GFF, GTF and BED are one feature per line, so this is the original loop
    unchanged -- it lives behind the same iterator interface GenBank needs so
    the handler has one code path.

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
            # enumerate() is 0-based; source lines are 1-based, and the export
            # re-scan counts the same way (AE-1).
            feature = dataclasses.replace(feature, line=i + 1)
            acc.add(feature)
            if feature.attributes:
                # Re-parses the attribute column that parse_line already
                # parsed once internally -- parse_gff_line/parse_gtf_line
                # keep only the fields they extract (name, feature_id,
                # parent, biotype) and the raw string, not the full key
                # set. Cheap in-memory string work, not a second file
                # read, so it doesn't defeat this function's one-pass-
                # over-the-file claim; a known, minor duplication of
                # work rather than a bug.
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


def _genbank_rows(source: Path, ctx, acc, facts: dict):
    """Row iterator for GenBank.

    Two things differ from the line formats. Contig lengths come from each
    record's own LOCUS line rather than the payload, so coverage works with
    no paired reference. And a segment-bearing parent is counted but kept out
    of the coverage accumulator: `_ContigCoverage` merges intervals, so its
    outer span would fill in the introns its own children carve out.
    """
    lengths: dict[str, int] = {}
    records = 0
    has_sequence = False
    names: list[str] = []

    for record in genbank_reader.iter_records(source):
        ctx.check_cancel()
        records += 1
        has_sequence = has_sequence or record.has_sequence
        names.append(record.accession)
        if record.length:
            lengths[record.accession] = record.length

        seen_parents: set[str] = set()
        rows_this_record = list(
            genbank_parse.iter_features(record.feature_lines, accession=record.accession)
        )
        for feature in rows_this_record:
            seen_parents.update(feature.parents)
            yield feature

        for feature in rows_this_record:
            if feature.feature_id in seen_parents:
                # Counted, but its outer span never reaches coverage.
                acc.add_without_coverage(feature)
            else:
                acc.add(feature)
            if feature.attributes:
                acc.add_attribute_keys(
                    annotation_parse.parse_gff_attributes(feature.attributes).keys()
                )

    facts["genbank_record_count"] = records
    facts["genbank_has_sequence"] = has_sequence
    facts["genbank_locus_names"] = names
    facts["_contig_lengths"] = lengths


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
    if fmt not in ("gff", "gtf", "bed", "genbank"):
        raise PermanentError(f"run_annotation_stats cannot read format {fmt!r}")

    source = Path(ctx.payload["annotation_path"])
    if not source.exists():
        raise PermanentError(f"annotation file is missing: {source}")

    # Contig lengths come from the payload -- the ingest parser already read
    # them from the reference, and the handler cannot query for them. GenBank
    # is the exception: it states each contig's length on its own LOCUS line,
    # so it needs no paired reference (see below, after the db is built).
    contig_lengths = {
        name: int(length)
        for name, length in (ctx.payload.get("contig_lengths") or [])
    }

    acc = annotation_stats.AnnotationAccumulator(contig_lengths=contig_lengths)
    header: list[str] = []

    ctx.progress(phase="parse", pct=0.1, message="reading features")

    report_dir = settings.annotation_stats_dir / str(object_id)
    report_dir.mkdir(parents=True, exist_ok=True)

    extra_facts: dict = {}
    if fmt == "genbank":
        rows = _genbank_rows(source, ctx, acc, extra_facts)
    else:
        parse_line = {
            "gff": annotation_parse.parse_gff_line,
            "gtf": annotation_parse.parse_gtf_line,
            "bed": annotation_parse.parse_bed_line,
        }[fmt]
        rows = _line_rows(parse_line, source, ctx, acc, header, fmt)

    # Built at a temporary path and renamed into place, so a failed recompute
    # leaves the previous working database rather than a half-built one the
    # table would query.
    tmp_db = report_dir / "features.db.tmp"
    total = annotation_db.build_annotation_db(rows=rows, db_path=tmp_db)

    # Resolution and the gene table run against the temporary path, before
    # the rename: a database is published only once it is fully classified,
    # so the table never queries rows whose parent_status is still the
    # insert-time default.
    ctx.progress(phase="resolve", pct=0.7, message="resolving hierarchy")
    resolution = annotation_hierarchy.resolve_hierarchy(db_path=tmp_db)
    status_counts = resolution["counts"]
    gene_result = annotation_hierarchy.build_gene_table(db_path=tmp_db)

    tmp_db.replace(report_dir / "features.db")

    # GenBank states each contig's length on its own LOCUS line, so it needs
    # no reference. The payload's lengths remain the source for GFF/GTF/BED.
    # This must run after build_annotation_db above: _genbank_rows is a
    # generator, and extra_facts["_contig_lengths"] is only populated once
    # the generator has been fully consumed (the assignment is the last thing
    # that happens after its `for record in ...` loop completes). The assert
    # is the tripwire for that ordering: if build_annotation_db ever stops
    # draining `rows` to completion before returning, this must fail loudly
    # here rather than let every GenBank contig's length silently go missing.
    if fmt == "genbank":
        parsed_lengths = extra_facts.pop("_contig_lengths", None)
        assert parsed_lengths is not None, (
            "_genbank_rows must be fully consumed before this point"
        )
        if parsed_lengths:
            acc.set_contig_lengths(parsed_lengths)
            # A GenBank file carries its own lengths, so it is not waiting on
            # a reference and must not be re-analyzed by ingest's backfill.
            extra_facts["annotation_contig_lengths_known"] = True

    ctx.progress(phase="summarize", pct=0.9, message="summarizing")

    unresolved = sum(
        status_counts.get(s, 0) for s in annotation_hierarchy.UNRESOLVED_STATUSES
    )

    facts = {
        "annotation_stats_status": "ok",
        # From the payload, not from `contig_lengths` being non-empty: a
        # GenBank file states its own lengths on each LOCUS line and needs no
        # reference, so emptiness of the payload's list is not the same
        # question. The launcher is the only place that knows whether a
        # reference was looked for and found.
        "annotation_contig_lengths_known": bool(
            ctx.payload.get("contig_lengths_known")
        ),
        **acc.finish(),
        **annotation_stats.parse_header_directives(header),
        **extra_facts,
        "annotation_parent_status_counts": status_counts,
        "annotation_unresolved_count": unresolved,
        "annotation_max_depth": resolution["max_depth"],
        "annotation_gene_mode": gene_result["mode"],
        "annotation_gene_count": gene_result["count"],
    }

    log.info(
        "annotation_stats_built",
        object_id=str(object_id),
        features=total,
        unresolved=unresolved,
    )
    return {"object_id": str(object_id), "facts": facts}


@handler(
    "export_annotation_subset",
    # THREAD for the same reason run_annotation_stats is: the work is a
    # SQLite read and a file copy in this process, with no binary to spawn.
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
)
def export_annotation_subset(ctx: JobContext) -> dict:
    """Write the filtered subset of an annotation to a new file.

    The filter arrives as it was applied in the table and is passed straight
    through to `closure_lines`. This handler never re-derives it: that is
    what keeps the exported subset and the displayed table from drifting.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("export_annotation_subset requires an 'object_id'")

    fmt = ctx.payload.get("format_kind")
    if fmt == "genbank":
        raise PermanentError(
            "cannot export a subset of a genbank annotation: its features span "
            "several lines and its segment rows correspond to no single line"
        )
    if fmt not in ("gff", "gtf", "bed"):
        raise PermanentError(f"export_annotation_subset cannot read format {fmt!r}")

    source = Path(ctx.payload["annotation_path"])
    if not source.exists():
        raise PermanentError(f"annotation file is missing: {source}")

    db_path = Path(ctx.payload["db_path"])
    if not db_path.exists():
        raise PermanentError(
            "this annotation has no computed results; compute them before exporting"
        )

    raw_filters = ctx.payload.get("filters") or {}
    status = raw_filters.get("parent_status")
    filters = annotation_db.FeatureFilters(
        contig=raw_filters.get("contig"),
        start_min=raw_filters.get("start_min"),
        start_max=raw_filters.get("start_max"),
        feature_type=raw_filters.get("feature_type"),
        biotype=raw_filters.get("biotype"),
        name_query=raw_filters.get("name_query"),
        strand=raw_filters.get("strand"),
        top_level_only=False,
        parent_status=tuple(status) if status else None,
    )

    ctx.progress(phase="closure", pct=0.2, message="selecting features")
    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)
    if not lines:
        raise PermanentError(
            "no features matched the requested filters, so there is nothing to export"
        )

    ctx.progress(phase="write", pct=0.5, message="writing subset")
    header: list[str] = []
    with _open_text(source) as fh:
        for i, raw in enumerate(fh):
            if i >= _HEADER_SCAN_LINES:
                break
            stripped = raw.rstrip("\n")
            if stripped.startswith("#"):
                header.append(stripped)

    # _prepare_workdir, not a bare tmp path: it puts the output under
    # settings.tmp_dir, which shares a filesystem with objects/, so ingesting
    # the finished file is an atomic rename rather than a copy. It also wipes
    # the directory on entry, so a retry does not inherit a half-written file.
    work = _prepare_workdir(ctx, "annotation_export")
    dest = work / ctx.payload["output_name"]
    try:
        written = annotation_export.write_subset(
            source=source, dest=dest, db_path=db_path, lines=lines,
            header=header, fmt=fmt,
        )
    except annotation_export.StaleIndexError as e:
        # Not retryable: the same job would read the same mismatched file.
        # The index has to be recomputed first, which is a user action.
        raise PermanentError(
            f"{e} -- recompute this annotation's results and export again"
        ) from e

    log.info(
        "annotation_subset_exported",
        object_id=str(object_id),
        features=written,
    )
    return {
        "object_id": str(object_id),
        "feature_count": written,
        "output": {"tmp_path": str(dest), "name": ctx.payload["output_name"]},
    }


@handler(
    "materialize_annotation_edits",
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
)
def materialize_annotation_edits(ctx: JobContext) -> dict:
    """Rewrite edited columns in an annotation source file and produce a
    derived object.

    Reads all AnnotationEdit documents for the source object, scans the
    source line-by-line, and rewrites the specific column(s) in each edited
    line. Every unedited column is preserved verbatim — this never
    reconstructs a line from a Feature (same philosophy as export's
    "re-emit source line").

    On success, the applier creates a derived annotation object and clears
    the pending edits.
    """
    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("materialize_annotation_edits requires an 'object_id'")

    fmt = ctx.payload.get("format_kind")
    if fmt not in ("gff", "gtf"):
        raise PermanentError(
            f"materialize_annotation_edits cannot process format {fmt!r}"
        )

    ann_name = ctx.payload.get("annotation_name", f"{object_id}.edited.gff3")
    source = Path(ctx.payload["annotation_path"])
    if not source.exists():
        raise PermanentError(f"annotation file is missing: {source}")

    # Load all pending edits.
    from app.db.client import run_from_thread

    edits = run_from_thread(_load_edits(object_id))
    if not edits:
        raise PermanentError("no pending edits to materialize")

    # Group edits by line for efficient scanning.
    by_line: dict[int, dict[str, str]] = {}
    edit_summary: list[dict] = []
    for e in edits:
        by_line.setdefault(e.line, {})[e.field] = e.new_value
        edit_summary.append({
            "line": e.line,
            "field": e.field,
            "old_value": e.old_value,
            "new_value": e.new_value,
        })

    # Map editable field to 0-based column index.
    _FIELD_TO_COL = {
        "source": 1,
        "type": 2,
        "start": 3,
        "end": 4,
        "attributes": 8,
    }

    ctx.progress(phase="rewrite", pct=0.3, message="rewriting edited lines")

    # ED-7 re-parse check: same parser the original normalization used.
    parse_line = {
        "gff": annotation_parse.parse_gff_line,
        "gtf": annotation_parse.parse_gtf_line,
    }[fmt]

    work = _prepare_workdir(ctx, "annotation_materialize")
    out_name = (
        ann_name
        if ann_name.endswith((".gff3", ".gtf", ".gff", ".gtf"))
        else f"{ann_name}.gff3"
    )
    dest = work / out_name

    changed = 0
    with _open_text(source) as fh, open(dest, "w") as out:
        for i, raw in enumerate(fh, start=1):
            stripped = raw.rstrip("\n")
            if i in by_line:
                changes = by_line[i]
                columns = stripped.split("\t")
                if len(columns) < 9:
                    # Skip comment/blank lines that happen to be in by_line
                    # (shouldn't happen since validation checks for this).
                    out.write(stripped + "\n")
                    continue
                for field, new_val in changes.items():
                    col = _FIELD_TO_COL[field]
                    columns[col] = new_val
                stripped = "\t".join(columns)
                changed += 1
                # ED-7: re-parse the edited line.
                feature = parse_line(stripped)
                if feature is None:
                    raise PermanentError(
                        f"line {i} no longer parses after applying edit; "
                        f"the edit produced an invalid annotation line"
                    )
            out.write(stripped + "\n")

    if changed != len(by_line):
        log.warning(
            "annotation_materialized_lines_mismatch",
            expected=len(by_line),
            changed=changed,
        )

    log.info(
        "annotation_edits_materialized",
        object_id=object_id,
        edits=len(edit_summary),
        lines=changed,
    )
    return {
        "object_id": object_id,
        "edit_count": len(edit_summary),
        "edit_summary": edit_summary,
        "output": {"tmp_path": str(dest), "name": out_name},
    }


async def _load_edits(object_id: str):
    """Load pending edits for the source object. Coroutine so it can be
    scheduled via run_from_thread from the THREAD-mode handler."""
    from beanie import PydanticObjectId

    from app.models.annotation_edit import AnnotationEdit

    return await AnnotationEdit.find(
        AnnotationEdit.object_id == PydanticObjectId(object_id)
    ).to_list()


@handler(
    "extract_genbank_sequence",
    # THREAD for the same reason its two siblings are: the work is file I/O
    # in this process, with no binary to spawn or kill via process group.
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    # 512MB regardless of input size. genbank_sequence streams, so memory is
    # flat in the size of the ORIGIN block -- a 300MB sequence costs no more
    # here than a 300KB one.
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
)
def extract_genbank_sequence(ctx: JobContext) -> dict:
    """Write a GenBank file's ORIGIN sequence out as a FASTA reference."""
    object_id = ctx.payload.get("object_id")
    if not object_id:
        raise PermanentError("extract_genbank_sequence requires an 'object_id'")

    source = Path(ctx.payload["genbank_path"])
    if not source.exists():
        raise PermanentError(f"genbank file is missing: {source}")

    # _prepare_workdir, not a bare tmp path: it puts the output under
    # settings.tmp_dir, which shares a filesystem with objects/, so ingesting
    # the finished file is an atomic rename rather than a copy of what may be
    # a very large FASTA. It also wipes the directory on entry, so a retry
    # does not inherit a half-written file.
    work = _prepare_workdir(ctx, "genbank_sequence")
    dest = work / ctx.payload["output_name"]

    ctx.progress(phase="extract", pct=0.1, message="extracting sequence")
    written = genbank_sequence.write_fasta(source=source, dest=dest)

    # Read from the file rather than trusting the `genbank_has_sequence` fact
    # that offered this action: the fact was recorded by an earlier job and
    # the file may have been replaced since.
    if written == 0:
        raise PermanentError(
            "this genbank file contains no sequence to extract"
        )

    log.info(
        "genbank_sequence_extracted",
        object_id=str(object_id),
        records=written,
    )
    return {
        "object_id": str(object_id),
        "record_count": written,
        "output": {"tmp_path": str(dest), "name": ctx.payload["output_name"]},
    }
