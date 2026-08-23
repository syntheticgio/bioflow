import type { DeletionPreview } from "../api/types";
import { formatBytes } from "./format";

/** "3 sub-projects, 47 files (2.1 GB), and 12 pipeline runs".
 *
 *  Zero-valued clauses are dropped, so an empty project produces an empty
 *  string and the caller falls back to the bare "Delete X?" wording.
 *
 *  Shared by both delete entry points -- the detail panel's danger zone and
 *  the project list's row action. They must agree about what a delete is
 *  about to destroy: two confirmations that count differently are worse than
 *  one, because the quieter number is the one a user trusts.
 */
export function describeContents(p: DeletionPreview): string {
  const parts: string[] = [];
  if (p.child_project_count > 0) {
    parts.push(
      `${p.child_project_count} sub-project${p.child_project_count === 1 ? "" : "s"}`,
    );
  }
  if (p.object_count > 0) {
    parts.push(
      `${p.object_count} file${p.object_count === 1 ? "" : "s"} (${formatBytes(p.total_bytes)})`,
    );
  }
  if (p.run_count > 0) {
    parts.push(`${p.run_count} pipeline run${p.run_count === 1 ? "" : "s"}`);
  }
  if (parts.length === 0) return "";
  if (parts.length === 1) return parts[0];
  return `${parts.slice(0, -1).join(", ")}, and ${parts[parts.length - 1]}`;
}
