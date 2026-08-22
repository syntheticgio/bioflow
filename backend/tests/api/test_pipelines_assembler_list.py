"""The assembler picker's listing endpoint.

Scoped to one object, unlike `/assemblers/{assembler}/schema` next to it,
because which assemblers a user may pick depends on the reads: a paired-layout
assembler cannot take long reads. That is the whole reason this could not be a
static route like the schema one.
"""

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from beanie import PydanticObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.pipelines import router
from app.errors import register_exception_handlers
from app.models import FormatKind, ObjectStatus
from app.pipelines import assembler_registry
from app.pipelines.assemblers import Assembler
from tests.api.bare_app import override_owner


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    # No profiles collection behind this app, so `OwnerDep` cannot resolve on
    # its own -- overridden the supported way rather than by passing a header.
    override_owner(app)
    return TestClient(app)


def _reads(chemistry):
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="SRR1_1.fastq",
        format=SimpleNamespace(kind=FormatKind.FASTQ),
        role=None,
        metadata={},
        facts={"qc_read_chemistry": chemistry},
        status=ObjectStatus.READY,
        project_id=PydanticObjectId(),
        owner="local",
    )


def _all_installed():
    """Every installable assembler probing as present.

    Otherwise this asserts against whichever tools the host image ships, and
    a "MEGAHIT is listed" assertion would pass or fail on the container rather
    than on the endpoint. Patched through `SPECS` because each spec captured
    its probe function at import (see `spec_for`).
    """
    return patch.dict(
        assembler_registry.SPECS,
        {
            assembler: dataclasses.replace(
                spec, tool=lambda: SimpleNamespace(available=True)
            )
            for assembler, spec in assembler_registry.SPECS.items()
            if spec.tool is not None
        },
    )


def _get(client, obj):
    with (
        _all_installed(),
        patch(
            "app.services.object_service.get_object", AsyncMock(return_value=obj)
        ),
    ):
        return client.get(f"/pipelines/assemblers?object_id={obj.id}")


class TestAssemblerListing:
    def test_short_reads_can_pick_the_paired_assemblers(self, client):
        """#785's point: SPAdes and MEGAHIT were installed, correct, and
        reachable only by an API caller passing `assembler:` by hand.
        """
        resp = _get(client, _reads("short"))
        assert resp.status_code == 200
        rows = {r["assembler"]: r for r in resp.json()["assemblers"]}
        for name in ("abyss", "spades", "megahit"):
            assert rows[name]["compatible"] is True

    def test_an_incompatible_assembler_is_listed_with_its_reason(self, client):
        """Greyed out rather than hidden -- a user who wonders where Flye went
        is better served by seeing why it cannot take these reads.
        """
        resp = _get(client, _reads("short"))
        flye = {r["assembler"]: r for r in resp.json()["assemblers"]}["flye"]
        assert flye["compatible"] is False
        assert flye["incompatible_reason"]

    def test_long_reads_invert_the_partition(self, client):
        resp = _get(client, _reads("hifi"))
        rows = {r["assembler"]: r for r in resp.json()["assemblers"]}
        assert rows["flye"]["compatible"] is True
        assert rows["megahit"]["compatible"] is False

    def test_the_default_is_marked_so_the_dialog_opens_on_it(self, client):
        resp = _get(client, _reads("short"))
        defaults = [r for r in resp.json()["assemblers"] if r["is_default"]]
        assert [r["assembler"] for r in defaults] == ["abyss"]

    def test_an_uninstalled_assembler_is_absent(self, client):
        """hifiasm has no probe to patch on -- `tool` is None -- so it stays
        absent even under `_all_installed`. Listing it would advertise a tool
        no selection could run.
        """
        resp = _get(client, _reads("short"))
        names = {r["assembler"] for r in resp.json()["assemblers"]}
        assert "hifiasm" not in names

    def test_unknown_chemistry_offers_nothing(self, client):
        """`launch_assembly` refuses these reads outright with "run QC first",
        so a picker offering choices here would contradict it.
        """
        resp = _get(client, _reads("unknown"))
        assert resp.status_code == 200
        assert resp.json()["assemblers"] == []

    def test_a_probe_going_off_removes_a_row(self, client):
        """The negative direction. The image ships most tools, so every
        assertion above passes whether or not `_all_installed` worked; this
        one fails if the patch is silently a no-op.
        """
        obj = _reads("short")
        megahit_off = dataclasses.replace(
            assembler_registry.SPECS[Assembler.MEGAHIT],
            tool=lambda: SimpleNamespace(available=False),
        )
        with (
            _all_installed(),
            patch.dict(
                assembler_registry.SPECS, {Assembler.MEGAHIT: megahit_off}
            ),
            patch(
                "app.services.object_service.get_object", AsyncMock(return_value=obj)
            ),
        ):
            resp = client.get(f"/pipelines/assemblers?object_id={obj.id}")

        names = {r["assembler"] for r in resp.json()["assemblers"]}
        assert "megahit" not in names
        assert "abyss" in names
