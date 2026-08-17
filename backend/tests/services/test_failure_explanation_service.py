"""The explanation generation path -- mocked at the AI seam, not the
network. Mirrors test_organism_service.py's TestGetOrGenerate exactly.
"""

import pytest

from app.services import failure_explanation_service


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

    return Completion("The process could not find one of its input files.", "test-model")


@pytest.mark.usefixtures("beanie_models")
@pytest.mark.asyncio(loop_scope="module")
class TestGetOrGenerate:
    async def test_no_provider_configured_yields_none(self, monkeypatch, beanie_models):
        monkeypatch.setattr(failure_explanation_service.ai_router, "resolve", _async_none)
        result = await failure_explanation_service.get_or_generate(
            "CalledProcessError", "exit status 1"
        )
        assert result is None

    async def test_a_successful_completion_is_cached_and_returned(
        self, monkeypatch, beanie_models
    ):
        monkeypatch.setattr(failure_explanation_service.ai_router, "resolve", _provider)
        monkeypatch.setattr(failure_explanation_service.ai_complete, "complete", _completion)

        result = await failure_explanation_service.get_or_generate(
            "CalledProcessError", "exit status 1"
        )
        assert result is not None
        assert result.text == "The process could not find one of its input files."
        assert result.model == "test-model"

        cached = await failure_explanation_service.get_cached(
            "CalledProcessError", "exit status 1"
        )
        assert cached is not None
        assert cached.text == "The process could not find one of its input files."

    async def test_a_cache_hit_does_not_call_the_model_again(
        self, monkeypatch, beanie_models
    ):
        # Reuses the row written by the previous test -- same code/message,
        # so this must be a cache hit.
        monkeypatch.setattr(failure_explanation_service.ai_router, "resolve", _provider)

        def must_not_run(*a, **k):
            raise AssertionError("a cache hit must not call the model")

        monkeypatch.setattr(failure_explanation_service.ai_complete, "complete", must_not_run)

        result = await failure_explanation_service.get_or_generate(
            "CalledProcessError", "exit status 1"
        )
        assert result is not None
        assert result.text == "The process could not find one of its input files."

    async def test_a_non_completion_result_yields_none(self, monkeypatch, beanie_models):
        from app.services.ai.adapters import Failure

        async def _failure(*a, **k):
            return Failure("bad_key")

        monkeypatch.setattr(failure_explanation_service.ai_router, "resolve", _provider)
        monkeypatch.setattr(failure_explanation_service.ai_complete, "complete", _failure)

        # A distinct code/message pair from the cache-hit tests above, so
        # this reaches the (patched) completion call rather than short
        # circuiting on an existing row.
        result = await failure_explanation_service.get_or_generate(
            "KeyError", "'reference_path'"
        )
        assert result is None
