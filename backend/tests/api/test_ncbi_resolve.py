"""The unified resolve endpoint's dispatch.

What matters is that one accession box routes to the right resolver and says
which branch it took, so the dialog can render a run table or an assembly card
without guessing from the shape of the response.
"""

import pytest

from app.metadata import assembly, assembly_components, sra_resolver

# `client` and `two_profiles` come from tests/api/conftest.py. This module used
# to build its own header-less client, which stopped working when
# /ncbi/resolve grew an `OwnerDep`: the route now needs a real profile to
# resolve, so the shared fixtures supply one -- and `beanie_models` comes along
# with them, since creating that profile is a database write.
pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


class TestResolveDispatch:
    async def test_a_run_accession_returns_the_sra_branch(
        self, client, two_profiles, monkeypatch
    ):
        monkeypatch.setattr(
            sra_resolver,
            "resolve_cached",
            _async(sra_resolver.SraResolution(accession="SRR1", kind="run")),
        )
        r = await client.post(
            "/api/v1/ncbi/resolve",
            json={"accession": "SRR1"},
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "run"
        assert body["sra"] is not None
        assert body["assembly"] is None

    async def test_an_assembly_accession_returns_the_assembly_branch(
        self, client, two_profiles, monkeypatch
    ):
        monkeypatch.setattr(
            assembly,
            "lookup",
            lambda a: assembly.AssemblyMetadata(
                accession="GCF_000002445.2", organism="Trypanosoma brucei"
            ),
        )
        monkeypatch.setattr(
            assembly,
            "component_availability",
            lambda a: list(assembly_components.from_report(
                {"annotation_info": {"name": "x"}}
            ).values()),
        )
        r = await client.post(
            "/api/v1/ncbi/resolve",
            json={"accession": "GCF_000002445.2"},
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "assembly"
        assert body["assembly"] is not None
        assert body["sra"] is None
        assert body["assembly"]["organism"] == "Trypanosoma brucei"
        assert len(body["assembly"]["components"]) == 4

    async def test_an_unknown_assembly_is_a_200_with_an_error(
        self, client, two_profiles, monkeypatch
    ):
        """A resolution that finds nothing is a result the dialog renders, not
        a failed request -- the same rule the SRA endpoint follows."""
        monkeypatch.setattr(assembly, "lookup", lambda a: None)
        monkeypatch.setattr(assembly, "component_availability", lambda a: None)
        r = await client.post(
            "/api/v1/ncbi/resolve",
            json={"accession": "GCF_999999999.1"},
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        assert r.json()["assembly"]["error"]

    async def test_gibberish_is_a_200_with_an_error(self, client, two_profiles):
        r = await client.post(
            "/api/v1/ncbi/resolve",
            json={"accession": "hello"},
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        body = r.json()
        assert body["sra"] is not None
        assert body["sra"]["error"]


class TestOldSraPathStillWorks:
    """The `/sra/*` paths moved their implementation to `ncbi.py`, but must
    keep working unchanged -- nothing currently using them should break."""

    async def test_the_old_sra_resolve_path_still_works(self, client, two_profiles, monkeypatch):
        monkeypatch.setattr(
            sra_resolver,
            "resolve_cached",
            _async(sra_resolver.SraResolution(accession="SRR1", kind="run")),
        )
        r = await client.post(
            "/api/v1/sra/resolve",
            json={"accession": "SRR1"},
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "run"
        assert body["accession"] == "SRR1"


def _async(value):
    async def fake(*args, **kwargs):
        return value
    return fake
