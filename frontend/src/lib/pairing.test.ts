import { describe, expect, it } from "vitest";
import { supersededBySelection } from "./pairing";
import type { DataObject } from "../api/types";

/** A ready FASTQ. Only the fields supersededBySelection reads are meaningful;
 *  the rest satisfy the type. */
function fastq(
  id: string,
  derived_from: string[] = [],
  role: string | null = null,
): DataObject {
  return {
    id,
    project_id: "p",
    name: `${id}.fastq`,
    size: 1000,
    status: "ready",
    blob_sha256: "abc",
    format: { kind: "fastq" },
    facts: {},
    metadata: {},
    tags: [],
    role,
    derived_from,
    produced_by_job: null,
    mate_object_id: null,
    sidecar_of: null,
    sidecar_role: null,
  } as unknown as DataObject;
}

describe("supersededBySelection", () => {
  it("names the raw parent of a selected trimmed file", () => {
    const raw = fastq("raw1");
    const trimmed = fastq("trim1", ["raw1"], "trimmed_reads");
    expect(supersededBySelection(["trim1"], [raw, trimmed])).toEqual(
      new Set(["raw1"]),
    );
  });

  it("names both raw mates when a paired trim output is selected", () => {
    // A paired trim job records both raw mates on each trimmed output.
    const objects = [
      fastq("raw1"),
      fastq("raw2"),
      fastq("trim1", ["raw1", "raw2"], "trimmed_reads"),
    ];
    expect(supersededBySelection(["trim1"], objects)).toEqual(
      new Set(["raw1", "raw2"]),
    );
  });

  it("leaves a raw file alone when its trimmed version is not selected", () => {
    // The trimmed child exists in the project but is not in this launch, so
    // hiding the raw would have no visible cause in the dialog.
    const raw = fastq("raw1");
    const trimmed = fastq("trim1", ["raw1"], "trimmed_reads");
    expect(supersededBySelection([], [raw, trimmed])).toEqual(new Set());
  });

  it("is empty when nothing selected was derived from anything", () => {
    const objects = [fastq("raw1"), fastq("raw2")];
    expect(supersededBySelection(["raw1"], objects)).toEqual(new Set());
  });

  it("ignores selected ids that name no known object", () => {
    expect(supersededBySelection(["gone"], [fastq("raw1")])).toEqual(new Set());
  });
});
