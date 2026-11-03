import { API_BASE_URL } from "./client";

export type MemoryRecordKind = "assumption" | "parameter" | "decision";
export type MemoryStatus = "proposed" | "accepted" | "rejected" | "superseded";
export type MemoryStatusFilter = MemoryStatus | "all";

export type MemoryRecord = {
  id: string;
  record_kind: MemoryRecordKind;
  workspace_id: string;
  status: MemoryStatus;
  origin: string;
  source_ai_job_id: string | null;
  promoted_at: string | null;
  created_at: string;
  updated_at: string;
  title: string | null;
  statement: string | null;
  decision_text: string | null;
  name: string | null;
  source_ref: string | null;
  notes: string | null;
  supersedes_parameter_id: string | null;
  scope: string | null;
  confidence: string | number | null;
  symbol: string | null;
  value: string | null;
  unit: string | null;
  value_status: string | null;
  value_min: number | null;
  value_max: number | null;
  rationale: string | null;
  linked_run_id: string | null;
};

export type ReplacementInvalidation = {
  id: string;
  source_ref: string;
  replacement_ref: string;
  affected_count: number;
  graph_digest: string;
  created_at: string;
};

export type ReplacementResult = {
  accepted_parameter: MemoryRecord;
  superseded_parameter: MemoryRecord;
  invalidation: ReplacementInvalidation;
};

export class MemoryRequestError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "MemoryRequestError";
    this.status = status;
  }
}

function errorMessage(value: unknown, status: number): string {
  if (value && typeof value === "object" && "detail" in value) {
    const detail = (value as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail.slice(0, 400);
    if (detail && typeof detail === "object" && "message" in detail) {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === "string" && message.trim()) return message.slice(0, 400);
    }
  }
  return `Request failed with ${status}`;
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);
  if (!response.ok) {
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      // Keep the status-only fallback for non-JSON errors.
    }
    throw new MemoryRequestError(response.status, errorMessage(payload, response.status));
  }
  return response.json() as Promise<T>;
}

export function listMemoryProposals(workspaceId: string, status: MemoryStatusFilter): Promise<MemoryRecord[]> {
  const params = new URLSearchParams({ workspace_id: workspaceId });
  if (status !== "all") params.set("status", status);
  return requestJson<MemoryRecord[]>(`/memory/proposals?${params.toString()}`);
}

export function promoteMemoryRecord(kind: MemoryRecordKind, id: string): Promise<MemoryRecord> {
  return requestJson<MemoryRecord>(`/memory/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/promote`, { method: "POST" });
}

export function rejectMemoryRecord(kind: MemoryRecordKind, id: string): Promise<MemoryRecord> {
  return requestJson<MemoryRecord>(`/memory/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/reject`, { method: "POST" });
}

export function promoteParameterReplacement(id: string): Promise<ReplacementResult> {
  return requestJson<ReplacementResult>(`/memory/parameter/${encodeURIComponent(id)}/promote-replacement`, { method: "POST" });
}
