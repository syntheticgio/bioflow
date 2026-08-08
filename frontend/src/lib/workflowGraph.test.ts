import { describe, expect, it } from "vitest";
import {
  NODE_WIDTH,
  canConnect,
  edgeKey,
  nextFreeSlot,
  nodePortPosition,
  wouldCycle,
} from "./workflowGraph";
import type { NodeTypeMeta, WorkflowEdge, WorkflowNode } from "../api/types";

// The two node types these tests wire together, shaped exactly as
// /workflows/node-types serves them. Hand-writing them here rather than
// importing the registry is deliberate: this file is testing the *client's*
// rules, and a fixture that drifts from the server is a failure worth seeing
// in the canvas rather than one hidden by sharing a definition.
const TRIM: NodeTypeMeta = {
  node_type: "trim",
  label: "Trim reads",
  inputs: [
    { name: "reads", type: { format: "fastq", role: null }, required: true },
    { name: "mate", type: { format: "fastq", role: null }, required: false },
  ],
  outputs: [
    {
      name: "trimmed",
      type: { format: "fastq", role: "trimmed_reads" },
      required: true,
    },
  ],
};

const ALIGN: NodeTypeMeta = {
  node_type: "align",
  label: "Align to reference",
  inputs: [
    { name: "reads", type: { format: "fastq", role: null }, required: true },
    { name: "mate", type: { format: "fastq", role: null }, required: false },
    {
      name: "reference",
      type: { format: "fasta", role: "reference" },
      required: true,
    },
  ],
  outputs: [
    {
      name: "alignment",
      type: { format: "bam", role: "alignment" },
      required: true,
    },
  ],
};

const CATALOG: Record<string, NodeTypeMeta> = { trim: TRIM, align: ALIGN };

function action(node_id: string, node_type: string): WorkflowNode {
  return { node_id, kind: "action", node_type, params: {}, continue_on_failure: false };
}

function input(node_id: string, format: string, role: string | null = null): WorkflowNode {
  return {
    node_id,
    kind: "input",
    label: node_id,
    accepts: { format, role },
    params: {},
    continue_on_failure: false,
  };
}

function edge(from_node: string, from_port: string, to_node: string, to_port: string): WorkflowEdge {
  return { from_node, from_port, to_node, to_port };
}

describe("canConnect", () => {
  it("accepts a wire whose types line up", () => {
    const nodes = [action("t", "trim"), action("a", "align")];
    const result = canConnect(nodes, [], CATALOG, {
      from_node: "t",
      from_port: "trimmed",
      to_node: "a",
      to_port: "reads",
    });
    expect(result.ok).toBe(true);
  });

  it("rejects a format mismatch", () => {
    // An alignment's BAM cannot feed a trim's FASTQ port.
    const nodes = [action("a", "align"), action("t", "trim")];
    const result = canConnect(nodes, [], CATALOG, {
      from_node: "a",
      from_port: "alignment",
      to_node: "t",
      to_port: "reads",
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/does not accept/i);
  });

  it("rejects a FASTA with no role reaching a reference port", () => {
    // The protein.faa case: format matches, role does not. This is the whole
    // reason PortType carries a role, and the canvas has to enforce it live or
    // the user only finds out on save.
    const nodes = [input("f", "fasta", null), action("a", "align")];
    const result = canConnect(nodes, [], CATALOG, {
      from_node: "f",
      from_port: "object",
      to_node: "a",
      to_port: "reference",
    });
    expect(result.ok).toBe(false);
  });

  it("accepts a correctly-roled reference", () => {
    const nodes = [input("f", "fasta", "reference"), action("a", "align")];
    const result = canConnect(nodes, [], CATALOG, {
      from_node: "f",
      from_port: "object",
      to_node: "a",
      to_port: "reference",
    });
    expect(result.ok).toBe(true);
  });

  it("accepts any role when the port asks for none", () => {
    // `role: null` means "any role for this format" -- the honest type for a
    // port that genuinely does not care.
    const nodes = [input("f", "fastq", "trimmed_reads"), action("t", "trim")];
    const result = canConnect(nodes, [], CATALOG, {
      from_node: "f",
      from_port: "object",
      to_node: "t",
      to_port: "reads",
    });
    expect(result.ok).toBe(true);
  });

  it("rejects a second wire into an occupied port", () => {
    const nodes = [action("t", "trim"), action("t2", "trim"), action("a", "align")];
    const existing = [edge("t", "trimmed", "a", "reads")];
    const result = canConnect(nodes, existing, CATALOG, {
      from_node: "t2",
      from_port: "trimmed",
      to_node: "a",
      to_port: "reads",
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/already/i);
  });

  it("allows one output to feed several ports", () => {
    // Fan-out is normal: one trimmed FASTQ into both an aligner and a QC node.
    const nodes = [action("t", "trim"), action("a", "align"), action("a2", "align")];
    const existing = [edge("t", "trimmed", "a", "reads")];
    const result = canConnect(nodes, existing, CATALOG, {
      from_node: "t",
      from_port: "trimmed",
      to_node: "a2",
      to_port: "reads",
    });
    expect(result.ok).toBe(true);
  });

  it("rejects a self-wire", () => {
    const nodes = [action("t", "trim")];
    const result = canConnect(nodes, [], CATALOG, {
      from_node: "t",
      from_port: "trimmed",
      to_node: "t",
      to_port: "reads",
    });
    expect(result.ok).toBe(false);
  });

  it("rejects a wire that would close a cycle", () => {
    const nodes = [action("t", "trim"), action("t2", "trim")];
    const existing = [edge("t", "trimmed", "t2", "reads")];
    const result = canConnect(nodes, existing, CATALOG, {
      from_node: "t2",
      from_port: "trimmed",
      to_node: "t",
      to_port: "reads",
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/cycle/i);
  });

  it("rejects wiring into an input node", () => {
    // An INPUT is a slot the user binds a file to; nothing flows into it.
    const nodes = [action("t", "trim"), input("f", "fastq")];
    const result = canConnect(nodes, [], CATALOG, {
      from_node: "t",
      from_port: "trimmed",
      to_node: "f",
      to_port: "object",
    });
    expect(result.ok).toBe(false);
  });

  it("reports an unknown port rather than silently allowing it", () => {
    const nodes = [action("t", "trim"), action("a", "align")];
    const result = canConnect(nodes, [], CATALOG, {
      from_node: "t",
      from_port: "nope",
      to_node: "a",
      to_port: "reads",
    });
    expect(result.ok).toBe(false);
  });
});

describe("wouldCycle", () => {
  it("sees a direct back-edge", () => {
    expect(wouldCycle([edge("a", "o", "b", "i")], "b", "a")).toBe(true);
  });

  it("sees a longer loop", () => {
    const edges = [edge("a", "o", "b", "i"), edge("b", "o", "c", "i")];
    expect(wouldCycle(edges, "c", "a")).toBe(true);
  });

  it("allows a diamond, which is not a cycle", () => {
    // a -> b, a -> c, and now b -> d, c -> d. Two paths to one node is a DAG.
    const edges = [edge("a", "o", "b", "i"), edge("a", "o", "c", "i"), edge("b", "o", "d", "i")];
    expect(wouldCycle(edges, "c", "d")).toBe(false);
  });

  it("allows an unrelated wire", () => {
    expect(wouldCycle([edge("a", "o", "b", "i")], "c", "d")).toBe(false);
  });
});

describe("edgeKey", () => {
  it("identifies an edge by its endpoints, so deletion can target one wire", () => {
    const e = edge("a", "out", "b", "in");
    expect(edgeKey(e)).toBe(edgeKey({ ...e }));
    expect(edgeKey(e)).not.toBe(edgeKey(edge("a", "out", "b", "other")));
  });
});

describe("nodePortPosition", () => {
  const node = { node_id: "n", position: { x: 100, y: 50 } };

  it("puts inputs on the left edge and outputs on the right", () => {
    const left = nodePortPosition(node.position, 0, 2, "input");
    const right = nodePortPosition(node.position, 0, 2, "output");
    expect(left.x).toBeLessThan(right.x);
  });

  it("spreads several ports down the node rather than stacking them", () => {
    const first = nodePortPosition(node.position, 0, 3, "input");
    const second = nodePortPosition(node.position, 1, 3, "input");
    expect(second.y).toBeGreaterThan(first.y);
  });

  it("centres a lone port", () => {
    const only = nodePortPosition(node.position, 0, 1, "input");
    const [a, b] = [
      nodePortPosition(node.position, 0, 2, "input"),
      nodePortPosition(node.position, 1, 2, "input"),
    ];
    expect(only.y).toBeCloseTo((a.y + b.y) / 2, 5);
  });
});

describe("nextFreeSlot", () => {
  it("puts the first node at the origin corner", () => {
    const spot = nextFreeSlot([]);
    expect(spot.x).toBeGreaterThan(0);
    expect(spot.y).toBeGreaterThan(0);
  });

  it("does not land a new node on top of an existing one", () => {
    // The bug this exists for: every node spawning at a fixed offset means the
    // second one lands under the first and has to be dragged out before it can
    // even be read.
    const existing = [{ ...action("a", "trim"), position: { x: 60, y: 60 } }];
    const spot = nextFreeSlot(existing);
    const overlaps =
      Math.abs(spot.x - 60) < NODE_WIDTH && Math.abs(spot.y - 60) < 80;
    expect(overlaps).toBe(false);
  });

  it("keeps finding room as the canvas fills", () => {
    const nodes: WorkflowNode[] = [];
    for (let i = 0; i < 8; i += 1) {
      const spot = nextFreeSlot(nodes);
      for (const other of nodes) {
        const p = other.position!;
        const clash =
          Math.abs(spot.x - p.x) < NODE_WIDTH && Math.abs(spot.y - p.y) < 80;
        expect(clash).toBe(false);
      }
      nodes.push({ ...action(`n${i}`, "trim"), position: spot });
    }
  });
});
