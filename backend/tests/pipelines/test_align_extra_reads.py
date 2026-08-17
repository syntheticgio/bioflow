"""Additional read sets threaded from launch_alignment through to the aligner.

A file-level launch can carry additional read sets alongside the primary
pair: the launcher resolves each set's members, validates them, and puts them
in the job payload, and the queue-side handler concatenates every set's R1
bytes into the primary R1 stream (and every set's R2 bytes into the R2
stream) before the aligner runs -- since none of the six aligners
align_runner drives take several read files positionally or share one
multi-file convention. See align_handlers._concatenate_reads for the full
reasoning.
"""

import gzip
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.errors import PermanentError, ValidationError
from app.models import FormatKind, ObjectStatus
from app.queue.align_handlers import _concatenate_reads, _extra_reads_paths
from app.services import memory_estimate, pipeline_service
from beanie import PydanticObjectId


def _memory_estimate(mb: int) -> memory_estimate.MemoryEstimate:
    return memory_estimate.MemoryEstimate(
        mb=mb,
        source=memory_estimate.EstimateSource.HEURISTIC,
        detail="from published tool coefficients",
    )


def _fastq_object(object_id, name, *, project_id):
    return SimpleNamespace(
        id=object_id,
        name=name,
        format=SimpleNamespace(kind=FormatKind.FASTQ),
        role=None,
        facts={},
        metadata={},
        status=ObjectStatus.READY,
        project_id=project_id,
        owner="local",
        size=1_000_000,
    )


class TestLaunchAlignmentCarriesExtraReads:
    """launch_alignment must resolve additional_read_sets the same way it
    resolves the primary read, and place the result on the job payload --
    following the same override-skips-the-refusal mocking pattern as
    test_launch_resource_refusal.py's TestAlignmentRefusal."""

    async def test_extra_reads_reach_the_job_payload(self):
        project_id = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "sample_R1.fastq", project_id=project_id)
        extra = _fastq_object(PydanticObjectId(), "sample_chunk2.fastq", project_id=project_id)
        reference = SimpleNamespace(
            id=PydanticObjectId(),
            name="ref.fasta",
            format=SimpleNamespace(kind=FormatKind.FASTA),
            role=None,
            facts={},
            status=ObjectStatus.READY,
            project_id=project_id,
            owner="local",
            size=3_000_000_000,
        )

        enqueued = {}

        async def _enqueue(job_type, **kwargs):
            enqueued["type"] = job_type
            enqueued.update(kwargs)
            return SimpleNamespace(id=PydanticObjectId())

        async def _get_object(object_id, owner):
            for obj in (primary, extra, reference):
                if obj.id == object_id:
                    return obj
            raise AssertionError(f"unexpected object id {object_id}")

        def _resolve_readable_side_effect(obj):
            # A distinct, checkable path per object rather than (None, None)
            # for everything, so the test can assert extra_reads carries the
            # *right* entry rather than merely "some" entry.
            return (None, f"/data/{obj.name}")

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=_get_object),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                AsyncMock(return_value={"minimap2": True, "fai": True}),
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(1024)),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(side_effect=_resolve_readable_side_effect),
            ),
            patch(
                "app.services.pipeline_service.sidecar_payload",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            await pipeline_service.launch_alignment(
                object_id=primary.id,
                reference_id=reference.id,
                owner="local",
                paired=False,
                additional_read_sets=[(extra.id, None)],
            )

        payload = enqueued["payload"]
        assert payload["extra_reads"] == [
            {"name": "sample_chunk2.fastq", "path": "/data/sample_chunk2.fastq"}
        ]
        # The primary read's own resolution is untouched by the extra-reads
        # feature -- it must still carry its own path under r1_path.
        assert payload["r1_path"] == "/data/sample_R1.fastq"

    async def test_no_extra_reads_means_no_payload_key(self):
        """The common case (one read file, no chunking) must not grow a
        payload key nothing consumes -- extra_reads should be entirely
        absent, not an empty list, mirroring how mate_object_id is omitted
        rather than set to None."""
        project_id = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "sample_R1.fastq", project_id=project_id)
        reference = SimpleNamespace(
            id=PydanticObjectId(),
            name="ref.fasta",
            format=SimpleNamespace(kind=FormatKind.FASTA),
            role=None,
            facts={},
            status=ObjectStatus.READY,
            project_id=project_id,
            owner="local",
            size=3_000_000_000,
        )

        enqueued = {}

        async def _enqueue(job_type, **kwargs):
            enqueued["type"] = job_type
            enqueued.update(kwargs)
            return SimpleNamespace(id=PydanticObjectId())

        async def _get_object(object_id, owner):
            for obj in (primary, reference):
                if obj.id == object_id:
                    return obj
            raise AssertionError(f"unexpected object id {object_id}")

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=_get_object),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                AsyncMock(return_value={"minimap2": True, "fai": True}),
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(1024)),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(None, None)),
            ),
            patch(
                "app.services.pipeline_service.sidecar_payload",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            await pipeline_service.launch_alignment(
                object_id=primary.id,
                reference_id=reference.id,
                owner="local",
                paired=False,
            )

        assert "extra_reads" not in enqueued["payload"]

    async def test_extra_read_id_equal_to_primary_is_rejected(self):
        """Passing the primary object's own id as an extra read would
        silently duplicate every one of its reads in the concatenated
        output -- the same failure the mate check already guards against
        for mate_object_id == object_id."""
        project_id = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "sample_R1.fastq", project_id=project_id)
        reference = SimpleNamespace(
            id=PydanticObjectId(),
            name="ref.fasta",
            format=SimpleNamespace(kind=FormatKind.FASTA),
            role=None,
            facts={},
            status=ObjectStatus.READY,
            project_id=project_id,
            owner="local",
            size=3_000_000_000,
        )

        async def _get_object(object_id, owner):
            for obj in (primary, reference):
                if obj.id == object_id:
                    return obj
            raise AssertionError(f"unexpected object id {object_id}")

        with patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=_get_object),
        ):
            with pytest.raises(ValidationError):
                await pipeline_service.launch_alignment(
                    object_id=primary.id,
                    reference_id=reference.id,
                    owner="local",
                    paired=False,
                    additional_read_sets=[(primary.id, None)],
                )

    async def test_extra_read_id_equal_to_mate_is_rejected(self):
        project_id = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "sample_R1.fastq", project_id=project_id)
        mate = _fastq_object(PydanticObjectId(), "sample_R2.fastq", project_id=project_id)
        reference = SimpleNamespace(
            id=PydanticObjectId(),
            name="ref.fasta",
            format=SimpleNamespace(kind=FormatKind.FASTA),
            role=None,
            facts={},
            status=ObjectStatus.READY,
            project_id=project_id,
            owner="local",
            size=3_000_000_000,
        )

        async def _get_object(object_id, owner):
            for obj in (primary, mate, reference):
                if obj.id == object_id:
                    return obj
            raise AssertionError(f"unexpected object id {object_id}")

        with patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=_get_object),
        ):
            with pytest.raises(ValidationError):
                await pipeline_service.launch_alignment(
                    object_id=primary.id,
                    reference_id=reference.id,
                    owner="local",
                    mate_object_id=mate.id,
                    paired=True,
                    additional_read_sets=[(mate.id, None)],
                )

    async def test_duplicate_extra_read_ids_are_rejected(self):
        project_id = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "sample_R1.fastq", project_id=project_id)
        extra = _fastq_object(PydanticObjectId(), "sample_chunk2.fastq", project_id=project_id)
        reference = SimpleNamespace(
            id=PydanticObjectId(),
            name="ref.fasta",
            format=SimpleNamespace(kind=FormatKind.FASTA),
            role=None,
            facts={},
            status=ObjectStatus.READY,
            project_id=project_id,
            owner="local",
            size=3_000_000_000,
        )

        async def _get_object(object_id, owner):
            for obj in (primary, extra, reference):
                if obj.id == object_id:
                    return obj
            raise AssertionError(f"unexpected object id {object_id}")

        with patch(
            "app.services.object_service.get_object",
            AsyncMock(side_effect=_get_object),
        ):
            with pytest.raises(ValidationError):
                await pipeline_service.launch_alignment(
                    object_id=primary.id,
                    reference_id=reference.id,
                    owner="local",
                    paired=False,
                    additional_read_sets=[(extra.id, None), (extra.id, None)],
                )


class TestConcatenateReads:
    """The runner-side concatenation step, tested against real files rather
    than mocks -- it must produce a file containing every input's content,
    primary first, handling gzip correctly regardless of which inputs (if
    any) are actually compressed."""

    def test_concatenates_plain_text_files_in_order(self, tmp_path):
        primary = tmp_path / "primary.fastq"
        primary.write_bytes(b"@r1\nACGT\n+\nIIII\n")
        extra1 = tmp_path / "extra1"
        extra1.write_bytes(b"@r2\nTTTT\n+\nIIII\n")
        extra2 = tmp_path / "extra2"
        extra2.write_bytes(b"@r3\nGGGG\n+\nIIII\n")

        dest = tmp_path / "combined.fastq"
        out = _concatenate_reads(primary, [extra1, extra2], dest)

        assert out == dest
        assert out.read_bytes() == (
            b"@r1\nACGT\n+\nIIII\n@r2\nTTTT\n+\nIIII\n@r3\nGGGG\n+\nIIII\n"
        )

    def test_gzipped_primary_produces_a_gzipped_output_from_plain_extras(
        self, tmp_path
    ):
        """Extras resolved from managed blobs have no extension and are not
        necessarily compressed the same way as the primary -- the primary's
        compression state is what the aligner downstream will see, since the
        combined file is named and handed to it directly."""
        primary = tmp_path / "primary.fastq.gz"
        with gzip.open(primary, "wb") as f:
            f.write(b"@r1\nACGT\n+\nIIII\n")
        extra = tmp_path / "extra"
        extra.write_bytes(b"@r2\nTTTT\n+\nIIII\n")

        dest = tmp_path / "combined.fastq.gz"
        out = _concatenate_reads(primary, [extra], dest)

        with gzip.open(out, "rb") as f:
            content = f.read()
        assert content == b"@r1\nACGT\n+\nIIII\n@r2\nTTTT\n+\nIIII\n"

    def test_gzipped_extra_is_decompressed_into_a_plain_primary(self, tmp_path):
        primary = tmp_path / "primary.fastq"
        primary.write_bytes(b"@r1\nACGT\n+\nIIII\n")
        extra = tmp_path / "extra"
        with gzip.open(extra, "wb") as f:
            f.write(b"@r2\nTTTT\n+\nIIII\n")

        dest = tmp_path / "combined.fastq"
        out = _concatenate_reads(primary, [extra], dest)

        assert out.read_bytes() == b"@r1\nACGT\n+\nIIII\n@r2\nTTTT\n+\nIIII\n"

    def test_sniffs_gzip_by_content_not_extension(self, tmp_path):
        """A managed blob resolved from the store has no extension at all --
        gzip-ness must be detected from magic bytes, exactly like
        align_handlers._is_gzip already does for reference files."""
        primary = tmp_path / "primary_no_ext"
        with gzip.open(primary, "wb") as f:
            f.write(b"@r1\nACGT\n+\nIIII\n")
        extra = tmp_path / "extra_no_ext"
        with gzip.open(extra, "wb") as f:
            f.write(b"@r2\nTTTT\n+\nIIII\n")

        dest = tmp_path / "combined"
        out = _concatenate_reads(primary, [extra], dest)

        with gzip.open(out, "rb") as f:
            content = f.read()
        assert content == b"@r1\nACGT\n+\nIIII\n@r2\nTTTT\n+\nIIII\n"

    def test_no_extras_still_produces_the_primary_content(self, tmp_path):
        primary = tmp_path / "primary.fastq"
        primary.write_bytes(b"@r1\nACGT\n+\nIIII\n")

        dest = tmp_path / "combined.fastq"
        out = _concatenate_reads(primary, [], dest)

        assert out.read_bytes() == b"@r1\nACGT\n+\nIIII\n"


class TestExtraReadsPaths:
    """The payload-to-stream split: every set's own file feeds the R1 stream
    and, in a paired run, every set's mate feeds the R2 stream -- a mateless
    set in a paired run is a launch that slipped past validation, refused
    rather than silently misaligning."""

    def _entry(self, tmp_path, name, with_mate=False):
        path = tmp_path / name
        path.write_bytes(b"reads")
        entry = {"name": name, "path": str(path)}
        if with_mate:
            mate = tmp_path / f"{name}.mate"
            mate.write_bytes(b"mate")
            entry["mate_path"] = str(mate)
            entry["mate_name"] = f"{name}.mate"
        return entry

    def test_single_end_run_collects_only_the_r1s(self, tmp_path):
        entries = [
            self._entry(tmp_path, "b.fastq"),
            self._entry(tmp_path, "c.fastq"),
        ]

        r1_paths, r2_paths = _extra_reads_paths(entries, paired=False)

        assert [p.name for p in r1_paths] == ["b.fastq", "c.fastq"]
        assert r2_paths == []

    def test_paired_run_collects_r1s_and_mates_separately(self, tmp_path):
        entries = [
            self._entry(tmp_path, "b_R1.fastq", with_mate=True),
            self._entry(tmp_path, "c_R1.fastq", with_mate=True),
        ]

        r1_paths, r2_paths = _extra_reads_paths(entries, paired=True)

        assert [p.name for p in r1_paths] == ["b_R1.fastq", "c_R1.fastq"]
        assert [p.name for p in r2_paths] == ["b_R1.fastq.mate", "c_R1.fastq.mate"]

    def test_paired_run_refuses_a_mateless_set(self, tmp_path):
        entries = [
            self._entry(tmp_path, "b_R1.fastq", with_mate=True),
            self._entry(tmp_path, "c_R1.fastq"),
        ]

        with pytest.raises(PermanentError, match="c_R1.fastq.*no mate"):
            _extra_reads_paths(entries, paired=True)

    def test_paired_run_ignores_a_mate_carried_by_a_single_end_entry(self, tmp_path):
        """The mate keys ride along on the entry, so they are only read when
        the run is paired -- a launch that validated as single-end must not
        suddenly treat the same entry as paired at dispatch."""
        entries = [self._entry(tmp_path, "b.fastq", with_mate=True)]

        r1_paths, r2_paths = _extra_reads_paths(entries, paired=False)

        assert [p.name for p in r1_paths] == ["b.fastq"]
        assert r2_paths == []


class TestResolveAdditionalReadSets:
    """The pairing rule and whole-request uniqueness, tested against the
    resolver itself rather than through a full launch: pairing is a property
    of the run, decided by the primary pair -- so a paired run requires every
    additional set to have a mate (suggested like the primary's, or the
    launch fails), a single-end run forbids any set from declaring one, and
    no file may appear twice anywhere in the launch."""

    async def _resolve(
        self,
        *,
        primary,
        mate=None,
        sets=(),
        paired=True,
        suggested=None,
    ):
        objects = {primary.id: primary}
        if mate is not None:
            objects[mate.id] = mate
        for r1, r2 in sets:
            objects[r1.id] = r1
            if r2 is not None:
                objects[r2.id] = r2

        async def _get_object(object_id, owner):
            return objects[object_id]

        async def _suggest(obj):
            return (suggested or {}).get(obj.id)

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=_get_object),
            ),
            patch(
                "app.services.pipeline_service.suggest_mate",
                AsyncMock(side_effect=_suggest),
            ),
        ):
            return await pipeline_service._resolve_alignment_read_sets(
                primary=primary,
                mate_object_id=mate.id if mate is not None else None,
                additional_sets=[
                    (r1.id, r2.id if r2 is not None else None) for r1, r2 in sets
                ],
                owner="local",
                paired=paired,
            )

    async def test_a_paired_run_suggests_a_mate_for_each_set(self):
        """R-3: an additional set resolves its mate exactly like the primary
        -- explicit link, else suggest_mate -- so the set is a pair."""
        pid = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "a_R1.fastq", project_id=pid)
        primary_mate = _fastq_object(PydanticObjectId(), "a_R2.fastq", project_id=pid)
        extra = _fastq_object(PydanticObjectId(), "b_R1.fastq", project_id=pid)
        extra_mate = _fastq_object(PydanticObjectId(), "b_R2.fastq", project_id=pid)

        sets = await self._resolve(
            primary=primary,
            mate=primary_mate,
            sets=[(extra, None)],
            suggested={extra.id: extra_mate},
        )

        assert len(sets) == 2
        assert sets[1].r1.id == extra.id
        assert sets[1].r2.id == extra_mate.id

    async def test_a_paired_run_rejects_a_set_with_no_mate(self):
        """R-4: a paired run with a mateless additional set fails naming the
        set, rather than silently concatenating it into a run it breaks."""
        pid = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "a_R1.fastq", project_id=pid)
        primary_mate = _fastq_object(PydanticObjectId(), "a_R2.fastq", project_id=pid)
        extra = _fastq_object(PydanticObjectId(), "b_R1.fastq", project_id=pid)

        with pytest.raises(ValidationError, match="no mate"):
            await self._resolve(
                primary=primary,
                mate=primary_mate,
                sets=[(extra, None)],
            )

    async def test_the_workflow_nodes_chunks_are_exempt_from_the_rule(self):
        """The workflow align node's `reads` port is multi for chunked/split
        reads of the primary's own library -- pieces, not libraries -- so its
        sets carry the non-strict flag and a paired run accepts them mateless,
        concatenated into the run's R1 stream as they always were."""
        pid = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "a_R1.fastq", project_id=pid)
        primary_mate = _fastq_object(PydanticObjectId(), "a_R2.fastq", project_id=pid)
        chunk = _fastq_object(PydanticObjectId(), "a_chunk2.fastq", project_id=pid)

        objects = {primary.id: primary, primary_mate.id: primary_mate, chunk.id: chunk}

        async def _get_object(object_id, owner):
            return objects[object_id]

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=_get_object),
            ),
            # A chunk name carries no pairing scheme, so the real suggest_mate
            # would return None for it anyway; stubbing keeps the test off the
            # mate_object_id attribute the production objects carry.
            patch(
                "app.services.pipeline_service.suggest_mate",
                AsyncMock(return_value=None),
            ),
        ):
            sets = await pipeline_service._resolve_alignment_read_sets(
                primary=primary,
                mate_object_id=primary_mate.id,
                additional_sets=[(chunk.id, None, False)],
                owner="local",
                paired=True,
            )

        assert len(sets) == 2
        assert sets[1].r1.id == chunk.id
        assert sets[1].r2 is None

    async def test_a_single_end_run_rejects_a_set_that_declares_a_mate(self):
        """R-4: the reverse direction -- a set carrying a mate in a
        single-end run is refused rather than silently dropped."""
        pid = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "a_R1.fastq", project_id=pid)
        extra = _fastq_object(PydanticObjectId(), "b_R1.fastq", project_id=pid)
        extra_mate = _fastq_object(PydanticObjectId(), "b_R2.fastq", project_id=pid)

        with pytest.raises(ValidationError, match="single-end"):
            await self._resolve(
                primary=primary,
                sets=[(extra, extra_mate)],
                paired=False,
            )

    async def test_no_file_may_appear_twice_in_a_launch(self):
        """R-5: whole-request uniqueness -- a set member that is the primary's
        mate (or the primary, or another set's member) is refused."""
        pid = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "a_R1.fastq", project_id=pid)
        primary_mate = _fastq_object(PydanticObjectId(), "a_R2.fastq", project_id=pid)

        with pytest.raises(ValidationError, match="used twice"):
            await self._resolve(
                primary=primary,
                mate=primary_mate,
                sets=[(primary_mate, None)],
            )

    async def test_the_r1_leads_swap_applies_per_set(self):
        """A set added R2-first normalizes to R1-first like the primary pair,
        so its R1 leads in the concatenated stream."""
        pid = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "a_R1.fastq", project_id=pid)
        primary_mate = _fastq_object(PydanticObjectId(), "a_R2.fastq", project_id=pid)
        r2_first = _fastq_object(PydanticObjectId(), "b_R2.fastq", project_id=pid)
        r1_file = _fastq_object(PydanticObjectId(), "b_R1.fastq", project_id=pid)

        sets = await self._resolve(
            primary=primary,
            mate=primary_mate,
            sets=[(r2_first, r1_file)],
        )

        assert sets[1].r1.id == r1_file.id
        assert sets[1].r2.id == r2_first.id


class TestLaunchDistinguishesReadSets:
    """R-8/R-9/R-14: additional sets are real inputs of a launch -- they
    count toward the memory estimate, they make two otherwise-identical
    launches distinct jobs, and the flat params channel is dead."""

    def _reference(self, project_id):
        return SimpleNamespace(
            id=PydanticObjectId(),
            name="ref.fasta",
            format=SimpleNamespace(kind=FormatKind.FASTA),
            role=None,
            facts={},
            status=ObjectStatus.READY,
            project_id=project_id,
            owner="local",
            size=3_000_000_000,
        )

    async def test_dedup_key_covers_every_set_member_in_order(self):
        """R-9: two launches that differ only in their additional sets must be
        distinct jobs -- the key carries every set's R1 then R2 id in order."""
        pid = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "a_R1.fastq", project_id=pid)
        primary_mate = _fastq_object(PydanticObjectId(), "a_R2.fastq", project_id=pid)
        extra = _fastq_object(PydanticObjectId(), "b_R1.fastq", project_id=pid)
        extra_mate = _fastq_object(PydanticObjectId(), "b_R2.fastq", project_id=pid)
        reference = self._reference(pid)

        keys: list[str] = []

        async def _enqueue(job_type, **kwargs):
            # The default aligner (bwa-mem2 when installed) has no index on
            # the patched status, so a build_index job is enqueued before the
            # alignment; only the align jobs carry the reads in their key.
            if job_type == "align_reads":
                keys.append(kwargs.get("dedup_key") or "")
            return SimpleNamespace(id=PydanticObjectId())

        async def _get_object(object_id, owner):
            for obj in (primary, primary_mate, extra, extra_mate, reference):
                if obj.id == object_id:
                    return obj
            raise AssertionError(f"unexpected object id {object_id}")

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=_get_object),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                AsyncMock(return_value={"minimap2": True, "fai": True}),
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(1024)),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(None, None)),
            ),
            patch(
                "app.services.pipeline_service.sidecar_payload",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            await pipeline_service.launch_alignment(
                object_id=primary.id,
                reference_id=reference.id,
                owner="local",
                mate_object_id=primary_mate.id,
                paired=True,
                additional_read_sets=[(extra.id, extra_mate.id)],
            )
            await pipeline_service.launch_alignment(
                object_id=primary.id,
                reference_id=reference.id,
                owner="local",
                mate_object_id=primary_mate.id,
                paired=True,
            )

        assert len(keys) == 2
        assert keys[0] != keys[1]
        assert keys[0].startswith(
            "align:"
            + ":".join(
                [
                    str(primary.id),
                    str(extra.id),
                    str(primary_mate.id),
                    str(extra_mate.id),
                    str(reference.id),
                ]
            )
        )

    async def test_memory_estimate_sums_every_set_member(self):
        """R-8: the estimate sees every R1 and R2 byte in the launch, not
        just the primary's."""
        pid = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "a_R1.fastq", project_id=pid)
        primary_mate = _fastq_object(PydanticObjectId(), "a_R2.fastq", project_id=pid)
        extra = _fastq_object(PydanticObjectId(), "b_R1.fastq", project_id=pid)
        extra_mate = _fastq_object(PydanticObjectId(), "b_R2.fastq", project_id=pid)
        reference = self._reference(pid)

        resolve_mock = AsyncMock(return_value=_memory_estimate(1024))

        async def _enqueue(job_type, **kwargs):
            return SimpleNamespace(id=PydanticObjectId())

        async def _get_object(object_id, owner):
            for obj in (primary, primary_mate, extra, extra_mate, reference):
                if obj.id == object_id:
                    return obj
            raise AssertionError(f"unexpected object id {object_id}")

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=_get_object),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                AsyncMock(return_value={"minimap2": True, "fai": True}),
            ),
            patch(
                "app.services.memory_estimate.resolve",
                resolve_mock,
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(None, None)),
            ),
            patch(
                "app.services.pipeline_service.sidecar_payload",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.services.run_service.create_run",
                AsyncMock(return_value=SimpleNamespace(id="run1", owner="local")),
            ),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            await pipeline_service.launch_alignment(
                object_id=primary.id,
                reference_id=reference.id,
                owner="local",
                mate_object_id=primary_mate.id,
                paired=True,
                additional_read_sets=[(extra.id, extra_mate.id)],
            )

        resolve_mock.assert_called()
        # The first resolve is the launch-time estimate (the index build and
        # the declared reservation call it again with the same reads); the
        # estimate must see every set member's bytes.
        assert (
            resolve_mock.call_args_list[0].kwargs["input_bytes"]
            == primary.size + primary_mate.size + extra.size + extra_mate.size
        )

    async def test_stale_params_extra_reads_is_rejected(self):
        """R-14: the flat params channel is dead -- a caller still sending it
        hears about the replacement field instead of silently losing files."""
        with pytest.raises(ValidationError, match="additional_read_sets"):
            await pipeline_service.launch_alignment(
                object_id=PydanticObjectId(),
                reference_id=PydanticObjectId(),
                owner="local",
                params={"extra_reads": ["some-object-id"]},
            )

    async def test_run_records_every_set_member_under_its_own_role(self):
        """R-15: the run's provenance lists each additional set as one input
        per file -- EXTRA_READS for the set's own file, EXTRA_MATE for its
        mate -- so a run stays describable after its inputs are deleted and
        the roles say which stream each file fed. The label counts the sets."""
        pid = PydanticObjectId()
        primary = _fastq_object(PydanticObjectId(), "a_R1.fastq", project_id=pid)
        primary_mate = _fastq_object(PydanticObjectId(), "a_R2.fastq", project_id=pid)
        extra = _fastq_object(PydanticObjectId(), "b_R1.fastq", project_id=pid)
        extra_mate = _fastq_object(PydanticObjectId(), "b_R2.fastq", project_id=pid)
        reference = self._reference(pid)

        create_run = AsyncMock(return_value=SimpleNamespace(id="run1", owner="local"))

        async def _enqueue(job_type, **kwargs):
            return SimpleNamespace(id=PydanticObjectId())

        async def _get_object(object_id, owner):
            for obj in (primary, primary_mate, extra, extra_mate, reference):
                if obj.id == object_id:
                    return obj
            raise AssertionError(f"unexpected object id {object_id}")

        with (
            patch(
                "app.services.object_service.get_object",
                AsyncMock(side_effect=_get_object),
            ),
            patch(
                "app.services.pipeline_service.reference_index_status",
                AsyncMock(return_value={"minimap2": True, "fai": True}),
            ),
            patch(
                "app.services.memory_estimate.resolve",
                AsyncMock(return_value=_memory_estimate(1024)),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(None, None)),
            ),
            patch(
                "app.services.pipeline_service.sidecar_payload",
                AsyncMock(return_value={}),
            ),
            patch("app.services.run_service.create_run", create_run),
            patch("app.services.run_service.link_job", AsyncMock()),
            patch("app.queue.queue.enqueue", _enqueue),
        ):
            await pipeline_service.launch_alignment(
                object_id=primary.id,
                reference_id=reference.id,
                owner="local",
                mate_object_id=primary_mate.id,
                paired=True,
                additional_read_sets=[(extra.id, extra_mate.id)],
            )

        inputs = create_run.call_args.kwargs["inputs"]
        assert [(i.role, i.name) for i in inputs] == [
            ("reads", "a_R1.fastq"),
            ("mate", "a_R2.fastq"),
            ("extra_reads", "b_R1.fastq"),
            ("extra_mate", "b_R2.fastq"),
            ("reference", "ref.fasta"),
        ]
        assert create_run.call_args.kwargs["label"] == (
            "a (paired) +1 read set → ref.fasta"
        )
