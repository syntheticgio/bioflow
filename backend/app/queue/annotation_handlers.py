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
from app.pipelines import (
    annotation_db,
    annotation_export,
    annotation_hierarchy,
    annotation_parse,
    annotation_stats,
    genbank_parse,
    genbank_reader,
)
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
            # i is 0-based and counts every line including comments and
            # blanks, which is what makes this address the file rather than
            # the features in it -- export re-reads exactly this line.
            feature = parse_line(stripped, i + 1)
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
        # What export verifies the source against. Null for a file the
        # launcher could not digest (register-in-place, or hashing still
        # queued); export then proceeds on per-line verification alone and
        # says so in the exported object's facts.
        "annotation_source_sha256": ctx.payload.get("annotation_sha256"),
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
    "annotation_subset_export",
    # THREAD for the same reason as run_annotation_stats: this is Python
    # file I/O and SQLite, with no binary to spawn or kill.
    mode=HandlerMode.THREAD,
    job_class=JobClass.COMPUTE,
    resources=JobResources(cpu=1, mem_mb=512, io=IoClass.HEAVY),
)
def run_annotation_subset_export(ctx: JobContext) -> dict:
    """Write the filtered subset of an annotation to a new file."""
    payload = ctx.payload
    if payload.get("format_kind") == "genbank":
        raise PermanentError(
            "GenBank features span multiple lines and its segment children "
            "are synthetic, so a subset cannot be re-emitted from it"
        )

    source = Path(payload["annotation_path"])
    if not source.exists():
        raise PermanentError(f"annotation file is missing: {source}")

    db_path = Path(payload["db_path"])
    if not db_path.exists():
        raise PermanentError(
            "No computed results for this annotation. Compute results first."
        )

    # The whole-file check, before any line is read. Per-line verification
    # alone passes on a stale index whose file was replaced with one that is
    # mostly unchanged -- the exported lines each verify while the subset
    # silently mixes two versions.
    recorded = payload.get("recorded_sha256")
    current = payload.get("source_sha256")
    verified = bool(recorded) and bool(current)
    if verified and recorded != current:
        raise PermanentError(
            f"{payload.get('source_name') or source.name} has changed since "
            f"its results were computed; recompute results and try again"
        )

    # parent_status is declared a tuple but arrives from the JSON payload as
    # a list -- the queue serializes the launcher's dataclasses.asdict(). The
    # IN clause iterates either, so nothing breaks today; restoring the tuple
    # keeps the frozen dataclass's declared type honest for the next reader.
    raw_filters = dict(payload.get("filters") or {})
    if raw_filters.get("parent_status") is not None:
        raw_filters["parent_status"] = tuple(raw_filters["parent_status"])
    filters = annotation_db.FeatureFilters(**raw_filters)

    ctx.progress(phase="select", pct=0.2, message="selecting features")
    matched = annotation_db.count_features(db_path=db_path, filters=filters)
    lines = annotation_export.closure_lines(db_path=db_path, filters=filters)

    ctx.progress(phase="write", pct=0.6, message="writing subset")
    out_dir = Path(payload["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    name = annotation_export.subset_name(
        payload.get("source_name") or source.name, payload.get("filters") or {}
    )
    dest = out_dir / name

    verify = annotation_export.verification_map(db_path=db_path, lines=lines)
    # write_subset defaults to the GFF3 parser when none is passed, which
    # would spuriously fail every verification on a GTF or BED export -- the
    # two formats structure column 9 differently, so a real caller must name
    # its own format's parser explicitly.
    parse_line = {
        "gff": annotation_parse.parse_gff_line,
        "gtf": annotation_parse.parse_gtf_line,
        "bed": annotation_parse.parse_bed_line,
    }[payload["format_kind"]]
    try:
        exported = annotation_export.write_subset(
            source=source, dest=dest, lines=lines, verify=verify,
            parse_line=parse_line,
        )
    except annotation_export.ExportMismatch as e:
        dest.unlink(missing_ok=True)
        raise PermanentError(str(e)) from e

    log.info(
        "annotation_subset_exported",
        object_id=str(payload.get("object_id")),
        matched=matched,
        exported=exported,
    )
    return {
        "object_id": str(payload.get("object_id")),
        "output": {"tmp_path": str(dest), "name": name},
        "counts": {"matched": matched, "exported": exported},
        "facts": {
            "annotation_subset_filters": payload.get("filters") or {},
            "annotation_subset_matched": matched,
            "annotation_subset_exported": exported,
            # False when the launcher had no digest to compare (a
            # register-in-place file, or hashing still queued). The export
            # still ran on per-line verification; this makes the weaker
            # guarantee auditable rather than hidden.
            "annotation_subset_source_verified": verified,
        },
    }
