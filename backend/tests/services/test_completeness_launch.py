"""The launch path for assembly completeness scoring.

Same posture test_assembly_launch.py's TestLaunchReachesTheQueue takes: run
the whole way to the enqueue rather than stopping at "the service raised
nothing", since a validation bug three statements past the check that matters
would otherwise pass silently.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.errors import PermanentError, ValidationError
from app.models import FormatKind, ObjectRole, ObjectStatus
from app.pipelines.tools import Tool
from app.services import pipeline_service

_COMPLEASM_TOOL = Tool(name="compleasm", path="/usr/local/bin/compleasm", version="0.2.9")


def _fasta(*, organism=None, role=ObjectRole.REFERENCE, status=ObjectStatus.READY):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="assembly.fasta",
        format=SimpleNamespace(kind=FormatKind.FASTA),
        role=role,
        metadata={"organism": organism} if organism else {},
        facts={},
        status=status,
        project_id=PydanticObjectId(),
        owner="local",
        blob_sha256="a" * 64,
    )


class TestCheckCompletenessCallable:
    def test_not_ready_is_refused(self):
        obj = _fasta(organism="Saccharomyces cerevisiae", status=ObjectStatus.HASHING)
        with pytest.raises(ValidationError, match="not ready"):
            pipeline_service._check_completeness_callable(obj)

    def test_non_fasta_is_refused(self):
        obj = _fasta(organism="Saccharomyces cerevisiae")
        obj.format = SimpleNamespace(kind=FormatKind.FASTQ)
        with pytest.raises(ValidationError, match="not a FASTA"):
            pipeline_service._check_completeness_callable(obj)

    def test_protein_fasta_is_refused(self):
        """The trap CLAUDE.md records the align card already having shipped
        wrong: protein.faa is FormatKind.FASTA and would pass any "does this
        look like a genome" sniff test that isn't role-based."""
        obj = _fasta(organism="Saccharomyces cerevisiae", role=ObjectRole.PROTEIN)
        with pytest.raises(ValidationError, match="protein"):
            pipeline_service._check_completeness_callable(obj)

    def test_transcript_fasta_is_refused(self):
        obj = _fasta(organism="Saccharomyces cerevisiae", role=ObjectRole.TRANSCRIPT)
        with pytest.raises(ValidationError, match="transcript"):
            pipeline_service._check_completeness_callable(obj)

    def test_reference_role_is_accepted(self):
        obj = _fasta(organism="Saccharomyces cerevisiae", role=ObjectRole.REFERENCE)
        pipeline_service._check_completeness_callable(obj)  # does not raise

    def test_unset_role_is_accepted(self):
        """An uploaded assembly is as eligible as one this application
        produced -- the card is not gated on provenance."""
        obj = _fasta(organism="Saccharomyces cerevisiae", role=None)
        pipeline_service._check_completeness_callable(obj)  # does not raise


class TestLaunchCompletenessReachesTheQueue:
    async def test_a_yeast_assembly_infers_a_lineage_and_reaches_the_queue(self):
        obj = _fasta(organism="Saccharomyces cerevisiae S288C")
        enqueued = {}

        async def _enqueue(job_type, **kwargs):
            enqueued["type"] = job_type
            enqueued.update(kwargs)
            return SimpleNamespace(id="job1")

        with (
            patch("app.pipelines.tools.compleasm", return_value=_COMPLEASM_TOOL),
            patch(
                "app.services.object_service.get_object",
                AsyncMock(return_value=obj),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=("a" * 64, None)),
            ),
            patch(
                "app.queue.lineage_handlers.lineage_present", return_value=True
            ),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            job = await pipeline_service.launch_completeness(
                object_id=obj.id, owner="local"
            )

        assert job.id == "job1"
        assert enqueued["type"] == "assess_completeness"
        assert enqueued["payload"]["lineage"] == "saccharomycetaceae"
        assert enqueued["payload"]["odb"] == "odb12"
        assert enqueued["max_attempts"] == 1

    async def test_an_explicit_lineage_overrides_inference(self):
        obj = _fasta(organism="Saccharomyces cerevisiae")
        enqueued = {}

        async def _enqueue(job_type, **kwargs):
            enqueued.update(kwargs)
            return SimpleNamespace(id="job1")

        with (
            patch("app.pipelines.tools.compleasm", return_value=_COMPLEASM_TOOL),
            patch(
                "app.services.object_service.get_object",
                AsyncMock(return_value=obj),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=("a" * 64, None)),
            ),
            patch(
                "app.queue.lineage_handlers.lineage_present", return_value=True
            ),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            await pipeline_service.launch_completeness(
                object_id=obj.id, owner="local", lineage="eukaryota"
            )

        assert enqueued["payload"]["lineage"] == "eukaryota"

    async def test_no_organism_and_no_override_is_refused_not_guessed(self):
        """An uploaded assembly with no organism metadata is a normal case.
        The honest response is to ask, not to silently score against a
        guessed domain -- a eukaryotic assembly scored as bacteria reports
        every real gene as missing."""
        obj = _fasta(organism=None)

        with (
            patch("app.pipelines.tools.compleasm", return_value=_COMPLEASM_TOOL),
            patch(
                "app.services.object_service.get_object",
                AsyncMock(return_value=obj),
            ),
        ):
            with pytest.raises(ValidationError, match="organism metadata"):
                await pipeline_service.launch_completeness(
                    object_id=obj.id, owner="local"
                )

    async def test_missing_lineage_dataset_is_refused_before_enqueue(self):
        """A completeness job must not depend on the network -- the launch
        path checks presence itself rather than letting the job discover
        this partway through a run that may have taken hours."""
        obj = _fasta(organism="Saccharomyces cerevisiae")
        enqueue = AsyncMock()

        with (
            patch("app.pipelines.tools.compleasm", return_value=_COMPLEASM_TOOL),
            patch(
                "app.services.object_service.get_object",
                AsyncMock(return_value=obj),
            ),
            patch(
                "app.queue.lineage_handlers.lineage_present", return_value=False
            ),
            patch("app.queue.queue.enqueue", enqueue),
        ):
            with pytest.raises(ValidationError, match="not downloaded"):
                await pipeline_service.launch_completeness(
                    object_id=obj.id, owner="local"
                )
        enqueue.assert_not_awaited()

    async def test_compleasm_not_installed_is_refused_before_anything_else(self):
        missing = Tool(name="compleasm", path=None, version=None, error="not found")
        obj = _fasta(organism="Saccharomyces cerevisiae")
        get_object = AsyncMock(return_value=obj)

        with patch("app.pipelines.tools.compleasm", return_value=missing):
            with pytest.raises(PermanentError):
                await pipeline_service.launch_completeness(
                    object_id=obj.id, owner="local"
                )
        get_object.assert_not_awaited()


class TestLaunchLineageDownloadReachesTheQueue:
    async def test_download_request_reaches_the_queue(self):
        enqueued = {}

        async def _enqueue(job_type, **kwargs):
            enqueued["type"] = job_type
            enqueued.update(kwargs)
            return SimpleNamespace(id="job1")

        with (
            patch("app.pipelines.tools.compleasm", return_value=_COMPLEASM_TOOL),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            job = await pipeline_service.launch_lineage_download(
                lineage="bacteria", owner="local"
            )

        assert job.id == "job1"
        assert enqueued["type"] == "download_lineage"
        assert enqueued["payload"] == {"lineage": "bacteria", "odb": "odb12"}

    async def test_odb_defaults_from_the_registry(self):
        enqueued = {}

        async def _enqueue(job_type, **kwargs):
            enqueued.update(kwargs)
            return SimpleNamespace(id="job1")

        with (
            patch("app.pipelines.tools.compleasm", return_value=_COMPLEASM_TOOL),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            await pipeline_service.launch_lineage_download(
                lineage="eukaryota", odb="odb10", owner="local"
            )

        assert enqueued["payload"]["odb"] == "odb10"
