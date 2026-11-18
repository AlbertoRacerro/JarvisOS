import { API_BASE_URL } from "./client";

export type StepStatus = "green" | "red";

export type StepResult = {
  step: string;
  status: StepStatus;
  reason: string | null;
  missing: string[];
  detail: string | null;
};

export type CaseResult = {
  id: string;
  title: string;
  workspace: string;
  workspace_id: string | null;
  source_title: string;
  steps: Record<string, StepResult>;
};

export type BoardSummary = {
  total_cases: number;
  total_cells: number;
  green_cells: number;
  red_cells: number;
  first_red: string | null;
};

export type BoardError = {
  case_id: string | null;
  message: string;
};

export type ReadinessBoardRead = {
  cases: CaseResult[];
  errors: BoardError[];
  summary: BoardSummary;
};

export async function getReadiness(): Promise<ReadinessBoardRead> {
  const response = await fetch(`${API_BASE_URL}/readiness`);
  if (!response.ok) {
    throw new Error(`Failed to load readiness board: ${response.status}`);
  }
  return response.json() as Promise<ReadinessBoardRead>;
}
