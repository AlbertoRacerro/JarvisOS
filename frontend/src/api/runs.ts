import { API_BASE_URL } from "./client";

export type SimulationRunSummary = {
  id: string;
  workspace_id: string;
  model_version_id: string | null;
  run_label: string | null;
  status: string;
  input_payload: string | null;
  parameter_payload: string | null;
  output_payload: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  notes: string | null;
};

export type SimulationRunDetail = SimulationRunSummary & { stale?: boolean; superseded?: boolean };

export type RunLog = {
  id: string;
  workspace_id: string;
  simulation_run_id: string;
  stream: string;
  content: string;
  truncated: boolean;
  created_at: string;
};

export type RunArtifact = {
  artifact_id: string;
  workspace_id: string;
  simulation_run_id: string;
  role: string;
  artifact_type: string;
  filename: string;
  size_bytes: number | null;
  created_at: string;
  source_ref: string | null;
  source_module: string | null;
  mime_type: string | null;
  sha256: string | null;
  status: string;
  under_data_root: boolean;
};

export class RunsRequestError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`Request failed with ${status}`);
    this.name = "RunsRequestError";
    this.status = status;
  }
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) throw new RunsRequestError(response.status);
  return response.json() as Promise<T>;
}

const base = (workspaceId: string) => `/workspaces/${encodeURIComponent(workspaceId)}/simulation-runs`;

export const listRuns = (workspaceId: string) => getJson<SimulationRunSummary[]>(base(workspaceId));
export const getRun = (workspaceId: string, runId: string) => getJson<SimulationRunDetail>(`${base(workspaceId)}/${encodeURIComponent(runId)}`);
export const listRunLogs = (workspaceId: string, runId: string) => getJson<RunLog[]>(`${base(workspaceId)}/${encodeURIComponent(runId)}/logs`);
export const listRunArtifacts = (workspaceId: string, runId: string) => getJson<RunArtifact[]>(`${base(workspaceId)}/${encodeURIComponent(runId)}/artifacts`);
