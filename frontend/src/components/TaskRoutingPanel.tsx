import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { AiProvider, AiRouting } from "../api/types";
import { notify } from "../stores/messageStore";

/**
 * Which provider serves which task.
 *
 * One row per slot in the backend's catalog -- never a hardcoded list, so that
 * adding an AI feature is a backend-only change and its row appears here on its
 * own. "Use default" writes a *deletion* from the slots map rather than a
 * value, which is what makes the default actually follow later changes instead
 * of being copied once.
 */
export function TaskRoutingPanel({
  routing,
  providers,
}: {
  routing: AiRouting;
  providers: AiProvider[];
}) {
  const queryClient = useQueryClient();

  const save = useMutation({
    mutationFn: (next: { default: string | null; slots: Record<string, string> }) =>
      api.setAiRouting(next),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["ai", "routing"] });
      queryClient.invalidateQueries({ queryKey: ["ai", "providers"] });
      queryClient.invalidateQueries({ queryKey: ["pipelines", "summaryStatus"] });
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const setDefault = (value: string) =>
    save.mutate({ default: value || null, slots: routing.slots });

  const setSlot = (slot: string, value: string) => {
    const slots = { ...routing.slots };
    if (value) {
      slots[slot] = value;
    } else {
      // Deletion, not an empty string: absence is what "use default" means.
      delete slots[slot];
    }
    save.mutate({ default: routing.default, slots });
  };

  return (
    <div className="settings-detail">
      <h2>Task routing</h2>
      <p className="settings-hint">
        Each AI feature can use its own provider. Anything left on “Use default”
        follows the default below.
      </p>

      {providers.length === 0 ? (
        <p className="settings-hint">Add a provider first — there is nothing to route to.</p>
      ) : (
        <table className="settings-table">
          <tbody>
            <tr>
              <th scope="row">Default</th>
              <td>
                <select
                  className="settings-input"
                  value={routing.default ?? ""}
                  onChange={(e) => setDefault(e.target.value)}
                  disabled={save.isPending}
                >
                  <option value="">Nothing — AI features off</option>
                  {providers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </td>
            </tr>

            {routing.catalog.map((slot) => (
              <tr key={slot.name}>
                <th scope="row">{slot.label}</th>
                <td>
                  <select
                    className="settings-input"
                    value={routing.slots[slot.name] ?? ""}
                    onChange={(e) => setSlot(slot.name, e.target.value)}
                    disabled={save.isPending}
                  >
                    <option value="">Use default</option>
                    {providers.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
