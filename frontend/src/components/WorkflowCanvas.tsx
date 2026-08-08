/**
 * The workflow graph editor.
 *
 * A hand-rolled SVG canvas rather than a graph library: this repo's frontend
 * runs on six runtime dependencies, and positions were already modelled
 * (`NodePosition`), so the editor is drag, click-to-wire, and delete over
 * plain SVG.
 *
 * The rules it enforces live in `lib/workflowGraph.ts`, which is where the
 * tests are -- there is no component-testing setup here (no jsdom, zero
 * `.test.tsx`), so behaviour that must be checked automatically has to be pure
 * and live outside the component. Everything below is verified in the browser.
 *
 * Client-side validation mirrors the server's rather than replacing it: every
 * save is validated again by `validate_definition`. What it buys is telling the
 * user before they save, which is the difference between a canvas you can build
 * in and one you fix by trial and error.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useRef, useState } from "react";
import { ApiRequestError, api } from "../api/client";
import type {
  GraphValidationError,
  NodeTypeMeta,
  WorkflowEdge,
  WorkflowNode,
} from "../api/types";
import {
  NODE_WIDTH,
  PORT_RADIUS,
  canConnect,
  edgeKey,
  nextFreeSlot,
  nodeHeight,
  nodePortPosition,
} from "../lib/workflowGraph";

interface PendingWire {
  node_id: string;
  port: string;
  x: number;
  y: number;
}

let nodeCounter = 0;
function freshNodeId(prefix: string): string {
  nodeCounter += 1;
  return `${prefix}_${nodeCounter}`;
}

export function WorkflowCanvas() {
  const queryClient = useQueryClient();
  const svgRef = useRef<SVGSVGElement>(null);

  const [name, setName] = useState("New workflow");
  const [nodes, setNodes] = useState<WorkflowNode[]>([]);
  const [edges, setEdges] = useState<WorkflowEdge[]>([]);
  const [definitionId, setDefinitionId] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingWire | null>(null);
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [serverErrors, setServerErrors] = useState<GraphValidationError[]>([]);
  const dragRef = useRef<{ node_id: string; dx: number; dy: number } | null>(null);

  const palette = useQuery({
    queryKey: ["workflow-node-types"],
    queryFn: () => api.listNodeTypes(),
  });

  const saved = useQuery({
    queryKey: ["workflows"],
    queryFn: () => api.listWorkflows(),
  });

  const catalog: Record<string, NodeTypeMeta> = useMemo(() => {
    const out: Record<string, NodeTypeMeta> = {};
    for (const entry of palette.data ?? []) out[entry.node_type] = entry;
    return out;
  }, [palette.data]);

  const save = useMutation({
    mutationFn: () => {
      const body = { name, description: "", nodes, edges };
      return definitionId
        ? api.updateWorkflow(definitionId, body)
        : api.createWorkflow(body);
    },
    onSuccess: (definition) => {
      setDefinitionId(definition.id);
      setServerErrors([]);
      setNotice(`Saved as version ${definition.version}.`);
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (error: unknown) => {
      // The server returns every problem at once under `details.errors`, which
      // is the whole reason validation returns a list. Showing only
      // `error.message` here would throw that away and put the user back to
      // fixing one wire per save.
      if (error instanceof ApiRequestError && error.code === "invalid_graph") {
        const details = error.details as { errors?: GraphValidationError[] };
        setServerErrors(details?.errors ?? []);
        setNotice(null);
        return;
      }
      setNotice(error instanceof Error ? error.message : "Save failed.");
    },
  });

  const toCanvas = useCallback((event: { clientX: number; clientY: number }) => {
    const box = svgRef.current?.getBoundingClientRect();
    return {
      x: event.clientX - (box?.left ?? 0),
      y: event.clientY - (box?.top ?? 0),
    };
  }, []);

  function addActionNode(meta: NodeTypeMeta) {
    setNodes((current) => [
      ...current,
      {
        node_id: freshNodeId(meta.node_type),
        kind: "action",
        node_type: meta.node_type,
        params: {},
        continue_on_failure: false,
        position: nextFreeSlot(current),
      },
    ]);
  }

  function addInputNode() {
    setNodes((current) => [
      ...current,
      {
        node_id: freshNodeId("input"),
        kind: "input",
        label: "input",
        // FASTQ with no role is the least surprising default: it is the
        // commonest starting file, and `role: null` accepts any role rather
        // than silently excluding files the user expected to bind.
        accepts: { format: "fastq", role: null },
        params: {},
        continue_on_failure: false,
        position: nextFreeSlot(current),
      },
    ]);
  }

  function removeNode(nodeId: string) {
    setNodes((current) => current.filter((n) => n.node_id !== nodeId));
    setEdges((current) =>
      current.filter((e) => e.from_node !== nodeId && e.to_node !== nodeId),
    );
    setSelected(null);
  }

  function startWire(node_id: string, port: string, x: number, y: number) {
    setPending({ node_id, port, x, y });
    // Seed the cursor at the port itself. Without this the pending wire is
    // invisible until the mouse happens to move, so clicking a port looks like
    // nothing happened -- and the user clicks again, cancelling it.
    setCursor({ x, y });
    setNotice(null);
  }

  function finishWire(toNode: string, toPort: string) {
    if (!pending) return;
    const candidate: WorkflowEdge = {
      from_node: pending.node_id,
      from_port: pending.port,
      to_node: toNode,
      to_port: toPort,
    };
    const verdict = canConnect(nodes, edges, catalog, candidate);
    if (!verdict.ok) {
      // Refused here rather than on save: the reason is specific to the wire
      // just attempted, and it is gone from the user's mind by the time a save
      // would report it.
      setNotice(verdict.reason ?? "That wire is not allowed.");
      setPending(null);
      return;
    }
    setEdges((current) => [...current, candidate]);
    setPending(null);
  }

  function onMouseMove(event: React.MouseEvent) {
    const point = toCanvas(event);
    if (pending) setCursor(point);
    const drag = dragRef.current;
    if (drag) {
      setNodes((current) =>
        current.map((n) =>
          n.node_id === drag.node_id
            ? { ...n, position: { x: point.x - drag.dx, y: point.y - drag.dy } }
            : n,
        ),
      );
    }
  }

  const errorsByNode = useMemo(() => {
    const out = new Map<string, string[]>();
    for (const error of serverErrors) {
      if (!error.node_id) continue;
      out.set(error.node_id, [...(out.get(error.node_id) ?? []), error.message]);
    }
    return out;
  }, [serverErrors]);

  function loadDefinition(id: string) {
    api.getWorkflow(id).then((definition) => {
      setDefinitionId(definition.id);
      setName(definition.name);
      setNodes(definition.nodes);
      setEdges(definition.edges);
      setServerErrors([]);
      setNotice(`Loaded "${definition.name}" (v${definition.version}).`);
    });
  }

  function newDefinition() {
    setDefinitionId(null);
    setName("New workflow");
    setNodes([]);
    setEdges([]);
    setServerErrors([]);
    setNotice(null);
  }

  return (
    <div className="workflow-canvas">
      <div className="workflow-toolbar">
        <input
          className="workflow-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label="Workflow name"
        />
        <button className="btn" onClick={addInputNode}>
          Add input
        </button>
        <button
          className="btn primary"
          onClick={() => save.mutate()}
          disabled={save.isPending || nodes.length === 0}
        >
          {save.isPending ? "Saving…" : definitionId ? "Save version" : "Save"}
        </button>
        <button className="btn" onClick={newDefinition}>
          New
        </button>
        <select
          className="workflow-open"
          value={definitionId ?? ""}
          onChange={(e) => e.target.value && loadDefinition(e.target.value)}
          aria-label="Open a saved workflow"
        >
          <option value="">Open…</option>
          {(saved.data ?? []).map((d) => (
            <option key={d.id} value={d.id}>
              {d.name} (v{d.version})
            </option>
          ))}
        </select>
      </div>

      {notice && <div className="workflow-notice">{notice}</div>}
      {serverErrors.length > 0 && (
        <ul className="workflow-errors">
          {serverErrors.map((error, i) => (
            <li key={i}>
              {error.node_id ? <strong>{error.node_id}: </strong> : null}
              {error.message}
            </li>
          ))}
        </ul>
      )}

      <div className="workflow-body">
        <aside className="workflow-palette">
          <h4>Tools</h4>
          {palette.isLoading && <p className="muted">Loading…</p>}
          {(palette.data ?? []).map((meta) => (
            <button
              key={meta.node_type}
              className="workflow-palette-item"
              onClick={() => addActionNode(meta)}
              title={`${meta.inputs.map((p) => p.name).join(", ") || "no inputs"} → ${
                meta.outputs.map((p) => p.name).join(", ") || "no outputs"
              }`}
            >
              {meta.label}
            </button>
          ))}
        </aside>

        <svg
          ref={svgRef}
          className="workflow-svg"
          onMouseMove={onMouseMove}
          onMouseUp={() => {
            dragRef.current = null;
          }}
          onMouseLeave={() => {
            dragRef.current = null;
          }}
          onClick={() => {
            if (pending) setPending(null);
          }}
        >
          {edges.map((edge) => {
            const from = nodes.find((n) => n.node_id === edge.from_node);
            const to = nodes.find((n) => n.node_id === edge.to_node);
            if (!from || !to) return null;
            const fromMeta = from.node_type ? catalog[from.node_type] : undefined;
            const toMeta = to.node_type ? catalog[to.node_type] : undefined;
            const fromPorts = from.kind === "input" ? ["object"] : (fromMeta?.outputs ?? []).map((p) => p.name);
            const toPorts = (toMeta?.inputs ?? []).map((p) => p.name);
            const a = nodePortPosition(
              from.position ?? { x: 0, y: 0 },
              Math.max(fromPorts.indexOf(edge.from_port), 0),
              Math.max(fromPorts.length, 1),
              "output",
            );
            const b = nodePortPosition(
              to.position ?? { x: 0, y: 0 },
              Math.max(toPorts.indexOf(edge.to_port), 0),
              Math.max(toPorts.length, 1),
              "input",
            );
            const mid = (a.x + b.x) / 2;
            return (
              <path
                key={edgeKey(edge)}
                className="workflow-edge"
                d={`M ${a.x} ${a.y} C ${mid} ${a.y}, ${mid} ${b.y}, ${b.x} ${b.y}`}
                onClick={(e) => {
                  e.stopPropagation();
                  setEdges((current) =>
                    current.filter((x) => edgeKey(x) !== edgeKey(edge)),
                  );
                }}
              />
            );
          })}

          {pending && cursor && (
            <path
              className="workflow-edge pending"
              d={`M ${pending.x} ${pending.y} L ${cursor.x} ${cursor.y}`}
            />
          )}

          {nodes.map((node) => {
            const meta = node.node_type ? catalog[node.node_type] : undefined;
            const position = node.position ?? { x: 0, y: 0 };
            const inputs = node.kind === "input" ? [] : (meta?.inputs ?? []);
            const outputs =
              node.kind === "input"
                ? [{ name: "object", type: node.accepts!, required: true }]
                : (meta?.outputs ?? []);
            const height = node.kind === "input" ? 54 : nodeHeight(meta);
            const problems = errorsByNode.get(node.node_id);
            return (
              <g key={node.node_id}>
                <rect
                  className={[
                    "workflow-node",
                    node.kind === "input" ? "input" : "",
                    selected === node.node_id ? "selected" : "",
                    problems ? "invalid" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  x={position.x}
                  y={position.y}
                  width={NODE_WIDTH}
                  height={height}
                  rx={6}
                  onMouseDown={(e) => {
                    const point = toCanvas(e);
                    dragRef.current = {
                      node_id: node.node_id,
                      dx: point.x - position.x,
                      dy: point.y - position.y,
                    };
                    setSelected(node.node_id);
                  }}
                >
                  {problems && <title>{problems.join("\n")}</title>}
                </rect>
                <text className="workflow-node-label" x={position.x + 10} y={position.y + 20}>
                  {node.kind === "input" ? (node.label ?? "input") : (meta?.label ?? node.node_type)}
                </text>
                {selected === node.node_id && (
                  <text
                    className="workflow-node-delete"
                    x={position.x + NODE_WIDTH - 14}
                    y={position.y + 20}
                    onClick={(e) => {
                      e.stopPropagation();
                      removeNode(node.node_id);
                    }}
                  >
                    ×
                  </text>
                )}

                {inputs.map((port, i) => {
                  const p = nodePortPosition(position, i, inputs.length, "input");
                  return (
                    <g key={`in-${port.name}`}>
                      <circle
                        className={`workflow-port${port.required ? " required" : ""}`}
                        cx={p.x}
                        cy={p.y}
                        r={PORT_RADIUS}
                        onClick={(e) => {
                          e.stopPropagation();
                          finishWire(node.node_id, port.name);
                        }}
                      >
                        <title>{`${port.name}: ${port.type.format}${
                          port.type.role ? `/${port.type.role}` : ""
                        }${port.required ? " (required)" : ""}`}</title>
                      </circle>
                      <text className="workflow-port-label" x={p.x + 10} y={p.y + 4}>
                        {port.name}
                      </text>
                    </g>
                  );
                })}

                {outputs.map((port, i) => {
                  const p = nodePortPosition(position, i, outputs.length, "output");
                  return (
                    <g key={`out-${port.name}`}>
                      <circle
                        className="workflow-port out"
                        cx={p.x}
                        cy={p.y}
                        r={PORT_RADIUS}
                        onClick={(e) => {
                          e.stopPropagation();
                          startWire(node.node_id, port.name, p.x, p.y);
                        }}
                      >
                        <title>{`${port.name}: ${port.type?.format ?? "any"}`}</title>
                      </circle>
                      <text
                        className="workflow-port-label out"
                        x={p.x - 10}
                        y={p.y + 4}
                        textAnchor="end"
                      >
                        {port.name}
                      </text>
                    </g>
                  );
                })}
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
