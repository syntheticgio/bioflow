import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import { SettingsNav } from "./SettingsNav";

/**
 * Miscellaneous app-wide toggles that don't belong under AI, Tools, or
 * Resources. Feedback is the first: hidden from the Help menu and its route
 * blocked until turned on here, off by default.
 */
export function SettingsGeneral() {
  const queryClient = useQueryClient();

  const settings = useQuery({
    queryKey: ["settings", "general"],
    queryFn: api.generalSettings,
  });

  const [feedbackEnabled, setFeedbackEnabled] = useState(false);

  useEffect(() => {
    if (!settings.data) return;
    setFeedbackEnabled(settings.data.feedback_enabled);
  }, [settings.data]);

  const save = useMutation({
    mutationFn: () => api.setGeneralSettings({ feedback_enabled: feedbackEnabled }),
    onSuccess: () => {
      notify.success("Settings saved.");
      queryClient.invalidateQueries({ queryKey: ["settings", "general"] });
    },
    onError: (e: Error) => notify.error(e.message),
  });

  if (settings.isLoading) {
    return (
      <div className="settings-page">
        <SettingsNav />
        <div>Loading…</div>
      </div>
    );
  }
  if (settings.isError || !settings.data) {
    return (
      <div className="settings-page">
        <SettingsNav />
        <div>Could not load settings.</div>
      </div>
    );
  }

  return (
    <div className="settings-page">
      <SettingsNav />
      <h1>Settings · General</h1>

      <label className="settings-field">
        <span>
          <input
            type="checkbox"
            checked={feedbackEnabled}
            onChange={(e) => setFeedbackEnabled(e.target.checked)}
          />{" "}
          Show the Feedback page
        </span>
      </label>
      <p className="settings-hint">
        When off, Feedback is hidden from the Help menu and cannot be opened
        directly.
      </p>

      <div className="settings-actions">
        <button onClick={() => save.mutate()} disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}
