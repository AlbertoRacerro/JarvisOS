import { request, root, type StudioVariable } from "./studio";

// Spec 191: procedure contract, test and bench shapes (backend studio/procedure.py and procedure_service.py).
export const PROCEDURE_NOTICE = "Simulation only. Actions change modelled tank inventories. This does not command any equipment. Unqualified.";
export type ParamRef = { $param: string };
export type Num = number | ParamRef;
export type ActionType = "draw" | "separate" | "refill" | "dose" | "inoculate";
// An action is a closed record keyed by the 185 field names; nested records hold medium and culture values.
export type ProcedureAction = { type: ActionType } & Record<string, unknown>;
export type StepCondition = { observed: string; direction: "above" | "below"; threshold: Num; hysteresis: Num; cooldown_s?: Num; max_fires?: Num };
export type ProcedureStep = { label: string; time_s: Num; every_s?: Num; count?: Num; end_s?: Num; condition?: StepCondition; actions: ProcedureAction[] };
export type ProcedureCreate = { category: "procedure"; name: string; engineering_question: string; parameters: StudioVariable[]; steps: ProcedureStep[] };

export type Expectation = { kind: "series" | "fire_time" | "balances_closed"; channel?: string; time_s?: number; phase?: "pre" | "post" | "sample"; fire_index?: number; event_order?: number; value?: number; tolerance?: number; relative?: boolean };
export type ProcedureCase = { name: string; parameter_values: Record<string, number>; draft_id: string; scenario_id: string; schedule_id: string; simulate: boolean; expect: Expectation[] };
export type ProcedureCaseResult = {
  name: string; passed: boolean; code: string | null; message: string | null; detail: unknown; compiled_sha256?: string | null; compiled?: { id: string; events: unknown[] } | null;
  prepare?: { ok: boolean; expanded_event_count?: number; time_triggered_events?: number; state_triggered_events?: number; first_event_time_s?: number | null; last_event_time_s?: number | null; duration_s?: number; expanded_schedule_sha256?: string } | null;
  simulation?: { status: string; error: { code?: string; message?: string } | null; balances_asserted?: boolean; event_count?: number; state_fires?: { time_s: number }[] } | null;
  expectations?: { kind: string; passed: boolean; actual?: unknown; expected?: unknown; message?: string }[]; errors?: string[];
};
export type ProcedureReport = { source_sha256: string; contract_sha256: string; passed: boolean; notice?: string; cases: ProcedureCaseResult[] };
export const isProcedureReport = (report: unknown): report is ProcedureReport => Boolean(report && Array.isArray((report as ProcedureReport).cases) && ((report as ProcedureReport).cases[0] === undefined || "name" in (report as ProcedureReport).cases[0]) && !("classification" in ((report as ProcedureReport).cases[0] ?? {})));

export type BenchScenario = { id: string; schedule_id: string | null; units: string[]; start_utc: string | null; end_utc: string | null };
export type ProcedureLink = {
  id: string; draft_id: string; scenario_id: string; schedule_id: string; model_spec_id: string; version_id: string; version_label: string; version_number: number;
  source_sha256: string; contract_sha256: string; parameter_values: Record<string, number>; compiled_sha256: string; applied_at: string;
  state: "applied" | "modified" | "missing"; state_text: string; latest_version_id: string | null; latest_version_label: string | null; update_available: boolean; notice?: string;
};
export type BenchDraft = { draft_id: string; name: string; revision: string; units: { tag: string; type: string }[]; tanks: string[]; scenarios: BenchScenario[]; schedules: { id: string; event_count: number; link: ProcedureLink | null }[]; scenario_check?: { ok: boolean; code?: string; message?: string } };
export type ScenarioRequest = { scenario_id: string; profile_id: string; days: number; output_cadence_s: number; circulation?: Record<string, number>; initial?: Record<string, { X: number; N: number; O2: number }> };
export type ApplyRequest = { version_id: string; draft_id: string; scenario_id: string; schedule_id: string; parameter_values: Record<string, number>; overwrite: boolean };
export type RunJob = { job_id: string; status: string; progress?: number; error?: { code?: string; message?: string } | null; created_at?: string; current?: unknown };
export type RunRow = { job: RunJob; procedure_link: ProcedureLink };
export type EventRecord = { type?: string; time_s?: number; trigger?: string; condition?: unknown; actions?: Record<string, unknown>[]; balance?: { closed?: boolean }; inventory_deltas?: Record<string, unknown>; [key: string]: unknown };
export type CycleKpi = Record<string, number | boolean | null>;
export type RunResults = {
  job_id: string; status: string; error?: { code?: string; message?: string } | null; procedure_link: ProcedureLink; event_log: EventRecord[];
  balances: { aggregate?: Record<string, { residual_abs_kg: number; residual_rel: number }>; asserted?: boolean } | null;
  campaign: { loops: Record<string, { cycles: CycleKpi[]; summary: Record<string, unknown> }>; duration_s?: number } | null; notice: string;
};
export type SeriesResponse = { channels: Record<string, number[]>; returned_points: number; total_points: number; stride: number };
export type EnvironmentProfileHead = { profile_id: string; name: string; start: string; end: string };

const bench = (ws: string) => `${root(ws)}/procedure`;
export const listBenchDrafts = (ws: string) => request<BenchDraft[]>(`${bench(ws)}/bench/drafts`);
export const createBenchScenario = (ws: string, draftId: string, body: ScenarioRequest) => request<BenchDraft>(`${bench(ws)}/bench/drafts/${encodeURIComponent(draftId)}/scenarios`, "POST", body);
export const applyProcedure = (ws: string, body: ApplyRequest) => request<ProcedureLink>(`${bench(ws)}/apply`, "POST", body);
export const updateProcedureLink = (ws: string, linkId: string, body: { version_id?: string; parameter_values?: Record<string, number>; overwrite: boolean }) => request<ProcedureLink>(`${bench(ws)}/links/${encodeURIComponent(linkId)}/update`, "POST", body);
export const listProcedureLinks = (ws: string, draftId?: string) => request<ProcedureLink[]>(`${bench(ws)}/links${draftId ? `?draft_id=${encodeURIComponent(draftId)}` : ""}`);
export const runProcedureLink = (ws: string, linkId: string) => request<{ job_id: string; status: string; procedure_link: ProcedureLink; notice: string }>(`${bench(ws)}/links/${encodeURIComponent(linkId)}/run`, "POST", {});
export const listProcedureRuns = (ws: string, draftId: string) => request<RunRow[]>(`${bench(ws)}/drafts/${encodeURIComponent(draftId)}/runs`);
export const getProcedureRun = (ws: string, draftId: string, jobId: string) => request<{ job: RunJob; procedure_link: ProcedureLink }>(`${bench(ws)}/drafts/${encodeURIComponent(draftId)}/runs/${encodeURIComponent(jobId)}`);
export const getProcedureResults = (ws: string, draftId: string, jobId: string) => request<RunResults>(`${bench(ws)}/drafts/${encodeURIComponent(draftId)}/runs/${encodeURIComponent(jobId)}/results`);
const dynamic = (ws: string, draftId: string, jobId: string) => `/workspaces/${encodeURIComponent(ws)}/process/drafts/${encodeURIComponent(draftId)}/dynamic/runs/${encodeURIComponent(jobId)}`;
export const getRunManifest = (ws: string, draftId: string, jobId: string) => request<{ channels?: Record<string, string> }>(`${dynamic(ws, draftId, jobId)}/manifest`);
export const getRunSeries = (ws: string, draftId: string, jobId: string, channels: string[], maxPoints = 400) =>
  request<SeriesResponse>(`${dynamic(ws, draftId, jobId)}/series?channels=${encodeURIComponent(channels.join(","))}&max_points=${maxPoints}`);
export const listEnvironmentProfiles = (ws: string) => request<EnvironmentProfileHead[]>(`/workspaces/${encodeURIComponent(ws)}/environment/profiles`);
