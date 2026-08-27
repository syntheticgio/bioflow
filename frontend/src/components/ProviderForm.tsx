import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { AiProvider, AiRouting } from "../api/types";
import { notify } from "../stores/messageStore";
import { ModelCombo } from "./ModelCombo";

/**
 * One provider's editable detail.
 *
 * The key field is **write-only**: it renders empty regardless of whether a key
 * is stored, and an empty submit omits `api_key` entirely so the backend keeps
 * what it has. That is the whole reason the PATCH endpoint distinguishes an
 * absent field from a null one -- without it, renaming a provider would wipe
 * its credential, silently, with the failure only surfacing hours later when a
 * summary stopped appearing.
 */
export function ProviderForm({
  provider,
  routing,
}: {
  provider: AiProvider;
  routing: AiRouting;
}) {
  const queryClient = useQueryClient();

  const [name, setName] = useState(provider.name);
  const [baseUrl, setBaseUrl] = useState(provider.base_url);
  // Whether this edit moves the provider to a different host, which is what
  // makes the server invalidate the stored key.
  const baseUrlChanged = baseUrl !== provider.base_url;
  const [model, setModel] = useState(provider.model);
  const [apiKey, setApiKey] = useState("");

  // Re-seed when the selected provider changes: the form is one component
  // reused across the list, so without this it keeps the previous one's values.
  useEffect(() => {
    setName(provider.name);
    setBaseUrl(provider.base_url);
    setModel(provider.model);
    setApiKey("");
  }, [provider.id, provider.name, provider.base_url, provider.model]);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["ai", "providers"] });
    queryClient.invalidateQueries({ queryKey: ["ai", "routing"] });
    queryClient.invalidateQueries({ queryKey: ["pipelines", "summaryStatus"] });
  };

  const save = useMutation({
    mutationFn: () =>
      api.updateAiProvider(provider.id, {
        name,
        base_url: baseUrl,
        model,
        // Present only when the user typed one. See the component docstring.
        ...(apiKey ? { api_key: apiKey } : {}),
      }),
    onSuccess: () => {
      setApiKey("");
      invalidate();
      notify.success("Saved.");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const clearKey = useMutation({
    mutationFn: () => api.updateAiProvider(provider.id, { api_key: null }),
    onSuccess: () => {
      invalidate();
      notify.success("Key cleared.");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const fetchModels = useMutation({
    mutationFn: () => api.fetchAiModels(provider.id),
    onSuccess: (result) => {
      invalidate();
      if (result.status === "ok") {
        notify.success(`Found ${result.models.length} model(s).`);
      } else {
        notify.error(`Could not reach this provider: ${result.reason}`);
      }
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteAiProvider(provider.id),
    onSuccess: () => {
      invalidate();
      notify.success("Provider deleted.");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const isDefault = routing.default === provider.id;

  const setDefault = useMutation({
    mutationFn: () => api.setAiRouting({ default: provider.id, slots: routing.slots }),
    onSuccess: () => {
      invalidate();
      notify.success(`${provider.name} is now the default.`);
    },
    onError: (e: Error) => notify.error(e.message),
  });

  return (
    <div className="settings-detail">
      <h2>{provider.name}</h2>

      <label className="settings-field">
        <span>Name</span>
        <input
          className="settings-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
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

      <label className="settings-field">
        <span>API key</span>
        <input
          className="settings-input"
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={
            !provider.has_key
              ? "No key set"
              : baseUrlChanged
                ? "Re-enter the key for the new Base URL"
                : `Key set (${provider.key_hint}) — leave blank to keep`
          }
          autoComplete="off"
        />
      </label>
      {provider.has_key && baseUrlChanged && (
        // The server drops the stored key when base_url actually changes -- a
        // key is a credential for a host, and carrying it to a new one is how
        // it gets exfiltrated. Saying so here is the difference between that
        // being a safeguard and it being a provider that mysteriously stopped
        // working, since the key field is write-only and shows nothing.
        <p className="settings-hint settings-hint-warn">
          Changing the Base URL clears the saved key, because a key belongs to
          the host it was issued for. Enter the key for the new URL above, or
          save and add it afterwards.
        </p>
      )}
      {provider.has_key && (
        <button
          className="settings-link-button"
          onClick={() => clearKey.mutate()}
          disabled={clearKey.isPending}
        >
          Clear stored key
        </button>
      )}

      <label className="settings-field">
        <span>Model</span>
        <ModelCombo
          value={model}
          options={provider.models_cache}
          onChange={setModel}
          id={`model-${provider.id}`}
        />
      </label>

      <div className="settings-actions">
        <button
          className="btn primary"
          onClick={() => save.mutate()}
          disabled={save.isPending}
        >
          Save
        </button>
        <button
          className="btn"
          onClick={() => fetchModels.mutate()}
          disabled={fetchModels.isPending}
        >
          {fetchModels.isPending ? "Fetching…" : "Fetch models"}
        </button>
        <button
          className="btn"
          onClick={() => setDefault.mutate()}
          disabled={isDefault || setDefault.isPending}
        >
          {isDefault ? "Default" : "Set default"}
        </button>
        <button
          className="btn danger settings-danger"
          onClick={() => {
            if (confirm(`Delete ${provider.name}? Any task using it falls back to the default.`)) {
              remove.mutate();
            }
          }}
          disabled={remove.isPending}
        >
          Delete
        </button>
      </div>

      <ProviderStatus provider={provider} />

      {provider.used_by.length > 0 && (
        <p className="settings-hint">Used by: {provider.used_by.join(", ")}</p>
      )}
    </div>
  );
}

/** The badge. Shows the age of the check, because "ok" from a week ago and
 *  "ok" from a minute ago are different claims. */
function ProviderStatus({ provider }: { provider: AiProvider }) {
  const age = provider.checked_at ? relativeAge(provider.checked_at) : null;

  if (provider.status === "untested") {
    return <p className="settings-status settings-status-untested">Not tested yet</p>;
  }
  if (provider.status === "failed") {
    return (
      <p className="settings-status settings-status-failed">
        Failed{provider.status_reason ? ` — ${humanReason(provider.status_reason)}` : ""}
        {age ? ` · ${age}` : ""}
      </p>
    );
  }
  return (
    <p className="settings-status settings-status-ok">
      Working{age ? ` · checked ${age}` : ""}
    </p>
  );
}

function humanReason(reason: string): string {
  const map: Record<string, string> = {
    invalid_key: "the API key was rejected",
    rate_limited: "rate limited",
    model_not_found: "no such model",
    unreachable: "could not connect",
    bad_response: "unexpected response",
  };
  return map[reason] ?? reason;
}

function relativeAge(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}
