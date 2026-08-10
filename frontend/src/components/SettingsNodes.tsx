import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import type { NodeInfo } from "../api/types";
import { SettingsNav } from "./SettingsNav";

export function SettingsNodes() {
  const nodes = useQuery({
    queryKey: ["nodes"],
    queryFn: api.nodes,
    refetchInterval: 10_000,
  });

  if (nodes.isLoading) {
    return (
      <div className="settings-page">
        <SettingsNav />
        <p>Loading…</p>
      </div>
    );
  }

  if (nodes.isError) {
    return (
      <div className="settings-page">
        <SettingsNav />
        <p className="error">Could not load node list.</p>
      </div>
    );
  }

  const list = nodes.data ?? [];

  return (
    <div className="settings-page">
      <SettingsNav />
      <h1>Settings · Nodes</h1>

      <div className="settings-body">
        {list.length === 0 ? (
          <p className="muted">
            No workers connected. Start a worker to see it here.
          </p>
        ) : (
          <table className="nodes-table">
            <thead>
              <tr>
                <th>Node</th>
                <th>Status</th>
                <th>Workers</th>
                <th>Running</th>
                <th>Reserved CPU</th>
                <th>Reserved RAM</th>
              </tr>
            </thead>
            <tbody>
              {list.map((n) => (
                <NodeRow key={n.node_id} node={n} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function NodeRow({ node }: { node: NodeInfo }) {
  const memMb = node.reserved.mem_mb;
  const memLabel =
    memMb >= 1024
      ? `${(memMb / 1024).toFixed(1)} GB`
      : `${memMb} MB`;

  return (
    <tr className={node.online ? "" : "offline"}>
      <td className="nodes-name">{node.node_id}</td>
      <td>
        <span className={`nodes-status ${node.online ? "online" : ""}`}>
          {node.online ? "Online" : "Offline"}
        </span>
      </td>
      <td>{node.online_workers}/{node.workers}</td>
      <td>{node.running_jobs}</td>
      <td>{node.reserved.cpu} CPU</td>
      <td>{memLabel}</td>
    </tr>
  );
}
