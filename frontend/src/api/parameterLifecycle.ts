import { API_BASE_URL } from "./client";

export type ParameterLifecycleState = "active" | "inactive" | "superseded" | "archived" | "deleted";
export type ParameterLifecycleAction = "activate" | "deactivate" | "archive" | "delete";

export type CanonicalParameter = {
  id: string;
  workspace_id: string;
  name: string;
  symbol?: string | null;
  value?: string | null;
  unit: string;
  value_status: "candidate" | "literature" | "measured" | "validated" | "accepted";
  value_min?: number | null;
  value_max?: number | null;
  source_ref?: string | null;
  confidence?: number | null;
  status: string;
  notes?: string | null;
  supersedes_parameter_id?: string | null;
  created_at: string;
  updated_at: string;
  lifecycle_state: ParameterLifecycleState;
};

export type ParameterEditInput = {
  name: string;
  symbol: string | null;
  value: string | null;
  unit: string;
  value_status: CanonicalParameter["value_status"];
  value_min: number | null;
  value_max: number | null;
  source_ref: string | null;
  confidence: number | null;
  notes: string | null;
};

export class ParameterLifecycleApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(status: number, code: string | null, message: string) {
    super(message);
    this.name = "ParameterLifecycleApiError";
    this.status = status;
    this.code = code;
  }
}

function errorDetail(payload: unknown): { code: string | null; message: string } {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) {
    return { code: null, message: "Canonical Parameter request failed." };
  }
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") return { code: null, message: detail };
  if (detail && typeof detail === "object") {
    const record = detail as { code?: unknown; message?: unknown };
    return {
      code: typeof record.code === "string" ? record.code : null,
      message: typeof record.message === "string" ? record.message : "Canonical Parameter request failed.",
    };
  }
  return { code: null, message: "Canonical Parameter request failed." };
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);
  const payload = await response.json().catch(() => null) as unknown;
  if (!response.ok) {
    const detail = errorDetail(payload);
    throw new ParameterLifecycleApiError(response.status, detail.code, detail.message);
  }
  return payload as T;
}

export function listCanonicalParameters(workspaceId: string, includeNoncurrent = false): Promise<CanonicalParameter[]> {
  const query = includeNoncurrent ? "?include_noncurrent=true" : "";
  return requestJson<CanonicalParameter[]>(`/workspaces/${workspaceId}/parameters${query}`);
}

export function updateCanonicalParameter(
  parameter: Pick<CanonicalParameter, "id" | "workspace_id" | "updated_at">,
  edits: ParameterEditInput,
): Promise<CanonicalParameter> {
  return requestJson<CanonicalParameter>(`/parameters/${parameter.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      workspace_id: parameter.workspace_id,
      expected_updated_at: parameter.updated_at,
      ...edits,
    }),
  });
}

export function transitionCanonicalParameter(
  parameter: Pick<CanonicalParameter, "id" | "workspace_id" | "updated_at" | "lifecycle_state">,
  action: ParameterLifecycleAction,
  reason: string | null = null,
): Promise<CanonicalParameter> {
  return requestJson<CanonicalParameter>(`/parameters/${parameter.id}/lifecycle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      workspace_id: parameter.workspace_id,
      action,
      expected_lifecycle_state: parameter.lifecycle_state,
      expected_updated_at: parameter.updated_at,
      reason,
    }),
  });
}
