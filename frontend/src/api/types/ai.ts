/**
 * A narrative summary written by the local model, if one is running.
 *
 * Entirely optional and entirely derived: every one of these keys is absent
 * unless a `summarize_object` job has succeeded, and nothing in the app
 * depends on their presence. The model and timestamp ride along because a
 * summary is only as trustworthy as the thing that wrote it and the numbers it
 * saw -- both of which can have moved on since.
 */
export interface AiSummaryFacts {
  ai_summary?: string;
  ai_summary_model?: string | null;
  /** ISO 8601, UTC. */
  ai_summary_at?: string;
  /**
   * Digest of the facts and metadata this summary was written from. Compared
   * against the object's current inputs to tell a summary that still describes
   * the file from one written before the last QC or trim run.
   */
  ai_summary_fingerprint?: string;
}

/** Same shape as AiSummaryFacts, for a differential-expression result. */
export interface DeSummaryFacts {
  ai_de_summary?: string;
  ai_de_summary_model?: string | null;
  ai_de_summary_at?: string;
  ai_de_summary_fingerprint?: string;
}

/** Same shape as AiSummaryFacts, for a VCF's call-set statistics. */
export interface VariantSummaryFacts {
  ai_variant_summary?: string;
  ai_variant_summary_model?: string | null;
  ai_variant_summary_at?: string;
  ai_variant_summary_fingerprint?: string;
}

/** A known provider, offered in the add-provider form. Picking one pre-fills
 *  the base URL; it stays editable afterwards, which is how a mainland
 *  DashScope account or a non-default local port gets configured. */
export interface AiPreset {
  id: string;
  label: string;
  kind: "openai_compat" | "anthropic";
  base_url: string;
  needs_key: boolean;
}

/** A configured provider. Note what is absent: there is no field carrying the
 *  API key. `key_hint` is the masked form and `has_key` is the boolean the
 *  form needs -- the real value never leaves the backend. */
export interface AiProvider {
  id: string;
  name: string;
  kind: "openai_compat" | "anthropic";
  base_url: string;
  model: string;
  key_hint: string | null;
  has_key: boolean;
  models_cache: string[];
  status: "ok" | "failed" | "untested";
  status_reason: string | null;
  checked_at: string | null;
  /** Human labels of the task slots routed here, including "Default". */
  used_by: string[];
}

export interface AiSlot {
  name: string;
  label: string;
}

export interface AiRouting {
  default: string | null;
  /** Only explicitly-overridden slots. An absent slot means "use default". */
  slots: Record<string, string>;
  catalog: AiSlot[];
}

export interface AiFetchModelsResult {
  status: "ok" | "failed";
  models: string[];
  reason: string | null;
  detail: string | null;
}

/** Create and update share a shape, but update omits `api_key` unless the user
 *  typed a new one -- that omission is what preserves the stored key. */
export interface AiProviderInput {
  name?: string;
  kind?: "openai_compat" | "anthropic";
  base_url?: string;
  model?: string;
  api_key?: string | null;
}

// --- Project Q&A chat ---

export interface ConversationTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ProjectConversation {
  turns: ConversationTurn[];
  compacted_summary: string | null;
}

export interface AgentToolCall {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result: string | null;
  ok: boolean | null;
}
