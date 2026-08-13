"""The export_annotation_subset handler."""

import dataclasses

import pytest

from app.errors import PermanentError
from app.pipelines import annotation_db, annotation_hierarchy, annotation_parse
from app.queue.annotation_handlers import export_annotation_subset


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    """Keep _prepare_workdir out of the real tmp/ tree.

    settings.tmp_dir is a read-only property derived from bioinfo_home (see
    app/config.py), not a plain field, so it is bioinfo_home that gets
    patched here -- the same convention test_assembly_qc_handlers.py and
    test_tool_handlers.py already use.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
    return settings


class _Ctx:
    """The parts of JobContext this handler touches.

    `job_id` is here because _prepare_workdir derives the scratch directory
    from it.
    """

    def __init__(self, payload, job_id="test-export-job"):
        self.payload = payload
        self.job_id = job_id

    def check_cancel(self):
        return None

    def progress(self, **kw):
        return None


_SOURCE = """\
##gff-version 3
chr1\t.\tgene\t100\t900\t.\t+\t.\tID=g1
chr1\t.\texon\t100\t200\t.\t+\t0\tID=e1;Parent=g1
chr2\t.\tgene\t10\t20\t.\t-\t.\tID=g2
"""


def _setup(tmp_path):
    source = tmp_path / "in.gff3"
    source.write_text(_SOURCE)

    rows = []
    for i, raw in enumerate(source.read_text().splitlines(), start=1):
        if raw.startswith("#"):
            continue
        feature = annotation_parse.parse_gff_line(raw)
        if feature is not None:
            rows.append(dataclasses.replace(feature, line=i))

    db_path = tmp_path / "features.db"
    annotation_db.build_annotation_db(rows=rows, db_path=db_path)
    annotation_hierarchy.resolve_hierarchy(db_path=db_path)
    return source, db_path


def test_export_writes_the_closure_and_reports_counts(tmp_path):
    source, db_path = _setup(tmp_path)
    ctx = _Ctx({
        "object_id": "abc123",
        "annotation_path": str(source),
        "db_path": str(db_path),
        "format_kind": "gff",
        "filters": {"contig": "chr1"},
        "output_name": "subset.gff3",
    })

    result = export_annotation_subset(ctx)

    assert result["feature_count"] == 2      # the gene and its exon
    assert result["output"]["name"] == "subset.gff3"
    written = open(result["output"]["tmp_path"]).read()
    assert "ID=g1" in written and "ID=e1" in written
    assert "ID=g2" not in written


def test_export_refuses_genbank(tmp_path):
    """AE-25: GenBank features span several lines and record none."""
    source, db_path = _setup(tmp_path)
    ctx = _Ctx({
        "object_id": "abc123",
        "annotation_path": str(source),
        "db_path": str(db_path),
        "format_kind": "genbank",
        "filters": {},
        "output_name": "subset.gff3",
    })

    with pytest.raises(PermanentError, match="genbank"):
        export_annotation_subset(ctx)


def test_export_refuses_an_empty_match(tmp_path):
    """AE-16: an annotation with a header and no features is a file that
    silently disappoints, so refuse rather than write it."""
    source, db_path = _setup(tmp_path)
    ctx = _Ctx({
        "object_id": "abc123",
        "annotation_path": str(source),
        "db_path": str(db_path),
        "format_kind": "gff",
        "filters": {"contig": "chrZ"},
        "output_name": "subset.gff3",
    })

    with pytest.raises(PermanentError, match="no features"):
        export_annotation_subset(ctx)


def test_a_stale_index_fails_permanently(tmp_path):
    """AE-15: retrying cannot help -- the index must be recomputed first."""
    source, db_path = _setup(tmp_path)
    kept = [ln for ln in source.read_text().splitlines() if "g1" not in ln]
    source.write_text("\n".join(kept) + "\n")

    ctx = _Ctx({
        "object_id": "abc123",
        "annotation_path": str(source),
        "db_path": str(db_path),
        "format_kind": "gff",
        "filters": {"contig": "chr1"},
        "output_name": "subset.gff3",
    })

    with pytest.raises(PermanentError, match="out of date"):
        export_annotation_subset(ctx)
