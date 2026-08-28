import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { ApiRequestError, api } from "../api/client";
import type {
  NodeInfo,
  NodeProvisionRequest,
  NodeProvisionStatus,
  NodeUpdateStatus,
} from "../api/types";
import {
  nodeStatusBadge,
  storageStatus,
  updateAffordance,
  versionLabel,
} from "../lib/nodeStaleness";
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

  /* ── Update control (singleton dialog + progress panel, shared by every row) ── */

  const currentVersion = useQuery({
    queryKey: ["nodes", "current-version"],
    queryFn: api.currentVersion,
  });

  const [pendingUpdate, setPendingUpdate] = useState<NodeInfo | null>(null);
  const [updateTaskId, setUpdateTaskId] = useState<string | null>(null);

  const updateStatus = useQuery({
    queryKey: ["node-update", updateTaskId],
    queryFn: () => api.getUpdateStatus(updateTaskId!),
    enabled: !!updateTaskId,
    refetchInterval: (query) => {
      const data = query.state.data as NodeUpdateStatus | undefined;
      return data?.status === "updating" ? 3000 : false;
    },
  });

  const startUpdate = useMutation({
    mutationFn: ({ nodeId, drain }: { nodeId: string; drain: boolean }) =>
      api.updateNode(nodeId, drain),
    onSuccess: (data) => {
      setUpdateTaskId(data.task_id);
      setPendingUpdate(null);
    },
  });

  /* ── Revoke control (same singleton-dialog shape as the update above) ── */

  const [pendingRevoke, setPendingRevoke] = useState<NodeInfo | null>(null);

  const revoke = useMutation({
    mutationFn: (nodeId: string) => api.revokeNode(nodeId),
    onSuccess: () => {
      setPendingRevoke(null);
      // The badge is derived from the server's `enrollment`, so a refetch is
      // what makes the revocation visible -- here and equally for a node
      // revoked from somewhere else.
      nodes.refetch();
    },
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

  const handleRetry = () => {
    setTaskId(null);
    setShowForm(true);
  };

  return (
    <div className="settings-page">
      <SettingsNav />
      <h1>Settings · Nodes</h1>

      <div className="settings-body settings-body-stacked">
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
                <th>Storage</th>
                <th>Version</th>
                <th>Workers</th>
                <th>Running</th>
                <th>Queued</th>
                <th>Reserved CPU</th>
                <th>Reserved RAM</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {list.map((n) => (
                <NodeRow
                  key={n.node_id}
                  node={n}
                  primaryDigest={currentVersion.data?.image_digest ?? null}
                  onUpdateClick={(n) => {
                    setUpdateTaskId(null);
                    setPendingUpdate(n);
                  }}
                  onRevokeClick={(n) => {
                    revoke.reset();
                    setPendingRevoke(n);
                  }}
                />
              ))}
            </tbody>
          </table>
        )}

        {pendingUpdate && (
          <UpdateConfirmDialog
            node={pendingUpdate}
            submitting={startUpdate.isPending}
            error={
              startUpdate.isError
                ? startUpdate.error instanceof ApiRequestError
                  ? startUpdate.error.message
                  : "Could not start the update."
                : null
            }
            onConfirm={(drain) =>
              startUpdate.mutate({ nodeId: pendingUpdate.node_id, drain })
            }
            onCancel={() => setPendingUpdate(null)}
          />
        )}

        {pendingRevoke && (
          <RevokeConfirmDialog
            node={pendingRevoke}
            submitting={revoke.isPending}
            error={
              revoke.isError
                ? revoke.error instanceof ApiRequestError
                  ? revoke.error.message
                  : "Could not revoke the node."
                : null
            }
            onConfirm={() => revoke.mutate(pendingRevoke.node_id)}
            onCancel={() => setPendingRevoke(null)}
          />
        )}

        {updateTaskId && updateStatus.data && (
          <UpdateProgress
            status={updateStatus.data}
            onClose={() => setUpdateTaskId(null)}
          />
        )}
      </div>
    </div>
  );
}

/* ── Provision form ── */

// Mirrors the server's ProvisionRequest validators (backend/app/api/v1/nodes.py).
// Both values are interpolated into commands that run on the remote node, so the
// server refuses anything outside these shapes; matching here is only so the user
// gets a sentence naming the field instead of a raw 422. The server remains the
// authority -- this is a courtesy, not the check.
const NODE_NAME_RE = /^[A-Za-z0-9._-]{1,64}$/;
const STORAGE_LOCATION_RE = /^(\/[A-Za-z0-9._-]+)+$/;

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
    if (!NODE_NAME_RE.test(fields.nodeName.trim())) {
      setError(
        "Node name may use only letters, digits, dot, underscore and hyphen (max 64 characters).",
      );
      return;
    }
    if (
      !STORAGE_LOCATION_RE.test(fields.storage.trim()) ||
      fields.storage.trim().split("/").includes("..")
    ) {
      setError(
        "Storage location must be an absolute path using letters, digits, dot, underscore and hyphen — no spaces or trailing slash.",
      );
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

      <p className="muted provision-key-notice">
        BioFlow will install its own SSH key on this machine and keep it, so it can
        update the node later. Your password is used once and is not stored. The
        key is encrypted on this machine; anyone with shell access here can read it.
      </p>

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

function NodeRow({
  node,
  primaryDigest,
  onUpdateClick,
  onRevokeClick,
}: {
  node: NodeInfo;
  primaryDigest: string | null;
  onUpdateClick: (node: NodeInfo) => void;
  onRevokeClick: (node: NodeInfo) => void;
}) {
  const memMb = node.reserved.mem_mb;
  const memLabel =
    memMb >= 1024
      ? `${(memMb / 1024).toFixed(1)} GB`
      : `${memMb} MB`;

  const storage = storageStatus({
    storageShared: node.storage_shared,
    storageLocation: node.storage_location,
  });

  const affordance = updateAffordance({
    imageDigest: node.image_digest,
    updatable: node.updatable,
    primaryDigest,
    enrollment: node.enrollment,
  });
  const badge = nodeStatusBadge({
    enrollment: node.enrollment,
    online: node.online,
    workers: node.workers,
    nodeId: node.node_id,
  });
  const revoked = node.enrollment === "revoked";

  return (
    <tr className={node.online && !revoked ? "" : "offline"}>
      <td className="nodes-name">{node.node_id}</td>
      <td>
        <span className={`nodes-status ${badge.modifier}`} title={badge.title}>
          {badge.label}
        </span>
      </td>
      <td>
        <span
          className={`nodes-storage ${storage.kind}`}
          title={storage.title}
        >
          {storage.label}
        </span>
      </td>
      <td>{versionLabel(node.version)}</td>
      <td>{node.online_workers}/{node.workers}</td>
      <td>{node.running_jobs}</td>
      <td>{node.queued_jobs}</td>
      <td>{node.reserved.cpu} CPU</td>
      <td>{memLabel}</td>
      <td>
        {affordance.kind === "available" && (
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => onUpdateClick(node)}
          >
            Update
          </button>
        )}
        {affordance.kind === "unavailable" && (
          <button
            type="button"
            className="btn btn-secondary"
            disabled
            title={affordance.reason}
          >
            Update
          </button>
        )}
        {!revoked && (
          <button
            type="button"
            className="btn btn-secondary nodes-revoke"
            onClick={() => onRevokeClick(node)}
            title={`Stop "${node.node_id}" claiming jobs. Does not uninstall anything.`}
          >
            Revoke
          </button>
        )}
      </td>
    </tr>
  );
}

/* ── Revoke confirmation dialog ── */

function RevokeConfirmDialog({
  node,
  submitting,
  error,
  onConfirm,
  onCancel,
}: {
  node: NodeInfo;
  submitting: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="provision-form">
      <div className="provision-form-title">Revoke {node.node_id}</div>
      {error && <div className="provision-error">{error}</div>}
      <p className="provision-form-desc">
        {node.node_id} will stop claiming new jobs once its workers next check
        in. Nothing is uninstalled: the containers keep running and BioFlow
        stays on the machine.
      </p>
      {node.running_jobs > 0 && (
        <p className="settings-hint settings-hint-warn">
          {node.running_jobs} job{node.running_jobs === 1 ? " is" : "s are"}{" "}
          running on this node. Revoking does not stop{" "}
          {node.running_jobs === 1 ? "it" : "them"}.
        </p>
      )}
      <div className="provision-form-actions">
        <button
          type="button"
          className="btn btn-primary"
          disabled={submitting}
          onClick={onConfirm}
        >
          {submitting ? "Revoking…" : "Revoke"}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={submitting}
          onClick={onCancel}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

/* ── Update confirmation dialog ── */

function UpdateConfirmDialog({
  node,
  submitting,
  error,
  onConfirm,
  onCancel,
}: {
  node: NodeInfo;
  submitting: boolean;
  error: string | null;
  onConfirm: (drain: boolean) => void;
  onCancel: () => void;
}) {
  const hasRunningJobs = node.running_jobs > 0;

  return (
    <div className="provision-form">
      <div className="provision-form-title">Update {node.node_id}</div>
      {error && <div className="provision-error">{error}</div>}
      {hasRunningJobs ? (
        <>
          <p className="provision-form-desc">
            {node.node_id} has {node.running_jobs} job
            {node.running_jobs === 1 ? "" : "s"} running. Finish them first, or
            update now and let them requeue elsewhere.
          </p>
          <div className="provision-form-actions">
            <button
              type="button"
              className="btn btn-primary"
              disabled={submitting}
              onClick={() => onConfirm(true)}
            >
              Finish jobs first
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={submitting}
              onClick={() => onConfirm(false)}
            >
              Update now (jobs requeue)
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={submitting}
              onClick={onCancel}
            >
              Cancel
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="provision-form-desc">
            Update {node.node_id} to the primary&apos;s current version.
          </p>
          <div className="provision-form-actions">
            <button
              type="button"
              className="btn btn-primary"
              disabled={submitting}
              onClick={() => onConfirm(true)}
            >
              {submitting ? "Starting…" : "Update"}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={submitting}
              onClick={onCancel}
            >
              Cancel
            </button>
          </div>
        </>
      )}
    </div>
  );
}

/* ── Update progress panel ── */

function UpdateProgress({
  status,
  onClose,
}: {
  status: NodeUpdateStatus;
  onClose: () => void;
}) {
  const isUpdating = status.status === "updating";
  const isSuccess = status.status === "success";

  return (
    <div
      className={`provision-result${
        isUpdating ? "" : isSuccess ? " success" : " failure"
      }`}
    >
      <div className="provision-result-icon">
        {isUpdating ? "…" : isSuccess ? "✓" : "✕"}
      </div>
      <div className="provision-result-text">
        <strong>
          {isUpdating
            ? `Updating ${status.host}`
            : isSuccess
              ? "Update complete"
              : "Update failed"}
        </strong>
        <p>{status.error ?? status.message}</p>
      </div>
      <div className="provision-result-actions">
        {!isUpdating && (
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Close
          </button>
        )}
      </div>
    </div>
  );
}
