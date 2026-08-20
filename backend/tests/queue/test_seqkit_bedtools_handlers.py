"""Queue handler unit tests for annotation_comparison and sequence_extraction handlers.
"""

import pytest

from app.errors import PermanentError
from app.pipelines import tools
from app.queue import annotation_comparison_handlers, sequence_extraction_handlers
from app.queue.registry import JobContext


def _ctx(payload: dict) -> JobContext:
    return JobContext(job_id="job-1", payload=payload, epoch=1, attempts=1, owner="local")


def _fake_tool(name: str, version: str) -> tools.Tool:
    return tools.Tool(name=name, path=f"/usr/bin/{name}", version=version)


class TestAnnotationComparisonHandlerPayloadValidation:
    def test_missing_annotation_a_id_raises_permanent_error(self, monkeypatch):
        fake = _fake_tool("bedtools", "2.31.1")
        monkeypatch.setattr(annotation_comparison_handlers.tools, "bedtools", lambda: fake)
        with pytest.raises(PermanentError, match="requires 'annotation_a_id'"):
            annotation_comparison_handlers.run_annotation_comparison(_ctx({}))

    def test_missing_annotation_b_id_raises_permanent_error(self, monkeypatch):
        fake = _fake_tool("bedtools", "2.31.1")
        monkeypatch.setattr(annotation_comparison_handlers.tools, "bedtools", lambda: fake)
        with pytest.raises(PermanentError, match="requires 'annotation_b_id'"):
            annotation_comparison_handlers.run_annotation_comparison(
                _ctx({"annotation_a_id": "anno-1"})
            )


class TestSequenceExtractionHandlerPayloadValidation:
    def test_missing_assembly_id_raises_permanent_error(self, monkeypatch):
        fake = _fake_tool("seqkit", "v2.8.0")
        monkeypatch.setattr(sequence_extraction_handlers.tools, "seqkit", lambda: fake)
        with pytest.raises(PermanentError, match="requires 'assembly_id'"):
            sequence_extraction_handlers.run_sequence_extraction(_ctx({}))

    def test_missing_regions_raises_permanent_error(self, monkeypatch):
        fake = _fake_tool("seqkit", "v2.8.0")
        monkeypatch.setattr(sequence_extraction_handlers.tools, "seqkit", lambda: fake)
        with pytest.raises(PermanentError, match="requires a non-empty 'regions' list"):
            sequence_extraction_handlers.run_sequence_extraction(_ctx({"assembly_id": "ass-1"}))
