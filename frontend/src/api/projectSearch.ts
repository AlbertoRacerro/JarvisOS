import { API_BASE_URL } from "./client";

export type ProjectSearchKind =
  | "requirement"
  | "parameter"
  | "assumption"
  | "decision"
  | "model"
  | "literature_source"
  | "literature_entry";

export type ProjectSearchResult = {
  kind: ProjectSearchKind;
  owner: "modeling" | "model-dossier" | "literature";
  stable_ref: string;
  workspace_id: string;
  title: string;
  summary: string | null;
  lifecycle_or_status: string | null;
  version_or_revision: string | null;
  provenance_refs: string[];
  source_refs: string[];
  route: "/memory/project-basis" | "/memory/models" | "/memory/literature";
  route_params: Record<string, string>;
  match_fields: string[];
  match_tier: "exact" | "prefix" | "contains" | "normalized" | "tokens";
};

export type ProjectSearchResponse = {
  query: string;
  items: ProjectSearchResult[];
  total_returned: number;
  truncated: boolean;
};

export async function projectSearch(workspaceId: string, query: string, signal?: AbortSignal): Promise<ProjectSearchResponse> {
  const params = new URLSearchParams({ q: query });
  const response = await fetch(
    `${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/project-search?${params.toString()}`,
    { signal }
  );
  if (!response.ok) throw new Error(`Project search failed with ${response.status}`);
  return response.json() as Promise<ProjectSearchResponse>;
}
