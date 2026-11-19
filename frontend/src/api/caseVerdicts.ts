import { API_BASE_URL } from "./client";
import type { CaseVerdictRead } from "./generated/engineering";

export type { CaseVerdictRead };

export type CreateCaseVerdictPayload = {
  case_id: string;
  run_id: string;
  quantity: string;
  published_value: number;
  published_unit: string;
  rel_tol: number;
  rationale?: string | null;
};

export type UpdateCaseVerdictPayload = {
  status?: "recorded" | "accepted" | "superseded" | null;
  rationale?: string | null;
};

export async function listCaseVerdicts(workspaceId: string): Promise<CaseVerdictRead[]> {
  const response = await fetch(
    `${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/engineering/case-verdicts`,
  );
  if (!response.ok) {
    throw new Error(`Failed to list case verdicts: ${response.status}`);
  }
  return response.json() as Promise<CaseVerdictRead[]>;
}

export async function createCaseVerdict(
  workspaceId: string,
  payload: CreateCaseVerdictPayload,
): Promise<CaseVerdictRead> {
  const response = await fetch(
    `${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/engineering/case-verdicts`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) {
    throw new Error(`Failed to create case verdict: ${response.status}`);
  }
  return response.json() as Promise<CaseVerdictRead>;
}

export async function updateCaseVerdict(
  workspaceId: string,
  verdictId: string,
  payload: UpdateCaseVerdictPayload,
): Promise<CaseVerdictRead> {
  const response = await fetch(
    `${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/engineering/case-verdicts/${encodeURIComponent(verdictId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) {
    throw new Error(`Failed to update case verdict: ${response.status}`);
  }
  return response.json() as Promise<CaseVerdictRead>;
}
