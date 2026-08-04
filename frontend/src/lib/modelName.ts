/**
 * Model ids are raw strings from wherever they came from -- a GGUF filename
 * fetched off a local server, an OpenRouter slug, hand-typed text. There is
 * no registry of "real" model names to look them up against, so this is a
 * best-effort formatter: known families get a hand-written alias, everything
 * else gets generic cleanup (strip quant/version noise, title-case). Falls
 * back to the raw id whenever a step would produce something empty or worse
 * than what it started with.
 */
const KNOWN_ALIASES: [RegExp, string][] = [
  [/^gemma-?4-?e2b/i, "Gemma 4 E2B"],
  [/^gemma-?3-?e2b/i, "Gemma 3 E2B"],
  [/^gemma-?2/i, "Gemma 2"],
  [/^deepseek-?v?4-?flash/i, "DeepSeek v4 Flash"],
  [/^deepseek-?v?3/i, "DeepSeek v3"],
  [/^deepseek-?r1/i, "DeepSeek R1"],
  [/^qwen-?3/i, "Qwen 3"],
  [/^qwen-?2\.?5/i, "Qwen 2.5"],
  [/^llama-?3\.?3/i, "Llama 3.3"],
  [/^llama-?3\.?1/i, "Llama 3.1"],
  [/^llama-?3/i, "Llama 3"],
  [/^mistral-?small/i, "Mistral Small"],
  [/^mixtral/i, "Mixtral"],
  [/^phi-?4/i, "Phi-4"],
];

export function formatModelName(rawModel: string): string | null {
  const raw = rawModel.trim();
  if (!raw) return null;

  // OpenRouter-style "vendor/model" slugs -- only the model half is a name.
  const afterSlash = raw.includes("/") ? raw.split("/").pop()! : raw;

  for (const [pattern, alias] of KNOWN_ALIASES) {
    if (pattern.test(afterSlash)) return alias;
  }

  const cleaned = afterSlash
    // Quantization / format suffixes: -Q6_K, -q4_0, .gguf, -GGUF, -fp16, -int8
    .replace(/[-_.]?(?:q\d(?:_\d)?|gguf|fp16|fp32|int[48]|bf16)\b/gi, "")
    // Instruction/chat-tune suffixes that add no identifying info here.
    .replace(/[-_](?:instruct|chat|it)\b/gi, "")
    .replace(/[-_]+/g, " ")
    .trim();

  if (!cleaned) return raw;

  return cleaned
    .split(" ")
    .map((word) =>
      /^v?\d/i.test(word) ? word.toUpperCase() : word.charAt(0).toUpperCase() + word.slice(1),
    )
    .join(" ");
}

/** "MLX Core (Gemma 4 E2B)" -- falls back to just the provider name when
 *  there is no model set yet (a freshly added, unconfigured provider). */
export function providerDisplayName(name: string, model: string): string {
  const formatted = formatModelName(model);
  return formatted ? `${name} (${formatted})` : name;
}
