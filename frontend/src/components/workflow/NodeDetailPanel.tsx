/**
 * One node, in full: its tool, its parameters, and what is wired into it.
 *
 * A panel rather than an SVG camera zoom, which is what the issue asked for
 * literally. The substance of this screen is a form, and forms are HTML -- a
 * real zoom would mean `foreignObject` or form controls hand-built in SVG,
 * both worse than the animation is good. Animating out of the node's position
 * keeps what the zoom was *for*: knowing which node you opened.
 *
 * Wiring is shown read-only. Rewiring is a canvas gesture, and a second way to
 * do it here would be a second set of rules to keep in step with
 * `canConnect`.
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { NodeTypeMeta, WorkflowEdge, WorkflowNode } from "../../api/types";
import { acceptedFormats, portsFor } from "../../lib/workflowGraph";
import { ParamForm } from "./ParamForm";

interface Props {
  node: WorkflowNode;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  catalog: Record<string, NodeTypeMeta>;
  onClose: () => void;
  onChangeTool: (nodeId: string, tool: string) => void;
  onChangeParam: (nodeId: string, key: string, value: unknown) => void;
  onChangeLabel: (nodeId: string, label: string) => void;
  onToggleContinue: (nodeId: string, value: boolean) => void;
}

export function NodeDetailPanel({
  node,
  nodes,
  edges,
  catalog,
  onClose,
  onChangeTool,
  onChangeParam,
  onChangeLabel,
  onToggleContinue,
}: Props) {
  const meta = node.node_type ? catalog[node.node_type] : undefined;
  const choice = meta?.tool_choice;
  const chosen = choice ? node.params?.[choice.param_key] : undefined;
  const tool = typeof chosen === "string" ? chosen : choice?.default;
  const { inputs, outputs } = portsFor(node, catalog);
  const nameOf = (id: string) =>
    nodes.find((n) => n.node_id === id)?.label ?? id;

  const schema = useQuery({
    queryKey: ["aligner-schema", tool],
    queryFn: () => api.alignerSchema(tool!),
    enabled: Boolean(node.node_type === "align" && tool && choice),
  });

  // Two sources, one form: an aligner's knobs are fetched per-tool, while a
  // node type whose parameters never vary declares them on its spec. A node
  // may have either, and until now only the fetched kind rendered at all.
  const staticFields = meta?.param_fields ?? [];
  const fields = schema.data?.fields ?? staticFields;

  // The section has two independent reasons to be visible -- a static-fields
  // node has fields to show, or an aligner fetch is in flight and wants its
  // "Loading…" text shown before any fields exist. Name both so the outer
  // gate is derived from the same booleans the inner content checks, rather
  // than a hand-copied OR that can drift out of sync with either branch.
  const showLoading = Boolean(choice && schema.isLoading);
  const showForm = fields.length > 0;

  return (
    <div className="node-detail">
      <div className="node-detail-header">
        <button className="btn" onClick={onClose}>
          ← Back to graph
        </button>
        <h2>{meta?.label ?? node.label ?? node.node_id}</h2>
      </div>

      <section>
        <label>
          <span>Label</span>
          <input
            value={node.label ?? ""}
            placeholder={node.node_id}
            onChange={(e) => onChangeLabel(node.node_id, e.target.value)}
          />
        </label>

        {choice && (
          <label>
            <span>Tool</span>
            <select
              value={tool}
              onChange={(e) => onChangeTool(node.node_id, e.target.value)}
            >
              {choice.options.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        )}

        <label className="checkbox">
          <input
            type="checkbox"
            checked={node.continue_on_failure}
            onChange={(e) => onToggleContinue(node.node_id, e.target.checked)}
          />
          <span>
            Carry on if this step fails
            <em>
              For steps whose failure is survivable -- QC and stats. Everything
              downstream of a load-bearing step is skipped when it fails.
            </em>
          </span>
        </label>
      </section>

      {(showForm || showLoading) && (
        <section>
          <h3>Parameters</h3>
          {showLoading && <p className="muted">Loading…</p>}
          {showForm && (
            <ParamForm
              fields={fields}
              values={node.params ?? {}}
              onChange={(key, value) => onChangeParam(node.node_id, key, value)}
            />
          )}
        </section>
      )}

      <section>
        <h3>Inputs</h3>
        <ul className="node-detail-ports">
          {inputs.map((port) => {
            const wired = edges.filter(
              (e) => e.to_node === node.node_id && e.to_port === port.name,
            );
            return (
              <li key={port.name}>
                <strong>{port.name}</strong>
                <em>
                  {acceptedFormats(port.type).join("/") || "any"}
                  {port.type.role ? `/${port.type.role}` : ""}
                  {port.required ? "" : " (optional)"}
                  {port.multiple ? " (several)" : ""}
                </em>
                <span>
                  {wired.length === 0
                    ? "not connected"
                    : wired.map((e) => nameOf(e.from_node)).join(", ")}
                </span>
              </li>
            );
          })}
          {inputs.length === 0 && <li className="muted">No inputs.</li>}
        </ul>

        <h3>Outputs</h3>
        <ul className="node-detail-ports">
          {outputs.map((port) => {
            const wired = edges.filter(
              (e) => e.from_node === node.node_id && e.from_port === port.name,
            );
            return (
              <li key={port.name}>
                <strong>{port.name}</strong>
                <em>
                  {acceptedFormats(port.type).join("/") || "any"}
                  {port.type.role ? `/${port.type.role}` : ""}
                </em>
                <span>
                  {wired.length === 0
                    ? "not connected"
                    : wired.map((e) => nameOf(e.to_node)).join(", ")}
                </span>
              </li>
            );
          })}
          {outputs.length === 0 && <li className="muted">No outputs.</li>}
        </ul>
      </section>
    </div>
  );
}
