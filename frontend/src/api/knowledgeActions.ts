import { API_BASE_URL } from "./client";

export type KnowledgeRouteId = "memory-project-basis" | "memory-models" | "memory-literature";
export type KnowledgeOwner = "modeling" | "model-dossier" | "literature";

export type StableKnowledgeRef = {
  owner: KnowledgeOwner;
  stable_ref: string;
};

export type JarvisExactRef = {
  workspace_id: string;
  owner: string;
  kind: string;
  id: string;
  version?: string;
  revision?: string;
  immutable_ref?: string;
  content_digest?: string;
};

export type KnowledgeContextPreview = {
  state: "current";
  workspace_id: string;
  route_id: KnowledgeRouteId;
  exact_refs: JarvisExactRef[];
  context_digest: string;
  context_sources_manifest: Array<Record<string, unknown>>;
  included_count: number;
  estimated_token_count: number;
};

export type KnowledgeProposal = {
  state: "proposed";
  workspace_id: string;
  route_id: KnowledgeRouteId;
  intent: string;
  exact_context_refs: JarvisExactRef[];
  context_digest: string;
  context_sources_manifest: Array<Record<string, unknown>>;
  target_domain: "project_basis" | "models" | "literature";
  summary: string;
  proposed_items: string[];
  questions: string[];
  research_steps: string[];
  assumptions: string[];
  warnings: string[];
  authoritative_next_action: string | null;
};

type Refused = { state: "refused"; reason: string };

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) throw new Error(`Knowledge action failed (${response.status})`);
  return response.json() as Promise<T>;
}

export async function previewKnowledgeContext(
  workspaceId: string,
  routeId: KnowledgeRouteId,
  refs: StableKnowledgeRef[]
): Promise<KnowledgeContextPreview> {
  const result = await postJson<KnowledgeContextPreview | Refused>("/memory/jarvis/context-preview", {
    workspace_id: workspaceId,
    route_id: routeId,
    refs
  });
  if (result.state !== "current") throw new Error(`Context refused: ${result.reason}`);
  return result;
}

export async function proposeKnowledgeAction(
  preview: KnowledgeContextPreview,
  intent: string
): Promise<KnowledgeProposal> {
  const result = await postJson<KnowledgeProposal | Refused>("/memory/jarvis/propose", {
    workspace_id: preview.workspace_id,
    route_id: preview.route_id,
    intent,
    exact_refs: preview.exact_refs,
    expected_context_digest: preview.context_digest
  });
  if (result.state !== "proposed") throw new Error(`Proposal refused: ${result.reason}`);
  return result;
}
