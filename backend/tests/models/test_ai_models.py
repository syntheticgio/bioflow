"""Shape of the AI provider and routing documents."""

import pytest
from app.models.ai import AiProvider, AiRouting, FailureReason, ProviderKind, TaskSlot

pytestmark = pytest.mark.asyncio(loop_scope="module")


class TestTaskSlot:
    def test_slots_have_human_labels(self):
        """The settings page renders one row per slot and must not hardcode
        the labels -- they come from here via the routing endpoint."""
        assert TaskSlot.FILE_SUMMARY.label == "File summaries"
        assert TaskSlot.ORGANISM_BLURB.label == "Organism blurbs"

    def test_every_slot_has_a_label(self):
        for slot in TaskSlot:
            assert slot.label
            assert slot.label != slot.value


class TestAiProvider:
    async def test_defaults(self, beanie_models):
        p = AiProvider(name="Local", kind=ProviderKind.OPENAI_COMPAT, base_url="http://x:1234")
        assert p.status == "untested"
        assert p.models_cache == []
        assert p.api_key_enc is None
        assert p.key_hint is None
        assert p.model == ""

    async def test_round_trips(self, beanie_models):
        p = AiProvider(
            name="Anthropic",
            kind=ProviderKind.ANTHROPIC,
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4-6",
        )
        await p.insert()
        found = await AiProvider.get(p.id)
        assert found is not None
        assert found.kind == ProviderKind.ANTHROPIC


class TestAiRouting:
    async def test_singleton_id_is_fixed(self, beanie_models):
        """A fixed id is what makes this a singleton -- two concurrent
        writers upsert the same document rather than creating a second one
        that silently shadows the first."""
        r = await AiRouting.load()
        r2 = await AiRouting.load()
        assert r.id == r2.id == AiRouting.SINGLETON_ID

    async def test_empty_slots_mean_use_default(self, beanie_models):
        r = await AiRouting.load()
        assert r.slots == {}
        assert r.default is None


class TestFailureReason:
    def test_values(self):
        assert FailureReason.INVALID_KEY == "invalid_key"
        assert FailureReason.RATE_LIMITED == "rate_limited"
        assert FailureReason.MODEL_NOT_FOUND == "model_not_found"
        assert FailureReason.UNREACHABLE == "unreachable"
        assert FailureReason.BAD_RESPONSE == "bad_response"
