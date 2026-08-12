import { describe, expect, it } from "vitest";
import type { RunKind, RunSummary } from "../api/types";
import { KIND_ACTIONS, kindAction, runFacts } from "./runFormat";

/**
 * The action phrase that heads a run's card, and the facts under it.
 *
 * `KIND_ACTIONS` is the enum-keyed registry shape CLAUDE.md warns about: a
 * kind with no entry renders a card with no action line, and nothing fails.
 * TypeScript's `Record<RunKind, string>` catches a missing key at build time,
 * but only for kinds this file knows about -- so the runtime check below is
 * against the same list the API type declares.
 */

// Every member of the RunKind union, written out. This is the part a new kind
// has to be added to.
//
// The annotation is what makes it self-checking, in both directions: typed as
// `readonly RunKind[]`, a member that is not a RunKind fails to compile, and
// the `Exclude` below fails if RunKind gains a member missing from here. A
// plain `as const` array would silently drift instead.
const ALL_KINDS = [
  "alignment",
  "trim",
  "sra_download",
  "variant_calling",
  "assembly_download",
  "uniprot_download",
  "quantify",
  "differential_expression",
  "assembly",
  "reference_assembly",
] as const satisfies readonly RunKind[];

// Resolves to `never` -- and so fails to compile at its use below -- if any
// RunKind is missing from ALL_KINDS.
type MissingKind = Exclude<RunKind, (typeof ALL_KINDS)[number]>;

function run(over: Partial<RunSummary> = {}): RunSummary {
  return {
    id: "r1",
    kind: "alignment",
    project_id: "p1",
    label: "reads → ref",
    status: "running",
    inputs: [],
    params: {},
    tool: null,
    outputs: [],
    created_at: "2026-08-09T00:00:00Z",
    updated_at: "2026-08-09T00:00:00Z",
    ...over,
  };
}

describe("kindAction", () => {
  it("gives every run kind an action phrase", () => {
    // Compile-time half: `never` here means ALL_KINDS covers RunKind. If a
    // kind is added to the API type and not to this file, this line stops
    // compiling -- which is the failure a runtime loop over a stale list
    // cannot produce.
    const unmapped: MissingKind[] = [];
    expect(unmapped).toEqual([]);

    for (const kind of ALL_KINDS) {
      const phrase = kindAction(kind);
      expect(phrase, `no action phrase for ${kind}`).toBeTruthy();
    }
  });

  it("says what the run does, not what its inputs are called", () => {
    // The reported problem: the stored label names the operands and never the
    // verb, so a card said "DRR1066343 (paired) → GCF_...fna" with no
    // indication that this was an alignment rather than anything else.
    expect(kindAction("alignment")).toMatch(/align/i);
    expect(kindAction("quantify")).toMatch(/count/i);
    expect(kindAction("differential_expression")).toMatch(/differential/i);
  });

  it("is a phrase rather than an echo of the enum token", () => {
    for (const kind of ALL_KINDS) {
      expect(kindAction(kind)).not.toBe(kind);
      expect(kindAction(kind)).not.toMatch(/_/);
    }
  });

  it("returns undefined for a kind this build has never heard of", () => {
    // An older frontend against a newer backend. Dropping the line beats
    // rendering a raw machine token where a sentence goes.
    expect(kindAction("something_new" as RunKind)).toBeUndefined();
  });

  it("has no entry that is not a real run kind", () => {
    expect(Object.keys(KIND_ACTIONS).sort()).toEqual([...ALL_KINDS].sort());
  });
});

describe("runFacts", () => {
  it("names the tool the same way whether it came from params or the run", () => {
    // These never appear on one card -- alignment carries its tool in params,
    // trim in the run's own field -- so two different labels for one concept
    // read as two different facts depending on which card you opened.
    const aligned = runFacts(run({ params: { aligner: "bowtie2" } }));
    const trimmed = runFacts(run({ kind: "trim", tool: "fastp" }));

    expect(aligned.find((f) => f.v === "bowtie2")?.k).toBe("Tool");
    expect(trimmed.find((f) => f.v === "fastp")?.k).toBe("Tool");
  });

  it("lists additional read sets under their own row", () => {
    const aligned = runFacts(
      run({
        inputs: [
          { object_id: "a", name: "a_R1.fastq", role: "reads" },
          { object_id: "b", name: "b_R1.fastq", role: "extra_reads" },
          { object_id: "c", name: "c_R2.fastq", role: "extra_mate" },
          { object_id: "ref", name: "ref.fna", role: "reference" },
        ],
      }),
    );

    expect(aligned.find((f) => f.k === "Reads")?.v).toBe("a_R1.fastq");
    expect(aligned.find((f) => f.k === "Additional reads")?.v).toBe(
      "b_R1.fastq + c_R2.fastq",
    );
  });
});
