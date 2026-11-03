import { API_BASE_URL } from "./client";

export type SurfaceRef = {
  route_id: string;
  draft_id?: string | null;
  process_selection?: Array<{ kind: "unit" | "stream"; id?: string | null; tag?: string | null }>;
  candidate_id?: string | null;
  bluecad_part_ids?: string[];
  studio_model_id?: string | null;
};

export type SurfaceBrief = {
  surface: "process" | "bluecad" | "studio" | "none";
  route_id: string;
  workspace_id: string;
  base_revision: string | null;
  draft_id: string | null;
  candidate_id: string | null;
  selected: Array<Record<string, unknown>>;
  summary: string;
  text: string;
  actions: string[];
  limits: string[];
  digest: string;
};

export type ActionOrigin = { kind: "local" | "relay"; thread_id: string; interaction_id?: string | null; relay_run_id?: string | null; model?: string | null };
export type ChangeLine = { label: string; before: string | null; after: string | null };
export type ActionOutcome = {
  action_id: string;
  workspace_id: string;
  surface: "process" | "bluecad" | "studio";
  state: "applied" | "proposed" | "refused" | "stale" | "dismissed" | "undone";
  tier: "immediate" | "confirm" | "none";
  summary: string;
  changes: ChangeLine[];
  base_revision: string;
  result_revision: string | null;
  draft_id: string | null;
  candidate_id: string | null;
  child_candidate_id: string | null;
  reason_code: string | null;
  reason: string | null;
  origin: ActionOrigin;
  request_digest: string;
  request: Record<string, unknown>;
  undo_available: boolean;
  created_at: string;
  updated_at: string;
};

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) }
  });
  if (!response.ok) {
    let message = `Workspace action request failed (${response.status})`;
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
      else if (body.detail && typeof body.detail === "object" && "message" in body.detail && typeof body.detail.message === "string") message = body.detail.message;
    } catch { /* Use the status message when the server has no structured detail. */ }
    throw new WorkspaceActionRequestError(response.status, message);
  }
  return response.json() as Promise<T>;
}

export class WorkspaceActionRequestError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = "WorkspaceActionRequestError";
  }
}

const basePath = (workspaceId: string) => `/workspaces/${encodeURIComponent(workspaceId)}/actions`;

export function getSurfaceBrief(workspaceId: string, surface: SurfaceRef): Promise<SurfaceBrief> {
  return requestJson(`${basePath(workspaceId)}/brief`, { method: "POST", body: JSON.stringify(surface) });
}

export async function listWorkspaceActions(workspaceId: string, filters: { thread_id?: string; interaction_id?: string; relay_run_id?: string } = {}): Promise<ActionOutcome[]> {
  const query = new URLSearchParams(Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1])));
  const result = await requestJson<{ actions: ActionOutcome[] }>(`${basePath(workspaceId)}${query.size ? `?${query}` : ""}`);
  return result.actions;
}

export function getWorkspaceAction(workspaceId: string, actionId: string): Promise<ActionOutcome> {
  return requestJson(`${basePath(workspaceId)}/${encodeURIComponent(actionId)}`);
}

export function applyWorkspaceAction(workspaceId: string, actionId: string): Promise<ActionOutcome> {
  return requestJson(`${basePath(workspaceId)}/${encodeURIComponent(actionId)}/apply`, { method: "POST" });
}

export function dismissWorkspaceAction(workspaceId: string, actionId: string): Promise<ActionOutcome> {
  return requestJson(`${basePath(workspaceId)}/${encodeURIComponent(actionId)}/dismiss`, { method: "POST" });
}

export function undoWorkspaceAction(workspaceId: string, actionId: string): Promise<ActionOutcome> {
  return requestJson(`${basePath(workspaceId)}/${encodeURIComponent(actionId)}/undo`, { method: "POST" });
}
