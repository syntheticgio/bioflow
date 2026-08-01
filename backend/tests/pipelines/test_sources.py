"""The external data source catalog behind the Sources help page."""

from app.pipelines import sources


class TestSourceCatalog:
    def test_lists_the_sources_the_app_actually_uses(self):
        names = {s.name for s in sources.DATA_SOURCES}
        assert "NCBI Datasets" in names
        assert "NCBI E-utilities" in names
        assert "NCBI Sequence Read Archive" in names
        # Two features call it: the proteome download and the variants
        # table's structure lookup. A source the app calls but does not list
        # is a reference page that quietly omits where the data came from.
        assert "UniProt" in names

    def test_every_source_is_documented(self):
        """Same forcing function as the tool catalog: a source added without
        a description fails here rather than rendering blank."""
        required = ("name", "kind", "summary", "usage", "homepage")
        missing = {
            s.name: [f for f in required if not getattr(s, f)]
            for s in sources.DATA_SOURCES
        }
        missing = {k: v for k, v in missing.items() if v}
        assert not missing, f"undocumented sources: {missing}"

    def test_urls_are_urls(self):
        for s in sources.DATA_SOURCES:
            for field in ("homepage", "docs", "citation_url", "terms"):
                value = getattr(s, field)
                if value:
                    assert value.startswith("https://"), (
                        f"{s.name}.{field} is not a URL: {value!r}"
                    )

    def test_kind_is_from_the_known_set(self):
        for s in sources.DATA_SOURCES:
            assert s.kind in set(sources.SOURCE_KINDS)

    def test_all_sources_serializes_for_the_api(self):
        payload = sources.all_sources()
        assert isinstance(payload, list)
        assert all(isinstance(item, dict) for item in payload)
        assert {"name", "kind", "summary", "usage", "homepage"} <= set(payload[0])

    def test_no_source_claims_a_version(self):
        """Sources have no version -- NCBI Datasets is whatever the API
        returned today. Showing one would be a fabrication."""
        assert all("version" not in item for item in sources.all_sources())
