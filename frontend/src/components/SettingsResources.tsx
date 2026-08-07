import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type { ResourceLimits } from "../api/types";
import { SettingsNav } from "./SettingsNav";

const MB_PER_GB = 1024;

/**
 * An admission budget, not an enforcement ceiling.
 *
 * BioFlow refuses to *start* a job it predicts will exceed the configured
 * memory limit -- it does not watch or kill a job already running. A job
 * whose actual usage turns out higher than predicted is not stopped, so the
 * copy on this page must never promise a hard cap; see
 * docs/superpowers/specs/2026-08-07-resource-limits-admission-design.md.
 *
 * Memory is shown and edited in GB (friendlier for a human-typed limit than
 * MB) and converted to/from the API's `max_mem_mb` at the edges. `max_cpu`
 * and `max_threads` have no controls here yet -- there is nowhere else in the
 * UI that could have set them, so every save echoes back whatever the GET
 * loaded for them unchanged rather than risking a silent wipe.
 */
export function SettingsResources() {
  const queryClient = useQueryClient();

  const limits = useQuery({
    queryKey: ["settings", "resources"],
    queryFn: api.resourceLimits,
  });

  const [noLimit, setNoLimit] = useState(true);
  const [memGb, setMemGb] = useState("");

  // Seed local form state from the loaded document. Only runs when the
  // fetched data changes (e.g. after a save's invalidation), not on every
  // keystroke -- otherwise typing would be overwritten by the stale query
  // result still cached from before the edit.
  useEffect(() => {
    if (!limits.data) return;
    if (limits.data.max_mem_mb == null) {
      setNoLimit(true);
      setMemGb("");
    } else {
      setNoLimit(false);
      setMemGb(String(limits.data.max_mem_mb / MB_PER_GB));
    }
  }, [limits.data]);

  const save = useMutation({
    mutationFn: () => {
      const current = limits.data as ResourceLimits;
      const max_mem_mb = noLimit
        ? null
        : Math.round(parseFloat(memGb || "0") * MB_PER_GB);
      return api.setResourceLimits({
        max_mem_mb,
        // No UI for these yet -- echo back whatever was loaded so this page
        // never clears a value it didn't offer a way to set.
        max_cpu: current.max_cpu,
        max_threads: current.max_threads,
      });
    },
    onSuccess: () => {
      notify.success("Resource limits saved.");
      queryClient.invalidateQueries({ queryKey: ["settings", "resources"] });
    },
    onError: (e: Error) => notify.error(e.message),
  });

  if (limits.isLoading) {
    return (
      <div className="settings-page">
        <SettingsNav />
        <div>Loading…</div>
      </div>
    );
  }
  if (limits.isError || !limits.data) {
    return (
      <div className="settings-page">
        <SettingsNav />
        <div>Could not load resource limits.</div>
      </div>
    );
  }

  const machineMemGb = (limits.data.machine_mem_mb / MB_PER_GB).toFixed(1);
  const invalidMem = !noLimit && (memGb.trim() === "" || parseFloat(memGb) <= 0);

  return (
    <div className="settings-page">
      <SettingsNav />
      <h1>Settings · Resources</h1>
      <p className="settings-hint">
        This machine has {machineMemGb} GB of memory and {limits.data.machine_cpu}{" "}
        CPU cores available.
      </p>

      <label className="settings-field">
        <span>Memory limit (GB)</span>
        <input
          className="settings-input"
          type="number"
          min="0"
          step="0.5"
          value={memGb}
          disabled={noLimit}
          onChange={(e) => setMemGb(e.target.value)}
        />
      </label>

      <label className="settings-field">
        <span>
          <input
            type="checkbox"
            checked={noLimit}
            onChange={(e) => setNoLimit(e.target.checked)}
          />{" "}
          No limit
        </span>
      </label>

      <p className="settings-hint">
        BioFlow will not start work it expects to exceed this. A job that ends
        up using more than predicted is not stopped -- this is an admission
        check on new work, not a running cap.
      </p>

      <div className="settings-actions">
        <button onClick={() => save.mutate()} disabled={save.isPending || invalidMem}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}
