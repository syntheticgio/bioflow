"""`launch_continuity_qc`'s chemistry routing and payload contract.

Modeled directly on `test_assembly_error_qc_launch.py`, GCI's closest
sibling. GCI's routing is stricter than CRAQ's: it has exactly two
long-read slots (`--hifi`/`--nano`) instead of one, and PacBio CLR --
long-read, and so superficially eligible -- must be refused outright rather
than folded into either slot (`pipeline_service.gci_slot_for_chemistry`).
This file locks that refusal at the launch-path level, not just in the
pure-function unit tests in `test_pipeline_service.py`.

Since winnowmap: each slot carries a *list* of BAMs, not one, because GCI's
own `--hifi`/`--nano` are `nargs='+'` and two aligners (minimap2 + winnowmap)
against the same reads is the routine case, not the ambiguous one. The
ambiguity refusal now fires only when a single aligner contributed more than
one BAM to a slot -- see `TestAutoPairChemistryRouting`'s
`test_refuses_ambiguous_same_aligner_candidates` and the accompanying
`test_two_different_aligners_are_not_ambiguous`.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.errors import ValidationError
from app.models import FormatKind, ObjectStatus, SidecarRole
from app.pipelines.align_runner import ReadChemistry
from app.pipelines.tools import Tool
from app.services import pipeline_service
from beanie import PydanticObjectId

_GCI = Tool(name="gci", path="/usr/local/bin/GCI.py", version="1.2.0")


def _obj(*, name, kind=FormatKind.BAM, status=ObjectStatus.READY, project_id=None,
         derived_from=None, owner="local", role=None, facts=None):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name=name,
        format=SimpleNamespace(kind=kind),
        status=status,
        facts=facts or {},
        project_id=project_id or PydanticObjectId(),
        owner=owner,
        derived_from=derived_from or [],
        blob_sha256="a" * 64,
        role=role,
    )


def _assembly(**kwargs):
    return _obj(name="draft.fasta", kind=FormatKind.FASTA, **kwargs)


def _bam(assembly, *, name="reads.bam", aligned_by="minimap2"):
    facts = {"aligned_by": aligned_by} if aligned_by is not None else {}
    return _obj(
        name=name,
        kind=FormatKind.BAM,
        derived_from=[assembly.id],
        project_id=assembly.project_id,
        facts=facts,
    )


def _bai_sidecar(bam):
    obj = _obj(name=f"{bam.name}.bai", kind=FormatKind.UNKNOWN, project_id=bam.project_id)
    obj.sidecar_role = SidecarRole.BAI
    return obj


async def _run(
    *,
    assembly,
    hifi_bams=(),
    nano_bams=(),
    hifi_bam_ids=None,
    nano_bam_ids=None,
    sidecars_by_bam=(),
    alignments=(([], [], [])),
    chemistry_by_bam=None,
    map_qual=None,
    plot=None,
):
    all_bams = [*hifi_bams, *nano_bams]
    objects = {o.id: o for o in [assembly, *all_bams]}
    chemistry_by_bam = chemistry_by_bam or {}

    async def _get_object(object_id, *, owner):
        return objects[object_id]

    async def _list_sidecars(object_id, *, owner):
        for bam, sidecars in sidecars_by_bam:
            if bam.id == object_id:
                return sidecars
        return []

    async def _chemistry_for(obj):
        if obj is None:
            return None
        return chemistry_by_bam.get(obj.id)

    async def _alignments_against(assembly_obj, *, owner):
        return alignments

    enqueued = {}

    async def _enqueue(job_type, **kwargs):
        enqueued["type"] = job_type
        enqueued.update(kwargs)
        return SimpleNamespace(id="job1")

    with (
        patch("app.pipelines.tools.gci", return_value=_GCI),
        patch("app.services.object_service.get_object", AsyncMock(side_effect=_get_object)),
        patch(
            "app.services.object_service.list_sidecars",
            AsyncMock(side_effect=_list_sidecars),
        ),
        patch(
            "app.services.pipeline_service._resolve_readable",
            AsyncMock(return_value=("a" * 64, None)),
        ),
        patch(
            "app.services.pipeline_service.read_chemistry_for_alignment",
            AsyncMock(side_effect=_chemistry_for),
        ),
        patch(
            "app.services.pipeline_service.alignments_against",
            AsyncMock(side_effect=_alignments_against),
        ),
        patch("app.queue.queue.enqueue", _enqueue),
    ):
        job = await pipeline_service.launch_continuity_qc(
            object_id=assembly.id,
            owner="local",
            hifi_bam_ids=hifi_bam_ids
            if hifi_bam_ids is not None
            else ([b.id for b in hifi_bams] if hifi_bams else None),
            nano_bam_ids=nano_bam_ids
            if nano_bam_ids is not None
            else ([b.id for b in nano_bams] if nano_bams else None),
            map_qual=map_qual,
            plot=plot,
        )
    return job, enqueued


def _entry_object_ids(payload, slot):
    return {e["object_id"] for e in payload.get(slot, [])}


class TestAutoPairChemistryRouting:
    async def test_auto_pairs_single_hifi_bam(self):
        assembly = _assembly()
        hifi_bam = _bam(assembly, name="hifi.bam")

        job, enqueued = await _run(
            assembly=assembly,
            alignments=([], [hifi_bam], []),
            chemistry_by_bam={hifi_bam.id: ReadChemistry.HIFI},
        )

        payload = enqueued["payload"]
        assert enqueued["type"] == "assess_assembly_continuity"
        assert _entry_object_ids(payload, "hifi_bams") == {str(hifi_bam.id)}
        assert payload.get("nano_bams") == []

    async def test_auto_pairs_single_ont_bam_into_nano_slot(self):
        assembly = _assembly()
        ont_bam = _bam(assembly, name="ont.bam")

        job, enqueued = await _run(
            assembly=assembly,
            alignments=([], [ont_bam], []),
            chemistry_by_bam={ont_bam.id: ReadChemistry.ONT_SIMPLEX},
        )

        payload = enqueued["payload"]
        assert _entry_object_ids(payload, "nano_bams") == {str(ont_bam.id)}
        assert payload.get("hifi_bams") == []

    async def test_auto_pairs_one_hifi_and_one_nano(self):
        assembly = _assembly()
        hifi_bam = _bam(assembly, name="hifi.bam")
        ont_bam = _bam(assembly, name="ont.bam")

        job, enqueued = await _run(
            assembly=assembly,
            alignments=([], [hifi_bam, ont_bam], []),
            chemistry_by_bam={
                hifi_bam.id: ReadChemistry.HIFI,
                ont_bam.id: ReadChemistry.ONT_DUPLEX,
            },
        )

        payload = enqueued["payload"]
        assert _entry_object_ids(payload, "hifi_bams") == {str(hifi_bam.id)}
        assert _entry_object_ids(payload, "nano_bams") == {str(ont_bam.id)}

    async def test_refuses_clr_only_long_reads(self):
        """A CLR BAM is long-read and so appears in `alignments_against`'s
        `long_` bucket, but must never become a GCI candidate -- it is
        dropped by `_gci_candidates`, leaving zero usable candidates, which
        must raise rather than silently proceeding with no long-read
        input."""
        assembly = _assembly()
        clr_bam = _bam(assembly, name="clr.bam")

        with pytest.raises(ValidationError, match="CLR"):
            await _run(
                assembly=assembly,
                alignments=([], [clr_bam], []),
                chemistry_by_bam={clr_bam.id: ReadChemistry.CLR},
            )

    async def test_refuses_short_read_only(self):
        assembly = _assembly()
        short_bam = _bam(assembly, name="short.bam")

        with pytest.raises(ValidationError, match="short-read"):
            await _run(
                assembly=assembly,
                alignments=([short_bam], [], []),
            )

    async def test_refuses_no_alignments_at_all(self):
        assembly = _assembly()

        with pytest.raises(ValidationError):
            await _run(assembly=assembly, alignments=([], [], []))

    async def test_refuses_ambiguous_same_aligner_candidates(self):
        """Two HiFi BAMs from the *same* aligner is still ambiguous -- there
        is no way to tell which one is meant, unlike two BAMs from two
        different aligners cross-checking the same reads."""
        assembly = _assembly()
        hifi_a = _bam(assembly, name="hifi_a.bam", aligned_by="minimap2")
        hifi_b = _bam(assembly, name="hifi_b.bam", aligned_by="minimap2")

        with pytest.raises(ValidationError, match="same aligner"):
            await _run(
                assembly=assembly,
                alignments=([], [hifi_a, hifi_b], []),
                chemistry_by_bam={
                    hifi_a.id: ReadChemistry.HIFI,
                    hifi_b.id: ReadChemistry.HIFI,
                },
            )

    async def test_two_different_aligners_are_not_ambiguous(self):
        """The routine winnowmap case: two HiFi BAMs against the same
        assembly, one from each aligner, both pass straight through to the
        payload rather than tripping the ambiguity refusal."""
        assembly = _assembly()
        mm2_bam = _bam(assembly, name="mm2.bam", aligned_by="minimap2")
        wm2_bam = _bam(assembly, name="wm2.bam", aligned_by="winnowmap")

        job, enqueued = await _run(
            assembly=assembly,
            alignments=([], [mm2_bam, wm2_bam], []),
            chemistry_by_bam={
                mm2_bam.id: ReadChemistry.HIFI,
                wm2_bam.id: ReadChemistry.HIFI,
            },
        )

        payload = enqueued["payload"]
        assert _entry_object_ids(payload, "hifi_bams") == {
            str(mm2_bam.id),
            str(wm2_bam.id),
        }

    async def test_three_aligners_one_duplicated_is_still_ambiguous(self):
        """Grouping is per-aligner: a duplicate within one aligner's group
        must still raise even when a different aligner's single BAM is
        present alongside it."""
        assembly = _assembly()
        mm2_a = _bam(assembly, name="mm2_a.bam", aligned_by="minimap2")
        mm2_b = _bam(assembly, name="mm2_b.bam", aligned_by="minimap2")
        wm2 = _bam(assembly, name="wm2.bam", aligned_by="winnowmap")

        with pytest.raises(ValidationError, match="same aligner"):
            await _run(
                assembly=assembly,
                alignments=([], [mm2_a, mm2_b, wm2], []),
                chemistry_by_bam={
                    mm2_a.id: ReadChemistry.HIFI,
                    mm2_b.id: ReadChemistry.HIFI,
                    wm2.id: ReadChemistry.HIFI,
                },
            )


class TestExplicitIdChemistryRouting:
    async def test_explicit_hifi_bam_id_accepted_when_chemistry_matches(self):
        assembly = _assembly()
        hifi_bam = _bam(assembly, name="hifi.bam")

        job, enqueued = await _run(
            assembly=assembly,
            hifi_bams=[hifi_bam],
            chemistry_by_bam={hifi_bam.id: ReadChemistry.HIFI},
        )

        assert _entry_object_ids(enqueued["payload"], "hifi_bams") == {str(hifi_bam.id)}

    async def test_explicit_hifi_bam_ids_accepted_for_two_aligners(self):
        assembly = _assembly()
        mm2_bam = _bam(assembly, name="mm2.bam", aligned_by="minimap2")
        wm2_bam = _bam(assembly, name="wm2.bam", aligned_by="winnowmap")

        job, enqueued = await _run(
            assembly=assembly,
            hifi_bams=[mm2_bam, wm2_bam],
            chemistry_by_bam={
                mm2_bam.id: ReadChemistry.HIFI,
                wm2_bam.id: ReadChemistry.HIFI,
            },
        )

        payload = enqueued["payload"]
        assert _entry_object_ids(payload, "hifi_bams") == {
            str(mm2_bam.id),
            str(wm2_bam.id),
        }
        aligned_by = {e["aligned_by"] for e in payload["hifi_bams"]}
        assert aligned_by == {"minimap2", "winnowmap"}

    async def test_explicit_hifi_bam_id_refused_when_bam_is_clr(self):
        """The dialog client could pass any BAM id under `hifi_bam_ids`
        regardless of its actual chemistry -- the explicit-id path must not
        be a bypass for the CLR refusal the auto-pair path enforces."""
        assembly = _assembly()
        clr_bam = _bam(assembly, name="clr.bam")

        with pytest.raises(ValidationError, match="not a HIFI alignment"):
            await _run(
                assembly=assembly,
                hifi_bams=[clr_bam],
                chemistry_by_bam={clr_bam.id: ReadChemistry.CLR},
            )

    async def test_explicit_nano_bam_id_refused_when_bam_is_hifi(self):
        assembly = _assembly()
        hifi_bam = _bam(assembly, name="hifi.bam")

        with pytest.raises(ValidationError, match="not a NANO alignment"):
            await _run(
                assembly=assembly,
                nano_bams=[hifi_bam],
                chemistry_by_bam={hifi_bam.id: ReadChemistry.HIFI},
            )

    async def test_explicit_bam_not_aligned_to_this_assembly_refused(self):
        assembly = _assembly()
        other_assembly = _assembly()
        stray_bam = _bam(other_assembly, name="stray.bam")

        with pytest.raises(ValidationError, match="was not aligned"):
            await _run(
                assembly=assembly,
                hifi_bams=[stray_bam],
                chemistry_by_bam={stray_bam.id: ReadChemistry.HIFI},
            )


class TestBaiPayloadKeysMatchWhatTheHandlerReads:
    async def test_hifi_bam_bai_keys_are_exactly_bai_prefixed(self):
        assembly = _assembly()
        hifi_bam = _bam(assembly, name="hifi.bam")
        bai = _bai_sidecar(hifi_bam)

        job, enqueued = await _run(
            assembly=assembly,
            hifi_bams=[hifi_bam],
            chemistry_by_bam={hifi_bam.id: ReadChemistry.HIFI},
            sidecars_by_bam=[(hifi_bam, [bai])],
        )

        entry = enqueued["payload"]["hifi_bams"][0]
        assert entry["bai_sha256"] == "a" * 64
        assert entry["bam_sha256"] == "a" * 64
        assert entry["object_id"] == str(hifi_bam.id)

    async def test_nano_bam_bai_keys_are_exactly_bai_prefixed(self):
        assembly = _assembly()
        nano_bam = _bam(assembly, name="nano.bam")
        bai = _bai_sidecar(nano_bam)

        job, enqueued = await _run(
            assembly=assembly,
            nano_bams=[nano_bam],
            chemistry_by_bam={nano_bam.id: ReadChemistry.ONT_SIMPLEX},
            sidecars_by_bam=[(nano_bam, [bai])],
        )

        entry = enqueued["payload"]["nano_bams"][0]
        assert entry["bai_sha256"] == "a" * 64
        assert entry["bam_sha256"] == "a" * 64


class TestPlotGating:
    async def test_plot_allowed_under_contig_limit(self):
        assembly = _assembly(facts={"sequence_count": 10})
        hifi_bam = _bam(assembly, name="hifi.bam")

        job, enqueued = await _run(
            assembly=assembly,
            hifi_bams=[hifi_bam],
            chemistry_by_bam={hifi_bam.id: ReadChemistry.HIFI},
            plot=True,
        )

        assert enqueued["payload"]["plot"] is True

    async def test_plot_denied_over_contig_limit(self):
        assembly = _assembly(facts={"sequence_count": 500})
        hifi_bam = _bam(assembly, name="hifi.bam")

        job, enqueued = await _run(
            assembly=assembly,
            hifi_bams=[hifi_bam],
            chemistry_by_bam={hifi_bam.id: ReadChemistry.HIFI},
            plot=True,
        )

        assert enqueued["payload"]["plot"] is False

    async def test_plot_denied_when_contig_count_unknown(self):
        assembly = _assembly(facts={})
        hifi_bam = _bam(assembly, name="hifi.bam")

        job, enqueued = await _run(
            assembly=assembly,
            hifi_bams=[hifi_bam],
            chemistry_by_bam={hifi_bam.id: ReadChemistry.HIFI},
            plot=True,
        )

        assert enqueued["payload"]["plot"] is False

    async def test_plot_not_requested_stays_false(self):
        assembly = _assembly(facts={"sequence_count": 10})
        hifi_bam = _bam(assembly, name="hifi.bam")

        job, enqueued = await _run(
            assembly=assembly,
            hifi_bams=[hifi_bam],
            chemistry_by_bam={hifi_bam.id: ReadChemistry.HIFI},
            plot=False,
        )

        assert enqueued["payload"]["plot"] is False


class TestMapQualDefault:
    async def test_map_qual_defaults_to_30(self):
        assembly = _assembly()
        hifi_bam = _bam(assembly, name="hifi.bam")

        job, enqueued = await _run(
            assembly=assembly,
            hifi_bams=[hifi_bam],
            chemistry_by_bam={hifi_bam.id: ReadChemistry.HIFI},
        )

        assert enqueued["payload"]["map_qual"] == 30

    async def test_map_qual_explicit_value_used(self):
        assembly = _assembly()
        hifi_bam = _bam(assembly, name="hifi.bam")

        job, enqueued = await _run(
            assembly=assembly,
            hifi_bams=[hifi_bam],
            chemistry_by_bam={hifi_bam.id: ReadChemistry.HIFI},
            map_qual=20,
        )

        assert enqueued["payload"]["map_qual"] == 20
