"""Known providers, as base URLs and labels.

Pure data on purpose. Every entry here is served by one of the two adapters, so
adding a provider next year is one line rather than a class -- which is the
whole reason the adapter split is by wire format rather than by vendor.

Base URLs are the OpenAI-compatible root, without the trailing `/v1`: adapters
append their own paths.
"""

from dataclasses import dataclass

from app.models.ai import ProviderKind


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    kind: ProviderKind
    base_url: str
    needs_key: bool


ALL: list[Preset] = [
    Preset("openai", "OpenAI", ProviderKind.OPENAI_COMPAT, "https://api.openai.com", True),
    Preset("anthropic", "Anthropic", ProviderKind.ANTHROPIC, "https://api.anthropic.com", True),
    Preset("deepseek", "DeepSeek", ProviderKind.OPENAI_COMPAT, "https://api.deepseek.com", True),
    # DashScope's OpenAI-compatible endpoint, not the native DashScope API.
    # The international host; mainland accounts use .aliyuncs.com without the
    # `-intl`, which is why this field stays editable after the preset is picked.
    Preset(
        "qwen",
        "Qwen (DashScope)",
        ProviderKind.OPENAI_COMPAT,
        "https://dashscope-intl.aliyuncs.com/compatible-mode",
        True,
    ),
    Preset("moonshot", "Moonshot (Kimi)",
           ProviderKind.OPENAI_COMPAT, "https://api.moonshot.ai", True),
    Preset("zhipu", "Zhipu (GLM)",
           ProviderKind.OPENAI_COMPAT, "https://open.bigmodel.cn/api/paas", True),
    Preset("openrouter", "OpenRouter",
           ProviderKind.OPENAI_COMPAT, "https://openrouter.ai/api", True),
    # The default this feature started as. Not a plugin system: a base URL the
    # user edits, covering LM Studio, Ollama, vLLM and anything else local.
    Preset(
        "local",
        "Local / custom",
        ProviderKind.OPENAI_COMPAT,
        "http://host.docker.internal:11234",
        False,
    ),
]


def by_id(preset_id: str) -> Preset | None:
    return next((p for p in ALL if p.id == preset_id), None)
