import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";

interface NodeSelectorProps {
  value: string;
  onChange: (nodeId: string) => void;
  /** If true, the selector occupies the full width. Defaults false. */
  fullWidth?: boolean;
}

/** Dropdown that lets the user target a job to a specific worker node.
 *  An empty value means "default" (the global pool). */
export function NodeSelector({ value, onChange, fullWidth }: NodeSelectorProps) {
  const nodes = useQuery({
    queryKey: ["nodes"],
    queryFn: api.nodes,
    refetchInterval: 30_000,
    staleTime: 10_000,
  });

  // The backend groups workers that never declared a node_id into a
  // catch-all bucket named "unknown". It is not a real target: no worker
  // enrolls under it, and selecting it would route the job nowhere useful.
  // Exclude it both from the count (so a single real node hides the
  // selector) and from the offered options.
  const selectable = (nodes.data ?? []).filter((n) => n.node_id !== "unknown");

  if (nodes.isLoading || !nodes.data || selectable.length <= 1) {
    // Nothing to pick: only one node or still loading.
    return null;
  }

  return (
    <div className={`settings-field${fullWidth ? "" : " inline"}`}>
      <span>Target node</span>
      <select
        className="settings-input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">Default (any node)</option>
        {selectable.map((n) => (
          <option key={n.node_id} value={n.node_id}>
            {n.node_id} ({n.online ? "online" : "offline"})
          </option>
        ))}
      </select>
    </div>
  );
}
