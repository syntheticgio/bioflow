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
