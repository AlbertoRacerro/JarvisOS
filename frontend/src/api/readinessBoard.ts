import { API_BASE_URL } from "./client";
export * from "./caseVerdicts";

export type StepStatus = "green" | "red";

export type ComparisonValue = {
  value: number;
  unit: string;
};

export type StoredVerdictSummary = {
  id: string;
  verdict: "pass" | "fail";
  status: "recorded" | "accepted" | "superseded";
  run_id: string;
  deviation: number;
  rationale?: string | null;
};

export type Comparison = {
  quantity?: string | null;
  run_id: string;
  computed: ComparisonValue;
  computed_in_published_unit: number | null;
  published: ComparisonValue;
  deviation: number | null;
  rel_tol: number;
};

export type StepResult = {
  step: string;
  status: StepStatus;
  reason: string | null;
  missing: string[];
  detail: string | null;
  comparison?: Comparison | null;
  stored_verdict?: StoredVerdictSummary | null;
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
