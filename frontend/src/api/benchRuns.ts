import { API_BASE_URL } from "./client";

// Spec 299: read-only bench results of one workspace. The routes carry no filesystem path.
export type BenchVerdict = "usable" | "usable_with_warnings" | "unchecked" | "not_usable";
export type BenchRunRow = { run_id: string; evaluator_id: string; status: string; verdict: BenchVerdict; completed_at: string };
export type BenchRunList = { runs: BenchRunRow[]; skipped: number };
export type BenchCheck = {
  id: string; family: string; status: string; actual: number | null; limit: number | null;
  unit: string | null; hint: string; source: string;
};
export type BenchOutput = { name: string; value: { value: number; unit: string } };
export type BenchPort = {
  name: string; interface: string; origin: number[]; direction: number[];
  outer_d?: number; wall_t?: number; pad_d?: number;
};
export type BenchRunDetail = {
  run_id: string; evaluator_id: string; status: string; verdict: BenchVerdict;
  failure: { code?: string; message?: string } | null; checks: BenchCheck[]; outputs: BenchOutput[];
  outputs_withheld: boolean; artifacts: unknown[]; truncated: boolean; ports?: BenchPort[];
};
type Side = { value: number | null; unit: string | null } | null;
export type BenchCompare = {
  run_id_a: string; run_id_b: string; evaluator_a: string; evaluator_b: string;
  status_a: string; status_b: string; verdict_a: BenchVerdict; verdict_b: BenchVerdict;
  outputs: Record<string, { a: Side; b: Side; unit?: string | null; absolute_difference: number | null; relative_difference: number | null; mark: string }>;
  checks: Record<string, { status_a: string | null; actual_a: number | null; status_b: string | null; actual_b: number | null }>;
  truncated: boolean;
};

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) throw new Error(`Request failed with ${response.status}`);
  return response.json() as Promise<T>;
}

const base = (workspaceId: string) => `/workspaces/${encodeURIComponent(workspaceId)}/engineering/bench`;

export function listBenchRuns(workspaceId: string): Promise<BenchRunList> {
  return getJson(`${base(workspaceId)}/runs`);
}

export function getBenchRun(workspaceId: string, runId: string): Promise<BenchRunDetail> {
  return getJson(`${base(workspaceId)}/runs/${encodeURIComponent(runId)}`);
}

export function compareBenchRuns(workspaceId: string, runA: string, runB: string): Promise<BenchCompare> {
  return getJson(`${base(workspaceId)}/compare?run_a=${encodeURIComponent(runA)}&run_b=${encodeURIComponent(runB)}`);
}
