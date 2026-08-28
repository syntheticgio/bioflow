import { describe, expect, it } from "vitest";

import { metadataEditorKey } from "./SchemaMetadataEditor";

/**
 * The key that makes a metadata draft per-file.
 *
 * DetailPanel renders <SchemaMetadataEditor key={metadataEditorKey(...)}>,
 * and React only remounts a component when its key changes. The editor's
 * resync effect bails while there are unsaved edits, so a key that does not
 * change between two files of the same role keeps file A's draft alive in
 * file B's form -- and saving writes file A's values onto file B (#854).
 * The role half of the key is what makes a *conversion* of the same file
 * remount, since the schema changes underneath the edits.
 */
describe("metadataEditorKey", () => {
  it("changes when the selected file changes, same role", () => {
    expect(metadataEditorKey("fileA", "trimmed_reads")).not.toBe(
      metadataEditorKey("fileB", "trimmed_reads"),
    );
  });

  it("changes when the file's role changes, same id", () => {
    expect(metadataEditorKey("fileA", "trimmed_reads")).not.toBe(
      metadataEditorKey("fileA", "alignment"),
    );
  });

  it("is stable for the same file and role", () => {
    expect(metadataEditorKey("fileA", "trimmed_reads")).toBe(
      metadataEditorKey("fileA", "trimmed_reads"),
    );
  });

  it("treats a null role as its own distinct value", () => {
    expect(metadataEditorKey("fileA", null)).not.toBe(
      metadataEditorKey("fileA", "trimmed_reads"),
    );
  });
});
