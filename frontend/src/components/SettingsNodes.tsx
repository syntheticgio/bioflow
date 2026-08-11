import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import type {
  NodeInfo,
  NodeProvisionRequest,
  NodeProvisionStatus,
} from "../api/types";
import { SettingsNav } from "./SettingsNav";

export function SettingsNodes() {
  const [showForm, setShowForm] = useState(false);
  const [taskId, setTaskId] = useState<string | null>(null);

  const nodes = useQuery({
    queryKey: ["nodes"],
    queryFn: api.nodes,
    refetchInterval: 10_000,
  });

  const provisionStatus = useQuery({
    queryKey: ["provision", taskId],
    queryFn: () => api.getProvisionStatus(taskId!),
    enabled: !!taskId,
    refetchInterval: (query) => {
      const data = query.state.data as NodeProvisionStatus | undefined;
      return data?.status === "provisioning" ? 3000 : false;
    },
  });

  const provision = useMutation({
    mutationFn: (body: NodeProvisionRequest) => api.provisionNode(body),
    onSuccess: (data) => {
      setTaskId(data.task_id);
    },
  });

  const status = provisionStatus.data;
  const isDone = status && status.status !== "provisioning";

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

  const handleRetry = () => {
    setTaskId(null);
    setShowForm(true);
  };

  return (
    <div className="settings-page">
      <SettingsNav />
      <h1>Settings · Nodes</h1>

      <div className="settings-body">
        {/* Provisioning area */}
        {!taskId && !showForm && (
          <button
            type="button"
            className="btn provision-add-btn"
            onClick={() => setShowForm(true)}
          >
            + Add Node
          </button>
        )}

        {showForm && !taskId && (
          <ProvisionForm
            onSubmit={(body) => provision.mutate(body)}
            submitting={provision.isPending}
            onCancel={() => setShowForm(false)}
          />
        )}

        {taskId && status && status.status === "provisioning" && (
          <ProvisionProgress status={status} />
        )}

        {isDone && status && (
          <ProvisionResult
            status={status}
            onRetry={handleRetry}
            onClose={() => setTaskId(null)}
          />
        )}

        {/* Node table */}
        {list.length === 0 && !taskId ? (
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

/* ── Provision form ── */

type AuthTab = "password" | "key";

interface FormFields {
  host: string;
  port: number;
  username: string;
  password: string;
  privateKey: string;
  nodeName: string;
  storage: string;
  replicas: number;
}

function ProvisionForm({
  onSubmit,
  submitting,
  onCancel,
}: {
  onSubmit: (body: NodeProvisionRequest) => void;
  submitting: boolean;
  onCancel: () => void;
}) {
  const [authTab, setAuthTab] = useState<AuthTab>("password");
  const [fields, setFields] = useState<FormFields>({
    host: "",
    port: 22,
    username: "",
    password: "",
    privateKey: "",
    nodeName: "",
    storage: "/data/scratch",
    replicas: 2,
  });
  const [error, setError] = useState<string | null>(null);

  const set = (key: keyof FormFields, value: string | number) =>
    setFields((f) => ({ ...f, [key]: value }));

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!fields.host.trim()) {
      setError("Hostname is required.");
      return;
    }
    if (!fields.username.trim()) {
      setError("Username is required.");
      return;
    }
    if (authTab === "password" && !fields.password) {
      setError("Password is required.");
      return;
    }
    if (authTab === "key" && !fields.privateKey.trim()) {
      setError("Private key is required.");
      return;
    }
    if (!fields.nodeName.trim()) {
      setError("Node name is required.");
      return;
    }

    onSubmit({
      host: fields.host.trim(),
      port: fields.port,
      username: fields.username.trim(),
      password: authTab === "password" ? fields.password : null,
      private_key: authTab === "key" ? fields.privateKey.trim() : null,
      node_name: fields.nodeName.trim(),
      storage_location: fields.storage.trim(),
      worker_replicas: fields.replicas,
    });
  };

  return (
    <form className="provision-form" onSubmit={handleSubmit}>
      <div className="provision-form-title">Provision a compute node</div>
      <p className="provision-form-desc">
        Install a BioFlow worker on a remote machine over SSH. Credentials are
        used only for this install and are not stored.
      </p>

      {error && <div className="provision-error">{error}</div>}

      <div className="provision-field-row">
        <label className="provision-label">
          Hostname / IP
          <input
            type="text"
            className="provision-input"
            placeholder="192.168.1.50"
            value={fields.host}
            onChange={(e) => set("host", e.target.value)}
          />
        </label>
        <label className="provision-label provision-port">
          Port
          <input
            type="number"
            className="provision-input"
            min={1}
            max={65535}
            value={fields.port}
            onChange={(e) => set("port", parseInt(e.target.value) || 22)}
          />
        </label>
      </div>

      <label className="provision-label">
        Username
        <input
          type="text"
          className="provision-input"
          placeholder="jane"
          value={fields.username}
          onChange={(e) => set("username", e.target.value)}
        />
      </label>

      <div className="provision-auth-section">
        <div className="provision-auth-tabs">
          <button
            type="button"
            className={`provision-auth-tab${authTab === "password" ? " active" : ""}`}
            onClick={() => setAuthTab("password")}
          >
            Password
          </button>
          <button
            type="button"
            className={`provision-auth-tab${authTab === "key" ? " active" : ""}`}
            onClick={() => setAuthTab("key")}
          >
            Private Key
          </button>
        </div>

        {authTab === "password" ? (
          <input
            type="password"
            className="provision-input"
            placeholder="SSH password"
            value={fields.password}
            onChange={(e) => set("password", e.target.value)}
          />
        ) : (
          <textarea
            className="provision-input provision-keyarea"
            placeholder="-----BEGIN OPENSSH PRIVATE KEY-----&#10;...&#10;-----END OPENSSH PRIVATE KEY-----"
            rows={4}
            value={fields.privateKey}
            onChange={(e) => set("privateKey", e.target.value)}
          />
        )}
      </div>

      <label className="provision-label">
        Node Name
        <input
          type="text"
          className="provision-input"
          placeholder="child-laptop"
          value={fields.nodeName}
          onChange={(e) => set("nodeName", e.target.value)}
        />
      </label>

      <label className="provision-label">
        Storage Location
        <input
          type="text"
          className="provision-input"
          placeholder="/data/scratch"
          value={fields.storage}
          onChange={(e) => set("storage", e.target.value)}
        />
      </label>

      <label className="provision-label provision-replicas">
        Worker Replicas
        <input
          type="number"
          className="provision-input"
          min={1}
          max={8}
          value={fields.replicas}
          onChange={(e) => set("replicas", parseInt(e.target.value) || 2)}
        />
      </label>

      <div className="provision-form-actions">
        <button
          type="submit"
          className="btn btn-primary"
          disabled={submitting}
        >
          {submitting ? "Provisioning…" : "Provision Node"}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={onCancel}
          disabled={submitting}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

/* ── Progress display ── */

function ProvisionProgress({ status }: { status: NodeProvisionStatus }) {
  const phaseLabel = status.phase
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());

  return (
    <div className="provision-progress">
      <div className="provision-progress-header">
        <span className="provision-progress-phase">{phaseLabel}</span>
        <span className="provision-progress-host">{status.host}</span>
      </div>
      <p className="provision-progress-msg">{status.message}</p>
      <div className="provision-bar-track">
        <div
          className="provision-bar-fill"
          style={{
            width:
              status.pct != null ? `${Math.round(status.pct)}%` : undefined,
            animation: status.pct == null ? "provision-indeterminate 1.5s ease-in-out infinite" : undefined,
          }}
        />
      </div>
    </div>
  );
}

/* ── Result (success / failure) ── */

function ProvisionResult({
  status,
  onRetry,
  onClose,
}: {
  status: NodeProvisionStatus;
  onRetry: () => void;
  onClose: () => void;
}) {
  const isSuccess = status.status === "success";

  return (
    <div className={`provision-result${isSuccess ? " success" : " failure"}`}>
      <div className="provision-result-icon">{isSuccess ? "✓" : "✕"}</div>
      <div className="provision-result-text">
        <strong>
          {isSuccess ? "Node enrolled" : "Provisioning failed"}
        </strong>
        <p>
          {isSuccess
            ? `${status.node_name} is now connected.`
            : status.error || status.message}
        </p>
      </div>
      <div className="provision-result-actions">
        {!isSuccess && (
          <button type="button" className="btn btn-secondary" onClick={onRetry}>
            Try Again
          </button>
        )}
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          {isSuccess ? "Dismiss" : "Close"}
        </button>
      </div>
    </div>
  );
}

/* ── Node row ── */

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
