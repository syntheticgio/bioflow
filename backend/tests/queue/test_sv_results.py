"""`_apply_call_structural_variants`'s SV-index move.

The handler (`sv_handlers._build_sv_index`) builds the SQLite table under a
transient path inside the job's own scratch workdir -- it cannot know the
VCF's eventual object id, since ingest (which assigns one) happens later, in
this applier. The applier is therefore what has to move the file to its
permanent, VCF-keyed home once that id exists. These tests exercise that move
directly against `_apply_call_structural_variants`, independent of whether
Sniffles2 or bcftools are actually installed.
"""

import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.models import DataObject, ObjectRole, SidecarRole
from app.queue import results
from app.services import object_service, project_service

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]

OWNER = "sv-results-owner"


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    """Stub the enqueue `ingest_local_file` reaches, same as
    `test_results_owner.py`'s fixture of the same name -- these tests are
    not exercising the ingest-headers chain and should not need a live Redis
    to pass."""

    async def _skip_ingest(obj, **kwargs):
        return ""

    async def _skip_enqueue(*args, **kwargs):
        return None

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip_ingest)
    monkeypatch.setattr("app.queue.queue.enqueue", _skip_enqueue)


@pytest.fixture(scope="module", autouse=True)
def private_sv_stats_root(tmp_path_factory):
    """Give this module its own `sv_stats_dir` for its whole lifetime.

    `sv_stats_dir` is a read-only computed property (see app/config.py):
    patching it on the *instance*, the way `bioinfo_home` gets patched
    elsewhere, hits pydantic's own __setattr__/__delattr__ and raises "no
    attribute". Patching the property on the class bypasses that -- and
    leaves bioinfo_home (and therefore require_home()'s sentinel check,
    which ingest_local_file depends on) untouched, so ingest against the
    real configured home still succeeds.

    Module-scoped rather than per-test, which is the part that matters under
    xdist: the class is shared by every test in the *process*, so a
    per-test `patch.object(type(settings), ...)` opened a window in which an
    unrelated test running concurrently in the same worker read this
    module's temporary root as its own. That was an intermittent failure of
    `TestSvDbMove::test_snf_sidecar_is_ingested` -- roughly one run in four
    at -n12, never reproducible under -n0 or when selected alone. Holding
    the patch for the module closes the window without serializing anything.

    Same shape as `test_object_deletion.py`'s `private_report_roots`, and for
    the same underlying reason: a shared root plus a narrow patch is what
    makes a module hostile to whatever runs beside it.
    """
    root = tmp_path_factory.mktemp("sv-stats")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(type(settings), "sv_stats_dir", property(lambda _s: root))
        yield root


def _scratch_file(*, suffix: str = "") -> Path:
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    path = settings.tmp_dir / f"sv-results-{uuid.uuid4().hex}{suffix}"
    path.write_bytes(uuid.uuid4().bytes)
    return path


async def _bam(owner: str) -> DataObject:
    project = await project_service.create_project(
        name=f"{owner}-{uuid.uuid4().hex}", owner=owner
    )
    path = _scratch_file(suffix=".bam")
    return await object_service.ingest_local_file(
        owner=owner,
        project_id=project.id,
        path=path,
        name="aligned.bam",
        role=ObjectRole.ALIGNMENT,
    )


class TestSvDbMove:
    async def test_sv_db_is_moved_to_the_vcf_keyed_directory(
        self, tmp_path, private_sv_stats_root
    ):
        """The success path the review round flagged as untested: a db built
        under scratch lands at sv_stats_dir/<vcf_id>/sv.db, not anywhere
        keyed by the source BAM."""
        bam = await _bam(OWNER)

        # A scratch "workdir" the handler would have built the db under --
        # deliberately outside settings.sv_stats_dir, matching how
        # sv_handlers._build_sv_index now writes to out_dir (job scratch),
        # not the permanent report root.
        scratch_dir = tmp_path / "job-scratch" / "out"
        scratch_dir.mkdir(parents=True)
        scratch_db = scratch_dir / "sv.db"
        scratch_db.write_bytes(b"sqlite-bytes")

        vcf_out = _scratch_file(suffix=".vcf.gz")
        tbi_out = _scratch_file(suffix=".vcf.gz.tbi")

        await results._apply_call_structural_variants(
            {
                "bam_object_id": str(bam.id),
                "output": {"tmp_path": str(vcf_out), "name": "calls.sniffles.vcf.gz"},
                "index": {
                    "tmp_path": str(tbi_out),
                    "name": "calls.sniffles.vcf.gz.tbi",
                },
                "sv_db_path": str(scratch_db),
                "tool_version": "2.4",
                "params": {"min_sv_length": 50},
            },
            owner=OWNER,
        )

        produced = await DataObject.find(
            DataObject.derived_from == bam.id, DataObject.owner == OWNER
        ).to_list()
        assert [p.name for p in produced] == ["calls.sniffles.vcf.gz"]
        vcf = produced[0]

        dest = private_sv_stats_root / str(vcf.id) / "sv.db"
        assert dest.is_file(), "sv.db was not moved to the vcf-keyed directory"
        assert dest.read_bytes() == b"sqlite-bytes"

        # The scratch source no longer exists -- moved, not copied.
        assert not scratch_db.exists()

    async def test_a_move_failure_is_logged_not_raised(self, tmp_path, monkeypatch):
        """A permissions error or a vanished scratch file must not blow up
        the whole apply -- the VCF and its .tbi are the deliverable, and the
        SQLite table is regenerable from the VCF at any time."""
        bam = await _bam(OWNER)

        scratch_db = tmp_path / "does-not-exist" / "sv.db"  # never created
        vcf_out = _scratch_file(suffix=".vcf.gz")
        tbi_out = _scratch_file(suffix=".vcf.gz.tbi")

        errors: list[tuple] = []
        monkeypatch.setattr(
            results.log, "error", lambda event, **kw: errors.append((event, kw))
        )

        await results._apply_call_structural_variants(
            {
                "bam_object_id": str(bam.id),
                "output": {"tmp_path": str(vcf_out), "name": "calls.sniffles.vcf.gz"},
                "index": {
                    "tmp_path": str(tbi_out),
                    "name": "calls.sniffles.vcf.gz.tbi",
                },
                "sv_db_path": str(scratch_db),
            },
            owner=OWNER,
        )

        # The VCF still landed despite the missing db source.
        produced = await DataObject.find(
            DataObject.derived_from == bam.id, DataObject.owner == OWNER
        ).to_list()
        assert [p.name for p in produced] == ["calls.sniffles.vcf.gz"]

        assert any(event == "sv_db_move_failed" for event, _ in errors), (
            "a missing scratch source should log sv_db_move_failed, not raise"
        )

    async def test_no_sv_db_path_is_a_silent_no_op(self):
        """A result dict from before this key existed (or a handler that
        genuinely built nothing) must not error just because `sv_db_path` is
        absent."""
        bam = await _bam(OWNER)
        vcf_out = _scratch_file(suffix=".vcf.gz")

        await results._apply_call_structural_variants(
            {
                "bam_object_id": str(bam.id),
                "output": {"tmp_path": str(vcf_out), "name": "calls.sniffles.vcf.gz"},
            },
            owner=OWNER,
        )

        produced = await DataObject.find(
            DataObject.derived_from == bam.id, DataObject.owner == OWNER
        ).to_list()
        assert [p.name for p in produced] == ["calls.sniffles.vcf.gz"]


    async def test_snf_sidecar_is_ingested(self, tmp_path, monkeypatch):
        bam = await _bam(OWNER)
        vcf_out = _scratch_file(suffix=".vcf.gz")
        snf_out = _scratch_file(suffix=".snf")
        vcf_name = f"calls-{uuid.uuid4().hex}.vcf.gz"

        errors = []
        monkeypatch.setattr(
            results.log, "error", lambda event, **kw: errors.append((event, kw))
        )

        await results._apply_call_structural_variants(
            {
                "bam_object_id": str(bam.id),
                "output": {"tmp_path": str(vcf_out), "name": vcf_name},
                "snf": {"tmp_path": str(snf_out), "name": "calls.sniffles.snf"},
            },
            owner=OWNER,
        )

        assert errors == []
        vcf = await DataObject.find_one(DataObject.name == vcf_name)
        assert vcf is not None
        snf = await DataObject.find_one(
            DataObject.sidecar_of == vcf.id,
            DataObject.sidecar_role == SidecarRole.SNF,
        )
        assert snf is not None
        assert snf.name == "calls.sniffles.snf"


def test_sv_provenance_records_delly():
    """The VCF's own record of what made it. A literal caller here means
    every Delly callset claims to be Sniffles output. Requirement SV-620-6."""
    from app.queue.results import sv_provenance

    prov = sv_provenance({"caller": "delly", "tool_version": "2.6.0"})

    assert prov["variants_called_by"] == "delly"
    assert prov["variant_caller_version"] == "2.6.0"


def test_sv_provenance_records_sniffles():
    from app.queue.results import sv_provenance

    prov = sv_provenance({"caller": "sniffles2", "tool_version": "2.8.0"})

    assert prov["variants_called_by"] == "sniffles2"


def test_sv_provenance_falls_back_for_a_pre_620_result():
    """Jobs queued before #620 carry no caller field. They were all
    Sniffles, so that is the honest default -- but only for results with no
    caller at all, never as an override."""
    from app.queue.results import sv_provenance

    assert sv_provenance({})["variants_called_by"] == "sniffles2"


def test_sv_provenance_does_not_override_a_present_but_falsy_caller():
    """`or "sniffles2"` would silently override an explicit empty/None
    caller with the fallback. The fallback must apply only when the key is
    truly absent -- not whenever the value is falsy."""
    from app.queue.results import sv_provenance

    assert sv_provenance({"caller": ""})["variants_called_by"] == ""


class TestMergeSvApplier:
    async def test_apply_merge_structural_variants(
        self, tmp_path, private_sv_stats_root
    ):
        bam = await _bam(OWNER)
        vcf_out = _scratch_file(suffix=".vcf.gz")
        snf1 = await object_service.ingest_local_file(
            owner=OWNER,
            project_id=bam.project_id,
            path=_scratch_file(suffix=".snf"),
            name="sample1.snf",
            role=ObjectRole.VARIANTS,
            sidecar_role="snf",
        )
        snf2 = await object_service.ingest_local_file(
            owner=OWNER,
            project_id=bam.project_id,
            path=_scratch_file(suffix=".snf"),
            name="sample2.snf",
            role=ObjectRole.VARIANTS,
            sidecar_role="snf",
        )

        scratch_dir = tmp_path / "merge-scratch" / "out"
        scratch_dir.mkdir(parents=True)
        scratch_db = scratch_dir / "sv.db"
        scratch_db.write_bytes(b"joint-sqlite-bytes")

        vcf_out = _scratch_file(suffix=".vcf.gz")
        tbi_out = _scratch_file(suffix=".vcf.gz.tbi")

        await results._apply_merge_structural_variants(
            {
                "snf_object_ids": [str(snf1.id), str(snf2.id)],
                "output": {"tmp_path": str(vcf_out), "name": "joint_calls.sniffles.vcf.gz"},
                "index": {
                    "tmp_path": str(tbi_out),
                    "name": "joint_calls.sniffles.vcf.gz.tbi",
                },
                "sv_db_path": str(scratch_db),
            },
            owner=OWNER,
        )

        joint_vcf = await DataObject.find_one(DataObject.name == "joint_calls.sniffles.vcf.gz")
        assert joint_vcf is not None
        assert joint_vcf.role == ObjectRole.VARIANTS

        dest = private_sv_stats_root / str(joint_vcf.id) / "sv.db"
        assert dest.is_file()
        assert dest.read_bytes() == b"joint-sqlite-bytes"

