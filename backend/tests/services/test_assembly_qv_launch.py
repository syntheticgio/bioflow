"""launch_qv_qc's read-set resolution and payload contract with its handler.

The handler (`app/queue/assembly_qc_handlers.py`, `assess_assembly_qv`) reads
`payload["reads"]` as a list of `{"read_sha256"/"read_path": ...}` entries via
`_resolve_read_inputs`, `payload["read_db_path"]` as an optional prebuilt meryl
database directory, and `payload["k"]`/`payload["threads"]` as plain ints. This
test locks those keys down the same way `test_assembly_error_qc_launch.py`
locks the BAI prefix keys -- a renamed key on one side and not the other would
only surface at runtime against a real read set.

It also covers the "ambiguity is unavailable, not a guess" rule: a project
with several read sets refuses rather than picks one, matching
`launch_assembly_error_qc` and `launch_polish`. And it covers the cache path:
`_materialize_meryl_cache` is exercised directly against a real filesystem
sidecar group, since that reconstruction logic is new and has no existing
sibling to lean on for confidence.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId

from app.config import settings
from app.errors import ValidationError
from app.models import FormatKind, ObjectStatus, SidecarRole
from app.pipelines.tools import Tool
from app.services import pipeline_service

_MERYL = Tool(name="meryl", path="/usr/local/bin/meryl", version="1.4.1")
_MERQURY = Tool(name="merqury", path="/usr/local/bin/merqury.sh", version="1.3")


def _obj(
    *, name, kind=FormatKind.FASTQ, status=ObjectStatus.READY, project_id=None,
    owner="local", role=None, mate_object_id=None, facts=None,
):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name=name,
        format=SimpleNamespace(kind=kind),
        status=status,
        facts=facts or {},
        project_id=project_id or PydanticObjectId(),
        owner=owner,
        derived_from=[],
        blob_sha256="a" * 64,
        role=role,
        mate_object_id=mate_object_id,
    )


def _assembly():
    return _obj(name="draft.fasta", kind=FormatKind.FASTA)


def _reads(*, name="reads.fastq.gz", project_id=None, mate_object_id=None):
    return _obj(
        name=name, kind=FormatKind.FASTQ, project_id=project_id, mate_object_id=mate_object_id
    )


async def _run(
    *, assembly, read_sets, read_object_id=None, k=None,
    list_sidecars=None, enqueue_returns_job=True,
):
    objects = {assembly.id: assembly}
    for group in read_sets:
        for o in group:
            objects[o.id] = o

    async def _get_object(object_id, *, owner):
        return objects[object_id]

    async def _list_objects(project_id, *, owner, status=None):
        return [o for group in read_sets for o in group]

    async def _list_sidecars_default(object_id, *, owner):
        return []

    enqueued = {}

    async def _enqueue(job_type, **kwargs):
        enqueued["type"] = job_type
        enqueued.update(kwargs)
        if not enqueue_returns_job:
            return None
        return SimpleNamespace(id="job1")

    with (
        patch("app.pipelines.tools.meryl", return_value=_MERYL),
        patch("app.pipelines.tools.merqury", return_value=_MERQURY),
        patch("app.services.object_service.get_object", AsyncMock(side_effect=_get_object)),
        patch("app.services.object_service.list_objects", AsyncMock(side_effect=_list_objects)),
        patch(
            "app.services.object_service.list_sidecars",
            AsyncMock(side_effect=list_sidecars or _list_sidecars_default),
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("a" * 64, None)),
        ),
        patch("app.queue.queue.enqueue", _enqueue),
        # A generous admission budget so this file's tests -- about read-set
        # resolution and caching, not the declared-budget refusal -- reach
        # the queue rather than being refused by QV_QC_MEM_MB (#527). See
        # test_declared_budget_refusal.py for the refusal's own coverage.
        patch(
            "app.services.pipeline_service.current_admission_budget_mb",
            AsyncMock(return_value=10_000_000),
        ),
    ):
        job = await pipeline_service.launch_qv_qc(
            assembly.id, owner="local", read_object_id=read_object_id, k=k,
        )
    return job, enqueued


class TestReadSetResolution:
    async def test_happy_path_with_a_single_unambiguous_read_set(self):
        assembly = _assembly()
        reads = _reads(project_id=assembly.project_id)

        job, enqueued = await _run(assembly=assembly, read_sets=[[reads]])

        assert enqueued["type"] == "assess_assembly_qv"
        payload = enqueued["payload"]
        assert payload["object_id"] == str(assembly.id)
        assert payload["read_object_id"] == str(reads.id)
        assert payload["k"] == pipeline_service.DEFAULT_MERYL_K
        # read_name rides along so the handler can link this file under its
        # own real extension -- see _resolve_read_inputs's docstring for why
        # a hardcoded ".fastq.gz" silently produced an empty k-mer database
        # against a real plain-text FASTQ, confirmed against a real run.
        assert payload["reads"] == [
            {"read_name": reads.name, "read_sha256": "a" * 64}
        ]
        assert "read_db_path" not in payload

    async def test_ambiguity_raises_validation_error(self):
        assembly = _assembly()
        reads_a = _reads(name="a.fastq.gz", project_id=assembly.project_id)
        reads_b = _reads(name="b.fastq.gz", project_id=assembly.project_id)

        with pytest.raises(ValidationError):
            await _run(assembly=assembly, read_sets=[[reads_a], [reads_b]])

    async def test_no_read_sets_raises_validation_error(self):
        assembly = _assembly()
        with pytest.raises(ValidationError):
            await _run(assembly=assembly, read_sets=[])

    async def test_explicit_read_object_id_bypasses_ambiguity(self):
        assembly = _assembly()
        reads_a = _reads(name="a.fastq.gz", project_id=assembly.project_id)
        reads_b = _reads(name="b.fastq.gz", project_id=assembly.project_id)

        job, enqueued = await _run(
            assembly=assembly,
            read_sets=[[reads_a], [reads_b]],
            read_object_id=reads_a.id,
        )

        assert enqueued["payload"]["read_object_id"] == str(reads_a.id)

    async def test_explicit_read_object_id_from_another_project_is_rejected(self):
        """get_object scopes by owner, not project -- without this check a
        read set from a different project of the same owner would enqueue
        without error and score this assembly's QV against reads it has
        nothing to do with, the exact 'plausible, confidently wrong' outcome
        the ambiguity rule exists to prevent."""
        assembly = _assembly()
        other_project_reads = _reads(name="other.fastq.gz")

        with pytest.raises(ValidationError):
            await _run(
                assembly=assembly,
                read_sets=[[other_project_reads]],
                read_object_id=other_project_reads.id,
            )

    async def test_explicit_read_object_id_pulls_in_its_mate(self):
        assembly = _assembly()
        mate = _reads(name="r2.fastq.gz", project_id=assembly.project_id)
        primary = _reads(
            name="r1.fastq.gz", project_id=assembly.project_id, mate_object_id=mate.id,
        )

        job, enqueued = await _run(
            assembly=assembly,
            read_sets=[[primary, mate]],
            read_object_id=primary.id,
        )

        assert len(enqueued["payload"]["reads"]) == 2

    async def test_custom_k_is_passed_through(self):
        assembly = _assembly()
        reads = _reads(project_id=assembly.project_id)

        job, enqueued = await _run(assembly=assembly, read_sets=[[reads]], k=17)

        assert enqueued["payload"]["k"] == 17

    async def test_dedup_key_includes_assembly_reads_and_k(self):
        assembly = _assembly()
        reads = _reads(project_id=assembly.project_id)

        job, enqueued = await _run(assembly=assembly, read_sets=[[reads]])

        assert enqueued["dedup_key"] == (
            f"assess_assembly_qv:{assembly.id}:{reads.id}:"
            f"{pipeline_service.DEFAULT_MERYL_K}"
        )

    async def test_resources_match_task_4s_handler_registration(self):
        assembly = _assembly()
        reads = _reads(project_id=assembly.project_id)

        job, enqueued = await _run(assembly=assembly, read_sets=[[reads]])

        assert enqueued["resources"].cpu == 4
        assert enqueued["resources"].mem_mb == 12288


class TestMerylCacheMaterialization:
    """`_materialize_meryl_cache` reassembles a flat MERYL_DB sidecar group
    back into a directory. This is new logic with no existing sibling, so it
    gets its own direct coverage rather than only being exercised indirectly
    through the launch path.
    """

    @pytest.fixture(autouse=True)
    def _isolated_home(self, monkeypatch, tmp_path: Path):
        """Redirect this class's blob writes off the real storage home.

        `_member` below writes real bytes where a managed blob would live, and
        `_materialize_meryl_cache` reads them back through `blob_path()` before
        reassembling the database under `settings.tmp_dir`. Unpatched, both land
        in the `/data` tree shared with the running stack (shared deliberately,
        per CLAUDE.md) -- and since `_member` never calls `create_blob_record`,
        every run left files that #412's drift sweep then reported as
        `orphaned_file`. `objects_dir` and `tmp_dir` are read-only properties
        derived from `bioinfo_home`, so that field is the one seam that moves
        both.
        """
        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")

    def _member(
        self, *, db_name, rel, k, owner, project_id, content=b"x", digest=None,
        expected_count=1,
    ):
        digest = digest or "b" * 64
        blob_dir = settings.objects_dir / digest[:2]
        blob_dir.mkdir(parents=True, exist_ok=True)
        blob_file = blob_dir / digest
        if not blob_file.exists():
            blob_file.write_bytes(content)
        return SimpleNamespace(
            id=PydanticObjectId(),
            name=f"{db_name}__{rel}",
            owner=owner,
            project_id=project_id,
            sidecar_role=SidecarRole.MERYL_DB,
            facts={
                "meryl_db_k": k,
                "meryl_db_name": db_name,
                "meryl_db_expected_count": expected_count,
            },
            blob_sha256=digest,
        )

    async def test_no_sidecars_returns_none(self):
        reads = _reads()

        async def _list_sidecars(object_id, *, owner):
            return []

        with patch(
            "app.services.object_service.list_sidecars",
            AsyncMock(side_effect=_list_sidecars),
        ):
            result = await pipeline_service._materialize_meryl_cache(
                reads, 21, owner="local"
            )
        assert result is None

    async def test_cache_at_wrong_k_is_ignored(self):
        reads = _reads()
        member = self._member(
            db_name="reads.meryl", rel="0x000000.merylIndex", k=17,
            owner="local", project_id=reads.project_id,
        )

        async def _list_sidecars(object_id, *, owner):
            return [member]

        with (
            patch(
                "app.services.object_service.list_sidecars",
                AsyncMock(side_effect=_list_sidecars),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(member.blob_sha256, None)),
            ),
        ):
            result = await pipeline_service._materialize_meryl_cache(
                reads, 21, owner="local"
            )
        assert result is None

    async def test_complete_cache_materializes_into_a_directory(self):
        reads = _reads()
        members = [
            self._member(
                db_name="reads.meryl", rel=f"0x{i:06x}.merylIndex", k=21,
                owner="local", project_id=reads.project_id, content=bytes([i]) * 8,
                digest=f"{i}" * 64, expected_count=3,
            )
            for i in range(3)
        ]

        async def _list_sidecars(object_id, *, owner):
            return members

        async def _resolve(obj):
            return obj.blob_sha256, None

        with (
            patch(
                "app.services.object_service.list_sidecars",
                AsyncMock(side_effect=_list_sidecars),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(side_effect=_resolve),
            ),
        ):
            result = await pipeline_service._materialize_meryl_cache(
                reads, 21, owner="local"
            )

        assert result is not None
        assert result.is_dir()
        linked = sorted(p.name for p in result.iterdir())
        assert linked == [f"0x{i:06x}.merylIndex" for i in range(3)]

    async def test_missing_blob_falls_back_to_none_rather_than_crashing(self):
        """A member whose blob is not actually on disk (a partial or
        corrupted cache) must not be handed to the handler as if it were
        complete -- that would let Merqury run against a broken database and
        produce a wrong QV silently. The safe outcome is "rebuild"."""
        reads = _reads()
        member = self._member(
            db_name="reads.meryl", rel="0x000000.merylIndex", k=21,
            owner="local", project_id=reads.project_id,
        )

        async def _list_sidecars(object_id, *, owner):
            return [member]

        async def _resolve_missing(obj):
            # A digest that was never written to the object store.
            return "f" * 64, None

        with (
            patch(
                "app.services.object_service.list_sidecars",
                AsyncMock(side_effect=_list_sidecars),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(side_effect=_resolve_missing),
            ),
        ):
            result = await pipeline_service._materialize_meryl_cache(
                reads, 21, owner="local"
            )
        assert result is None

    async def test_partial_group_smaller_than_expected_falls_back_to_none(self):
        """A group that lost members to a partial ingest (results.py's
        meryl_db_partially_applied case) must not be materialized as if it
        were complete -- Merqury scoring a truncated database would produce
        a confidently wrong QV with nothing to say so afterward. Two members
        both claim expected_count=3 (one was dropped during ingest), so the
        group's actual size never matches what every member says it should
        be, and the cache must be refused."""
        reads = _reads()
        members = [
            self._member(
                db_name="reads.meryl", rel=f"0x{i:06x}.merylIndex", k=21,
                owner="local", project_id=reads.project_id, content=bytes([i]) * 8,
                digest=f"{i}" * 64, expected_count=3,
            )
            for i in range(2)
        ]

        async def _list_sidecars(object_id, *, owner):
            return members

        async def _resolve(obj):
            return obj.blob_sha256, None

        # _resolve_readable is patched so the expected-count refusal is the
        # only reason this can return None. Without the patch, the real
        # _resolve_readable fails on these stub members and the function
        # returns None for that unrelated reason -- verified by mutation:
        # with the expected-count check deleted outright, this test still
        # passed until the patch made the guard the thing actually under
        # test.
        with (
            patch(
                "app.services.object_service.list_sidecars",
                AsyncMock(side_effect=_list_sidecars),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(side_effect=_resolve),
            ),
        ):
            result = await pipeline_service._materialize_meryl_cache(
                reads, 21, owner="local"
            )
        assert result is None

    async def test_member_name_not_matching_db_prefix_falls_back_to_none(self):
        """A sidecar whose stored name does not carry its own db_name prefix
        cannot be placed at a correct relative path -- treated as corrupt
        rather than guessed at."""
        reads = _reads()
        member = self._member(
            db_name="reads.meryl", rel="0x000000.merylIndex", k=21,
            owner="local", project_id=reads.project_id,
        )
        member.name = "totally-unrelated-name"

        async def _list_sidecars(object_id, *, owner):
            return [member]

        with (
            patch(
                "app.services.object_service.list_sidecars",
                AsyncMock(side_effect=_list_sidecars),
            ),
            patch(
                "app.services.pipeline_service._resolve_readable",
                AsyncMock(return_value=(member.blob_sha256, None)),
            ),
        ):
            result = await pipeline_service._materialize_meryl_cache(
                reads, 21, owner="local"
            )
        assert result is None


class TestQvLaunchUsesCache:
    async def test_found_cache_sets_read_db_path_on_payload(self):
        assembly = _assembly()
        reads = _reads(project_id=assembly.project_id)
        fake_dir = settings.tmp_dir / "fake-cache-dir"

        with patch(
            "app.services.pipeline_service._materialize_meryl_cache",
            AsyncMock(return_value=fake_dir),
        ):
            job, enqueued = await _run(assembly=assembly, read_sets=[[reads]])

        assert enqueued["payload"]["read_db_path"] == str(fake_dir)

    async def test_no_cache_omits_read_db_path(self):
        assembly = _assembly()
        reads = _reads(project_id=assembly.project_id)

        with patch(
            "app.services.pipeline_service._materialize_meryl_cache",
            AsyncMock(return_value=None),
        ):
            job, enqueued = await _run(assembly=assembly, read_sets=[[reads]])

        assert "read_db_path" not in enqueued["payload"]
