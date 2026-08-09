import { describe, expect, it } from "vitest";
import {
  NODE_WIDTH,
  canConnect,
  edgeKey,
  edgesInvalidatedBy,
  bindableObjects,
  nextFreeSlot,
  nodePortPosition,
  portsFor,
  wouldCycle,
} from "./workflowGraph";
import type {
  DataObject,
  NodeTypeMeta,
  WorkflowEdge,
  WorkflowNode,
} from "../api/types";

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
    { name: "reads", type: { format: "fastq", role: null }, required: true, multiple: true },
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
  tool_choice: {
    param_key: "aligner",
    options: [
      { value: "minimap2", label: "minimap2" },
      { value: "star", label: "STAR" },
    ],
    default: "minimap2",
  },
  ports_by_tool: {
    minimap2: {
      inputs: [
        { name: "reads", type: { format: "fastq", role: null }, required: true, multiple: true },
        { name: "mate", type: { format: "fastq", role: null }, required: false },
        { name: "reference", type: { format: "fasta", role: "reference" }, required: true },
      ],
      outputs: [{ name: "alignment", type: { format: "bam", role: "alignment" }, required: true }],
    },
    star: {
      inputs: [
        { name: "reads", type: { format: "fastq", role: null }, required: true, multiple: true },
        { name: "mate", type: { format: "fastq", role: null }, required: false },
        { name: "reference", type: { format: "fasta", role: "reference" }, required: true },
        { name: "annotation", type: { format: "gtf", role: null }, required: false },
      ],
      outputs: [{ name: "alignment", type: { format: "bam", role: "alignment" }, required: true }],
    },
  },
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
    // "reads" on trim is a scalar port (align's is multi -- see the
    // "canConnect with multi ports" tests below for that case).
    const nodes = [input("r1", "fastq"), input("r2", "fastq"), action("t", "trim")];
    const existing = [edge("r1", "object", "t", "reads")];
    const result = canConnect(nodes, existing, CATALOG, {
      from_node: "r2",
      from_port: "object",
      to_node: "t",
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

describe("bindableObjects", () => {
  function obj(
    id: string,
    format: string,
    role: string | null = null,
    extra: Partial<DataObject> = {},
  ): DataObject {
    return {
      id,
      name: `${id}.file`,
      format: { kind: format } as DataObject["format"],
      role: role as DataObject["role"],
      status: "ready",
      sidecar_of: null,
      ...extra,
    } as DataObject;
  }

  const port = { format: "fastq", role: null };

  it("keeps a file whose format the port accepts", () => {
    const kept = bindableObjects([obj("a", "fastq")], port);
    expect(kept.map((o) => o.id)).toEqual(["a"]);
  });

  it("drops a file of the wrong format", () => {
    expect(bindableObjects([obj("a", "bam", "alignment")], port)).toEqual([]);
  });

  it("enforces the role a port requires", () => {
    // The protein.faa trap again, this time in the binding dialog: offering it
    // for a reference port is how a user picks it by mistake.
    const candidates = [obj("prot", "fasta", "protein"), obj("ref", "fasta", "reference")];
    const kept = bindableObjects(candidates, { format: "fasta", role: "reference" });
    expect(kept.map((o) => o.id)).toEqual(["ref"]);
  });

  it("hides sidecars", () => {
    // A .fai or .mmi is biologically inert and never something a user binds.
    const candidates = [obj("real", "fastq"), obj("side", "fastq", null, { sidecar_of: "real" })];
    expect(bindableObjects(candidates, port).map((o) => o.id)).toEqual(["real"]);
  });

  it("hides files that are not ready", () => {
    // Binding a still-uploading file would launch a pipeline against a partial
    // one, which fails deep inside a tool rather than at the point of choice.
    const candidates = [
      obj("done", "fastq"),
      obj("busy", "fastq", null, { status: "uploading" as DataObject["status"] }),
    ];
    expect(bindableObjects(candidates, port).map((o) => o.id)).toEqual(["done"]);
  });

  it("offers everything of the format when the port names no role", () => {
    const candidates = [obj("plain", "fastq"), obj("trimmed", "fastq", "trimmed_reads")];
    expect(bindableObjects(candidates, port)).toHaveLength(2);
  });
});

describe("portsFor", () => {
  it("returns the default set for a node with no tool chosen", () => {
    const node = action("align_1", "align");
    const { inputs } = portsFor(node, CATALOG);
    expect(inputs.map((p) => p.name)).toContain("reads");
  });

  it("returns the tool's set when one is chosen", () => {
    const node = { ...action("align_1", "align"), params: { aligner: "star" } };
    const { inputs } = portsFor(node, CATALOG);
    expect(inputs.map((p) => p.name)).toContain("annotation");
  });

  it("falls back to the default for an unknown tool", () => {
    const node = { ...action("align_1", "align"), params: { aligner: "nope" } };
    const { inputs } = portsFor(node, CATALOG);
    expect(inputs.map((p) => p.name)).toContain("reads");
    expect(inputs.map((p) => p.name)).not.toContain("annotation");
  });

  it("returns the static set for a node type with no tool choice", () => {
    const node = action("t", "trim");
    const { inputs } = portsFor(node, CATALOG);
    expect(inputs.map((p) => p.name)).toEqual(["reads", "mate"]);
  });
});

describe("canConnect with multi ports", () => {
  it("accepts a second wire into a multi port", () => {
    const nodes = [input("r1", "fastq"), input("r2", "fastq"), action("align_1", "align")];
    const edges = [edge("r1", "object", "align_1", "reads")];
    const verdict = canConnect(nodes, edges, CATALOG, edge("r2", "object", "align_1", "reads"));
    expect(verdict.ok).toBe(true);
  });

  it("still refuses a second wire into a scalar port", () => {
    const nodes = [
      input("ref_a", "fasta", "reference"),
      input("ref_b", "fasta", "reference"),
      action("align_1", "align"),
    ];
    const edges = [edge("ref_a", "object", "align_1", "reference")];
    const verdict = canConnect(nodes, edges, CATALOG, edge("ref_b", "object", "align_1", "reference"));
    expect(verdict.ok).toBe(false);
    expect(verdict.reason).toMatch(/already has an input/);
  });

  it("type-checks every wire of a multi port", () => {
    const nodes = [input("r1", "fastq"), input("bam", "bam", "alignment"), action("align_1", "align")];
    const edges = [edge("r1", "object", "align_1", "reads")];
    const verdict = canConnect(nodes, edges, CATALOG, edge("bam", "object", "align_1", "reads"));
    expect(verdict.ok).toBe(false);
    expect(verdict.reason).toMatch(/does not accept/);
  });

  it("refuses a multi slot feeding a scalar port", () => {
    const slot = { ...input("many", "fastq"), multiple: true };
    const nodes = [slot, action("t", "trim")];
    const verdict = canConnect(nodes, [], CATALOG, edge("many", "object", "t", "reads"));
    expect(verdict.ok).toBe(false);
    expect(verdict.reason).toMatch(/one file/);
  });

  it("allows a multi slot feeding a multi port", () => {
    const slot = { ...input("many", "fastq"), multiple: true };
    const nodes = [slot, action("align_1", "align")];
    const verdict = canConnect(nodes, [], CATALOG, edge("many", "object", "align_1", "reads"));
    expect(verdict.ok).toBe(true);
  });
});

describe("edgesInvalidatedBy", () => {
  it("drops a wire into a port the new tool does not have", () => {
    const node = { ...action("align_1", "align"), params: { aligner: "star" } };
    const nodes = [input("gtf", "gtf"), node];
    const edges = [edge("gtf", "object", "align_1", "annotation")];
    const dropped = edgesInvalidatedBy(nodes, edges, CATALOG, "align_1", "minimap2");
    expect(dropped.map((e) => e.to_port)).toEqual(["annotation"]);
  });

  it("keeps wires into ports both tools share", () => {
    const node = { ...action("align_1", "align"), params: { aligner: "star" } };
    const nodes = [input("r1", "fastq"), node];
    const edges = [edge("r1", "object", "align_1", "reads")];
    const dropped = edgesInvalidatedBy(nodes, edges, CATALOG, "align_1", "minimap2");
    expect(dropped).toEqual([]);
  });

  it("leaves other nodes' wires alone", () => {
    const align = { ...action("align_1", "align"), params: { aligner: "star" } };
    const nodes = [input("r1", "fastq"), align, action("t", "trim")];
    const edges = [edge("r1", "object", "t", "reads")];
    const dropped = edgesInvalidatedBy(nodes, edges, CATALOG, "align_1", "minimap2");
    expect(dropped).toEqual([]);
  });
});
