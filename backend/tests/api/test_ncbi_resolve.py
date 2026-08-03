"""The unified resolve endpoint's dispatch.

What matters is that one accession box routes to the right resolver and says
which branch it took, so the dialog can render a run table or an assembly card
without guessing from the shape of the response.
"""

import pytest

from app.metadata import ncbi_assembly, ncbi_assembly_components, ncbi_taxonomy, sra_resolver

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
            ncbi_assembly,
            "lookup",
            lambda a: ncbi_assembly.AssemblyMetadata(
                accession="GCF_000002445.2", organism="Trypanosoma brucei"
            ),
        )
        monkeypatch.setattr(
            ncbi_assembly,
            "component_availability",
            lambda a: list(ncbi_assembly_components.from_report(
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
        monkeypatch.setattr(ncbi_assembly, "lookup", lambda a: None)
        monkeypatch.setattr(ncbi_assembly, "component_availability", lambda a: None)
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


class TestOrganismSuggest:
    async def test_returns_suggestions_from_taxon_suggest(
        self, client, two_profiles, monkeypatch
    ):
        monkeypatch.setattr(
            ncbi_taxonomy,
            "suggest_organisms",
            lambda q: [
                ncbi_taxonomy.TaxonSuggestion(
                    sci_name="Homo sapiens", tax_id=9606, common_name="human"
                )
            ],
        )
        r = await client.get(
            "/api/v1/ncbi/organism-suggest",
            params={"q": "hom"},
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["suggestions"]) == 1
        assert body["suggestions"][0]["sci_name"] == "Homo sapiens"
        assert body["suggestions"][0]["tax_id"] == 9606

    async def test_no_matches_is_a_200_with_an_empty_list(
        self, client, two_profiles, monkeypatch
    ):
        monkeypatch.setattr(ncbi_taxonomy, "suggest_organisms", lambda q: [])
        r = await client.get(
            "/api/v1/ncbi/organism-suggest",
            params={"q": "zzz"},
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        assert r.json()["suggestions"] == []


class TestOrganismSearch:
    async def test_returns_assemblies_and_sra_runs(
        self, client, two_profiles, monkeypatch
    ):
        monkeypatch.setattr(
            ncbi_taxonomy,
            "search_assemblies_by_taxon",
            lambda tax_id, **k: ncbi_taxonomy.AssemblyPage(
                assemblies=[
                    ncbi_assembly.AssemblyMetadata(
                        accession="GCF_000002445.2",
                        organism="Trypanosoma brucei",
                        refseq_category="reference genome",
                    )
                ],
                next_page_token="next-token",
                total_count=1,
            ),
        )
        monkeypatch.setattr(
            sra_resolver, "search_runs_by_organism", lambda o, **k: (["1"], 1)
        )
        monkeypatch.setattr(
            sra_resolver,
            "fetch_packages",
            lambda uids: ["<fake-package>"],
        )
        monkeypatch.setattr(
            sra_resolver,
            "runs_from_package",
            lambda package: [sra_resolver.RunInfo(accession="SRR1")],
        )

        r = await client.post(
            "/api/v1/ncbi/organism-search",
            json={"tax_id": 5691, "sci_name": "Trypanosoma brucei"},
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["assemblies"]) == 1
        assert body["assemblies"][0]["accession"] == "GCF_000002445.2"
        assert body["assemblies"][0]["refseq_category"] == "reference genome"
        assert body["assemblies_next_page_token"] == "next-token"
        assert len(body["sra_runs"]) == 1
        assert body["sra_runs"][0]["accession"] == "SRR1"
        assert body["sra_total_count"] == 1
        assert body["sra_next_offset"] is None
        assert body["error"] is None

    async def test_next_offset_is_set_when_more_runs_remain(
        self, client, two_profiles, monkeypatch
    ):
        monkeypatch.setattr(
            ncbi_taxonomy,
            "search_assemblies_by_taxon",
            lambda tax_id, **k: ncbi_taxonomy.AssemblyPage(),
        )
        monkeypatch.setattr(
            sra_resolver, "search_runs_by_organism", lambda o, **k: (["1"], 500)
        )
        monkeypatch.setattr(sra_resolver, "fetch_packages", lambda uids: [])

        r = await client.post(
            "/api/v1/ncbi/organism-search",
            json={"tax_id": 9606, "sci_name": "Homo sapiens", "sra_offset": 0, "page_size": 1},
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        assert r.json()["sra_next_offset"] == 1

    async def test_nothing_found_is_a_200_with_an_error(
        self, client, two_profiles, monkeypatch
    ):
        monkeypatch.setattr(
            ncbi_taxonomy,
            "search_assemblies_by_taxon",
            lambda tax_id, **k: ncbi_taxonomy.AssemblyPage(),
        )
        monkeypatch.setattr(
            sra_resolver, "search_runs_by_organism", lambda o, **k: ([], 0)
        )
        r = await client.post(
            "/api/v1/ncbi/organism-search",
            json={"tax_id": 999999999, "sci_name": "Nonexistentia fakeus"},
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        body = r.json()
        assert body["assemblies"] == []
        assert body["sra_runs"] == []
        assert body["error"]

    async def test_initial_search_caps_each_section_to_five(
        self, client, two_profiles, monkeypatch
    ):
        """`section` defaults to "both" -- the initial side-by-side search --
        which must cap each list rather than fetch a full `page_size` page of
        each, however large `page_size` is set."""
        seen_page_sizes: dict[str, int] = {}

        def fake_assemblies(tax_id, *, page_size, **k):
            seen_page_sizes["assemblies"] = page_size
            return ncbi_taxonomy.AssemblyPage()

        def fake_sra(o, *, retmax, **k):
            seen_page_sizes["sra"] = retmax
            return ([], 0)

        monkeypatch.setattr(ncbi_taxonomy, "search_assemblies_by_taxon", fake_assemblies)
        monkeypatch.setattr(sra_resolver, "search_runs_by_organism", fake_sra)

        r = await client.post(
            "/api/v1/ncbi/organism-search",
            json={"tax_id": 9606, "sci_name": "Homo sapiens", "page_size": 20},
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        assert seen_page_sizes == {"assemblies": 5, "sra": 5}

    async def test_narrowing_to_assemblies_section_skips_the_sra_fetch(
        self, client, two_profiles, monkeypatch
    ):
        called = {"sra": False}

        def fake_assemblies(tax_id, *, page_size, **k):
            assert page_size == 20
            return ncbi_taxonomy.AssemblyPage(
                assemblies=[
                    ncbi_assembly.AssemblyMetadata(accession="GCF_000002445.2")
                ]
            )

        def fake_sra(o, **k):
            called["sra"] = True
            return ([], 0)

        monkeypatch.setattr(ncbi_taxonomy, "search_assemblies_by_taxon", fake_assemblies)
        monkeypatch.setattr(sra_resolver, "search_runs_by_organism", fake_sra)

        r = await client.post(
            "/api/v1/ncbi/organism-search",
            json={
                "tax_id": 9606,
                "sci_name": "Homo sapiens",
                "page_size": 20,
                "section": "assemblies",
            },
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["assemblies"]) == 1
        assert body["sra_runs"] == []
        assert body["sra_total_count"] == 0
        assert called["sra"] is False

    async def test_narrowing_to_sra_section_skips_the_assembly_fetch(
        self, client, two_profiles, monkeypatch
    ):
        called = {"assemblies": False}

        def fake_assemblies(tax_id, **k):
            called["assemblies"] = True
            return ncbi_taxonomy.AssemblyPage()

        def fake_sra(o, *, retmax, **k):
            assert retmax == 20
            return (["1"], 1)

        monkeypatch.setattr(ncbi_taxonomy, "search_assemblies_by_taxon", fake_assemblies)
        monkeypatch.setattr(sra_resolver, "search_runs_by_organism", fake_sra)
        monkeypatch.setattr(sra_resolver, "fetch_packages", lambda uids: ["<pkg>"])
        monkeypatch.setattr(
            sra_resolver,
            "runs_from_package",
            lambda package: [sra_resolver.RunInfo(accession="SRR1")],
        )

        r = await client.post(
            "/api/v1/ncbi/organism-search",
            json={
                "tax_id": 9606,
                "sci_name": "Homo sapiens",
                "page_size": 20,
                "section": "sra",
            },
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        body = r.json()
        assert body["assemblies"] == []
        assert body["assemblies_next_page_token"] is None
        assert len(body["sra_runs"]) == 1
        assert body["sra_total_count"] == 1
        assert called["assemblies"] is False

    async def test_platform_filter_is_passed_to_the_sra_organism_search(
        self, client, two_profiles, monkeypatch
    ):
        seen = {}

        def fake_sra(o, *, platform_filter, **k):
            seen["platform_filter"] = platform_filter
            return ([], 0)

        monkeypatch.setattr(
            ncbi_taxonomy,
            "search_assemblies_by_taxon",
            lambda tax_id, **k: ncbi_taxonomy.AssemblyPage(),
        )
        monkeypatch.setattr(sra_resolver, "search_runs_by_organism", fake_sra)

        r = await client.post(
            "/api/v1/ncbi/organism-search",
            json={
                "tax_id": 4932,
                "sci_name": "Saccharomyces cerevisiae",
                "platform_filter": "OXFORD_NANOPORE",
            },
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        assert seen["platform_filter"] == "OXFORD_NANOPORE"

    async def test_assembly_level_filter_is_passed_to_the_taxon_search(
        self, client, two_profiles, monkeypatch
    ):
        seen = {}

        def fake_assemblies(tax_id, *, assembly_level, **k):
            seen["assembly_level"] = assembly_level
            return ncbi_taxonomy.AssemblyPage()

        monkeypatch.setattr(ncbi_taxonomy, "search_assemblies_by_taxon", fake_assemblies)
        monkeypatch.setattr(
            sra_resolver, "search_runs_by_organism", lambda o, **k: ([], 0)
        )

        r = await client.post(
            "/api/v1/ncbi/organism-search",
            json={
                "tax_id": 4932,
                "sci_name": "Saccharomyces cerevisiae",
                "assembly_level": "complete_genome",
            },
            headers=two_profiles["a_headers"],
        )
        assert r.status_code == 200
        assert seen["assembly_level"] == "complete_genome"


def _async(value):
    async def fake(*args, **kwargs):
        return value
    return fake
