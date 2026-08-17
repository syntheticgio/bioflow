export interface Project {
  id: string;
  name: string;
  slug: string;
  description: string;
  agent_system_prompt: string;
  parent_id: string | null;
  metadata: Record<string, unknown>;
  tags: string[];
  object_count: number;
  total_bytes: number;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface Breadcrumb {
  id: string;
  name: string;
}

export interface ProjectDetail extends Project {
  breadcrumbs: Breadcrumb[];
}

/**
 * One archive on disk under the exports directory, as `GET /exports` lists
 * it. `created_at` is a Unix timestamp in seconds (`Path.stat().st_mtime`
 * on the backend), not an ISO string like every other timestamp this app
 * renders -- convert with `new Date(created_at * 1000)` rather than handing
 * it to `formatDate`, which expects ISO.
 */
export interface ExportArchive {
  name: string;
  size_bytes: number;
  created_at: number;
}
