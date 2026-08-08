/**
 * The canvas's graph rules: what may be wired to what, and where ports sit.
 *
 * Pure, and the only part of the editor with tests -- this repo has no
 * component-testing setup (no jsdom, no testing-library, zero `.test.tsx`), so
 * anything that must be verified automatically has to live here rather than
 * inside a component. The drawing and dragging are checked in the browser.
 *
 * These rules deliberately mirror `workflow_service.validate_definition` rather
 * than replacing it: the server validates every save regardless. What this buys
 * is telling the user *before* they save, which is the difference between a
 * canvas you can build in and one you fix by trial and error.
 */

import type {
  NodePosition,
  NodeTypeMeta,
  PortType,
  WorkflowEdge,
  WorkflowNode,
} from "../api/types";

/** Node box geometry, shared by the renderer and the port maths so a wire
 *  always lands on the dot it is drawn to. */
export const NODE_WIDTH = 180;
export const NODE_HEADER = 30;
export const PORT_SPACING = 22;
export const PORT_RADIUS = 5;

export interface ConnectResult {
  ok: boolean;
  reason?: string;
}

/** Whether an object of this format/role may enter a port of this type.
 *  The mirror of `PortType.accepts` on the backend: a required role is not
 *  satisfied by an absent one, which is what stops a protein FASTA reaching an
 *  aligner's reference port. */
export function portAccepts(port: PortType, produced: PortType): boolean {
  if (port.format !== produced.format) return false;
  if (port.role === null || port.role === undefined) return true;
  return port.role === produced.role;
}

function outputType(
  node: WorkflowNode,
  portName: string,
  catalog: Record<string, NodeTypeMeta>,
): PortType | null {
  if (node.kind === "input") {
    // An INPUT has exactly one output, `object`, carrying whatever it accepts.
    return portName === "object" ? (node.accepts ?? null) : null;
  }
  const meta = node.node_type ? catalog[node.node_type] : undefined;
  return meta?.outputs.find((p) => p.name === portName)?.type ?? null;
}

function inputPort(
  node: WorkflowNode,
  portName: string,
  catalog: Record<string, NodeTypeMeta>,
): PortType | null {
  if (node.kind === "input") return null; // nothing flows into a slot
  const meta = node.node_type ? catalog[node.node_type] : undefined;
  return meta?.inputs.find((p) => p.name === portName)?.type ?? null;
}

/** Whether adding `from -> to` closes a loop, by asking if `from` is already
 *  reachable from `to`. Iterative rather than recursive so a deep graph reports
 *  a cycle instead of exhausting the stack. */
export function wouldCycle(
  edges: WorkflowEdge[],
  fromNode: string,
  toNode: string,
): boolean {
  if (fromNode === toNode) return true;
  const adjacency = new Map<string, string[]>();
  for (const e of edges) {
    const list = adjacency.get(e.from_node) ?? [];
    list.push(e.to_node);
    adjacency.set(e.from_node, list);
  }
  const stack = [toNode];
  const seen = new Set<string>([toNode]);
  while (stack.length) {
    const current = stack.pop()!;
    if (current === fromNode) return true;
    for (const next of adjacency.get(current) ?? []) {
      if (!seen.has(next)) {
        seen.add(next);
        stack.push(next);
      }
    }
  }
  return false;
}

/** Identity for one wire, so deleting targets exactly the wire clicked. */
export function edgeKey(edge: WorkflowEdge): string {
  return `${edge.from_node}:${edge.from_port}->${edge.to_node}:${edge.to_port}`;
}

export function canConnect(
  nodes: WorkflowNode[],
  edges: WorkflowEdge[],
  catalog: Record<string, NodeTypeMeta>,
  candidate: WorkflowEdge,
): ConnectResult {
  const byId = new Map(nodes.map((n) => [n.node_id, n]));
  const source = byId.get(candidate.from_node);
  const target = byId.get(candidate.to_node);
  if (!source || !target) return { ok: false, reason: "Unknown node." };

  if (candidate.from_node === candidate.to_node) {
    return { ok: false, reason: "A node cannot feed itself." };
  }

  if (target.kind === "input") {
    return { ok: false, reason: "An input node takes a file, not a wire." };
  }

  const produced = outputType(source, candidate.from_port, catalog);
  if (!produced) {
    return { ok: false, reason: `No output port ${candidate.from_port}.` };
  }

  const accepted = inputPort(target, candidate.to_port, catalog);
  if (!accepted) {
    return { ok: false, reason: `No input port ${candidate.to_port}.` };
  }

  // One wire per input port. Fan-*out* is fine and common -- a trimmed FASTQ
  // feeding both an aligner and a QC node -- but two objects arriving at one
  // port has no meaning.
  const occupied = edges.some(
    (e) => e.to_node === candidate.to_node && e.to_port === candidate.to_port,
  );
  if (occupied) {
    return { ok: false, reason: `${candidate.to_port} already has an input.` };
  }

  if (!portAccepts(accepted, produced)) {
    const role = produced.role ?? "any";
    return {
      ok: false,
      reason: `${candidate.to_port} does not accept ${produced.format}/${role}.`,
    };
  }

  if (wouldCycle(edges, candidate.from_node, candidate.to_node)) {
    return { ok: false, reason: "That would create a cycle." };
  }

  return { ok: true };
}

/** Where one port's dot sits, in canvas coordinates.
 *
 * Inputs run down the left edge and outputs down the right, evenly spaced and
 * centred as a group, so a node with one port puts it level with the node's
 * middle rather than at the top.
 */
export function nodePortPosition(
  position: NodePosition,
  index: number,
  total: number,
  side: "input" | "output",
): { x: number; y: number } {
  const x = side === "input" ? position.x : position.x + NODE_WIDTH;
  const span = (total - 1) * PORT_SPACING;
  const first = position.y + NODE_HEADER + PORT_SPACING - span / 2;
  return { x, y: first + index * PORT_SPACING };
}

/** Somewhere to drop a new node that is not on top of an existing one.
 *
 * A fixed offset per node -- the obvious first implementation -- puts the
 * second node under the first the moment anything has been dragged, and a node
 * you cannot read until you move it is worse than no placement at all. This
 * walks a grid and takes the first cell nothing occupies.
 */
export function nextFreeSlot(nodes: { position?: NodePosition }[]): NodePosition {
  const STEP_X = NODE_WIDTH + 40;
  const STEP_Y = 100;
  const taken = nodes
    .map((n) => n.position)
    .filter((p): p is NodePosition => Boolean(p));

  for (let row = 0; row < 40; row += 1) {
    for (let column = 0; column < 12; column += 1) {
      const candidate = { x: 40 + column * STEP_X, y: 50 + row * STEP_Y };
      const clash = taken.some(
        (p) =>
          Math.abs(p.x - candidate.x) < NODE_WIDTH &&
          Math.abs(p.y - candidate.y) < 80,
      );
      if (!clash) return candidate;
    }
  }
  // A canvas with 480 nodes on it has bigger problems than overlap.
  return { x: 40, y: 50 };
}

/** The height a node needs to hold its ports without them overflowing. */
export function nodeHeight(meta: NodeTypeMeta | undefined): number {
  const ports = Math.max(meta?.inputs.length ?? 1, meta?.outputs.length ?? 1, 1);
  return NODE_HEADER + PORT_SPACING * (ports + 1);
}
