"""The preset table is pure data, and these tests keep it honest."""

from app.models.ai import ProviderKind
from app.services.ai import presets


class TestPresets:
    def test_every_named_provider_is_present(self):
        """The list the settings page offers. Missing one means a user who
        wants it has to know its base URL by heart."""
        ids = {p.id for p in presets.ALL}
        assert ids == {
            "openai",
            "anthropic",
            "deepseek",
            "qwen",
            "moonshot",
            "zhipu",
            "openrouter",
            "local",
        }

    def test_only_anthropic_uses_the_anthropic_adapter(self):
        """Everything else speaks the OpenAI wire format -- that fact is what
        makes two adapters enough."""
        anthropic = [p.id for p in presets.ALL if p.kind == ProviderKind.ANTHROPIC]
        assert anthropic == ["anthropic"]

    def test_hosted_presets_carry_an_https_base_url(self):
        for p in presets.ALL:
            if p.id == "local":
                continue
            assert p.base_url.startswith("https://"), p.id

    def test_local_needs_no_key(self):
        """LM Studio, Ollama and vLLM accept anything or nothing; requiring a
        key would make the common local setup unconfigurable."""
        local = presets.by_id("local")
        assert local is not None
        assert local.needs_key is False

    def test_hosted_presets_need_a_key(self):
        for p in presets.ALL:
            if p.id != "local":
                assert p.needs_key is True, p.id

    def test_by_id_returns_none_for_unknown(self):
        assert presets.by_id("nope") is None
