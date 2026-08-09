"""The tile scan's contract with the QC handler: it contributes facts when it
can, and it never fails the job when it cannot."""

from pathlib import Path

import pytest

from app.queue import pipeline_handlers


def test_tile_facts_failure_is_swallowed(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(pipeline_handlers.tile_scanner, "scan", boom)

    # Returns empty rather than raising: a broken scan must not deny the user
    # the fastp facts that did parse.
    assert pipeline_handlers._tile_facts(tmp_path / "r1.fastq", tmp_path) == {}


def test_tile_facts_returns_scanner_facts(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pipeline_handlers.tile_scanner,
        "scan",
        lambda path, **kw: "sentinel-result",
    )
    monkeypatch.setattr(
        pipeline_handlers.tile_scanner,
        "write_matrix",
        lambda result, report_dir: {"qc_tile_source": "present"},
    )

    facts = pipeline_handlers._tile_facts(tmp_path / "r1.fastq", tmp_path)

    assert facts == {"qc_tile_source": "present"}


def test_tile_facts_reports_absent_for_a_file_without_tiles(tmp_path):
    """A file whose headers carry no tiles is an ordinary outcome.

    The handler must record `absent` rather than nothing at all: the frontend
    branches on this fact, and its absence would be indistinguishable from a
    QC run that predates this feature.
    """
    path = tmp_path / "r1.fastq"
    path.write_text("".join(f"@SRR123456.{i}\nACGT\n+\nIIII\n" for i in range(1200)))

    facts = pipeline_handlers._tile_facts(path, tmp_path / "reports")

    assert facts["qc_tile_source"] == "absent"
    assert "qc_tile_matrix" not in facts
