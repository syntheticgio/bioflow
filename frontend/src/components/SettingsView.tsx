import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import type { AiPreset } from "../api/types";
import { notify } from "../stores/messageStore";
import { ModalBackdrop } from "./ModalBackdrop";
import { ProviderForm } from "./ProviderForm";
import { ProviderList } from "./ProviderList";
import { SettingsNav } from "./SettingsNav";
import { TaskRoutingPanel } from "./TaskRoutingPanel";

/**
 * Where the AI providers are configured.
 *
 * Master-detail: the rail lists providers plus a routing entry, and the pane
 * shows whichever is selected. Because routing lives behind a click here, each
 * provider's detail carries a "Used by" line -- otherwise "what is actually
 * using Anthropic?" would be unanswerable while looking at Anthropic.
 *
 * `SettingsNav` sits above this rather than wrapping it, so `/settings/tools`
 * (SettingsTools.tsx) can render beside the same section rail without also
 * carrying this component's AI-specific queries and state.
 */
export function SettingsView() {
  const [selected, setSelected] = useState<string>("routing");
  const [adding, setAdding] = useState(false);

  const providers = useQuery({ queryKey: ["ai", "providers"], queryFn: api.aiProviders });
  const routing = useQuery({ queryKey: ["ai", "routing"], queryFn: api.aiRouting });

  if (providers.isLoading || routing.isLoading) {
    return (
      <div className="settings-page">
        <SettingsNav />
        <div>Loading…</div>
      </div>
    );
  }
  if (providers.isError || routing.isError) {
    return (
      <div className="settings-page">
        <SettingsNav />
        <div>Could not load settings.</div>
      </div>
    );
  }

  const list = providers.data ?? [];
  const current = list.find((p) => p.id === selected);

  return (
    <div className="settings-page">
      <SettingsNav />
      <h1>Settings · AI</h1>

      <div className="settings-body">
        <ProviderList
          providers={list}
          selected={selected}
          onSelect={setSelected}
          onAdd={() => setAdding(true)}
        />

        {current ? (
          <ProviderForm provider={current} />
        ) : (
          <TaskRoutingPanel routing={routing.data!} providers={list} />
        )}
      </div>

      <p className="settings-security-note">
        API keys are encrypted at rest. Anyone with access to this machine can
        decrypt them — this is not a hardened system.
      </p>

      {adding && (
        <AddProviderModal
          onClose={() => setAdding(false)}
          onCreated={(id) => {
            setAdding(false);
            setSelected(id);
          }}
        />
      )}
    </div>
  );
}

/** Picking a preset fills the base URL and the adapter kind. Both stay
 *  editable: a mainland DashScope account and a non-default LM Studio port are
 *  the same provider with a different URL. */
function AddProviderModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const queryClient = useQueryClient();
  const presets = useQuery({
    queryKey: ["ai", "presets"],
    queryFn: api.aiPresets,
    staleTime: Infinity,
  });

  const [presetId, setPresetId] = useState("");
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");

  const preset: AiPreset | undefined = presets.data?.find((p) => p.id === presetId);

  const choosePreset = (id: string) => {
    setPresetId(id);
    const p = presets.data?.find((x) => x.id === id);
    if (p) {
      setBaseUrl(p.base_url);
      if (!name) setName(p.label);
    }
  };

  const create = useMutation({
    mutationFn: () =>
      api.createAiProvider({
        name,
        kind: preset?.kind ?? "openai_compat",
        base_url: baseUrl,
        model: "",
        ...(apiKey ? { api_key: apiKey } : {}),
      }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ["ai", "providers"] });
      notify.success(`Added ${created.name}. Press Fetch models to test it.`);
      onCreated(created.id);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  return (
    <ModalBackdrop onClick={onClose} onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <div className="settings-modal" onClick={(e) => e.stopPropagation()}>
        <h2>Add provider</h2>

        <label className="settings-field">
          <span>Provider</span>
          <select
            className="settings-input"
            value={presetId}
            onChange={(e) => choosePreset(e.target.value)}
          >
            <option value="">Choose…</option>
            {(presets.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </label>

        <label className="settings-field">
          <span>Name</span>
          <input
            className="settings-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="What to call this in the routing table"
          />
        </label>

        <label className="settings-field">
          <span>Base URL</span>
          <input
            className="settings-input"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            spellCheck={false}
          />
        </label>

        {preset?.needs_key !== false && (
          <label className="settings-field">
            <span>API key</span>
            <input
              className="settings-input"
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="off"
            />
          </label>
        )}

        <div className="settings-actions">
          <button
            onClick={() => create.mutate()}
            disabled={!name || !baseUrl || create.isPending}
          >
            Add
          </button>
          <button onClick={onClose}>Cancel</button>
        </div>
      </div>
    </ModalBackdrop>
  );
}
