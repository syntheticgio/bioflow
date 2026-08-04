"""Which metadata values are worth asking a model about, and how they key.

The organism field is free text that real records fill with placeholders, bare
tax IDs and whole pasted descriptions. Every one of those that gets through
becomes a permanent cache row with a confabulated paragraph attached, so the
guard protects the cache at least as much as it protects the model.
"""

import pytest

from app.models import normalize_organism
from app.services import organism_service
from app.services.organism_service import is_summarizable


class TestNormalization:
    @pytest.mark.parametrize(
        "variant",
        ["Homo sapiens", "homo sapiens", "HOMO SAPIENS", "  Homo   sapiens  "],
    )
    def test_case_and_spacing_variants_share_one_key(self, variant):
        """Otherwise one species accumulates a cache row per spelling."""
        assert normalize_organism(variant) == "homo sapiens"

    def test_strain_suffixes_are_kept_because_they_are_different_organisms(self):
        """'E. coli K-12' and 'E. coli O157:H7' deserve different paragraphs."""
        assert normalize_organism("Escherichia coli K-12") != normalize_organism(
            "Escherichia coli O157:H7"
        )


class TestAcceptedValues:
    @pytest.mark.parametrize(
        "organism",
        [
            "Homo sapiens",
            "Escherichia coli K-12",
            "Saccharomyces cerevisiae",
            # Genus alone is a real and describable answer.
            "Escherichia",
        ],
    )
    def test_real_organisms_are_summarizable(self, organism):
        assert is_summarizable(organism) is True


class TestRejectedValues:
    @pytest.mark.parametrize(
        "value",
        ["unknown", "N/A", "none", "not collected", "unspecified", "missing"],
        ids=lambda v: v.replace(" ", "-"),
    )
    def test_placeholders_that_appear_in_real_metadata_are_rejected(self, value):
        assert is_summarizable(value) is False

    @pytest.mark.parametrize(
        "value",
        ["synthetic construct", "metagenome", "uncultured"],
    )
    def test_non_species_values_are_rejected(self, value):
        """Real SRA values, but nothing a species blurb can describe."""
        assert is_summarizable(value) is False

    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", "x", "12345", "---"],
        ids=["none", "empty", "spaces", "too-short", "bare-digits", "punctuation"],
    )
    def test_junk_is_rejected(self, value):
        assert is_summarizable(value) is False

    def test_a_pasted_description_is_too_long_to_be_a_species(self):
        assert is_summarizable("Homo sapiens " * 30) is False


async def _async_none(*a, **k):
    return None


async def _provider(*a, **k):
    from app.models.ai import ProviderKind
    from app.services.ai.router import ResolvedProvider

    return ResolvedProvider(
        provider_id="000000000000000000000000",
        name="Test",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://x:1",
        api_key=None,
        model="test-model",
        models_cache=[],
    )


async def _completion(*a, **k):
    from app.services.ai.adapters import Completion

    return Completion("A bacterium.", "test-model")


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
class TestGetOrGenerate:
    """The blurb generation path -- mocked at the AI seam, not the network."""

    async def test_no_provider_configured_yields_none(self, monkeypatch, beanie_models):
        monkeypatch.setattr(organism_service.ai_router, "resolve", _async_none)
        result = await organism_service.get_or_generate("Homo sapiens")
        assert result is None

    async def test_a_successful_completion_is_cached_and_returned(
        self, monkeypatch, beanie_models
    ):
        monkeypatch.setattr(organism_service.ai_router, "resolve", _provider)
        monkeypatch.setattr(organism_service.ai_complete, "complete", _completion)

        result = await organism_service.get_or_generate("Homo sapiens")
        assert result is not None
        assert result.text == "A bacterium."
        assert result.model == "test-model"

        cached = await organism_service.get_cached("Homo sapiens")
        assert cached is not None
        assert cached.text == "A bacterium."

    async def test_a_non_completion_result_yields_none(self, monkeypatch, beanie_models):
        from app.services.ai.adapters import Failure

        async def _failure(*a, **k):
            return Failure("bad_key")

        monkeypatch.setattr(organism_service.ai_router, "resolve", _provider)
        monkeypatch.setattr(organism_service.ai_complete, "complete", _failure)

        # A distinct organism from the cache-hit test above: that test already
        # wrote a cache row for "Homo sapiens", and a cache hit would short
        # circuit before ever reaching the (patched) completion call.
        result = await organism_service.get_or_generate("Mus musculus")
        assert result is None
