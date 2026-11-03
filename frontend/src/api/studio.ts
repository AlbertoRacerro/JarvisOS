import { API_BASE_URL } from "./client";
import type { ProcedureCase, ProcedureCreate, ProcedureReport, ProcedureStep } from "./studioProcedure";

export type StudioVariable = { name: string; label: string; unit: string; description: string; min?: number | null; max?: number | null };
export type StudioCase = { inputs: Record<string, number>; expected_outputs?: Record<string, number>; tolerance?: number };
export type StudioReport = { source_sha256: string; contract_sha256: string; passed: boolean; cases: { passed: boolean; classification: string; outputs?: Record<string, unknown> | null; errors: string[]; codes?: string[]; culture_generation?: Record<string, number> | null }[] };
// Spec 190: process-unit contract and test-case shapes (backend studio/unit_models.py).
export type StudioPort = { name: string; label: string };
export const CULTURE_FIELDS = ["biomass", "nitrogen", "phosphorus", "oxygen", "dic", "salinity"] as const;
export type StudioUnitStream = { mass_flow_kg_s: number; temperature_K: number; pressure_Pa: number; mass_fractions: Record<string, number>; culture: Record<string, number | null> };
export type StudioExpectedStream = { mass_flow_kg_s?: number | null; temperature_K?: number | null; pressure_Pa?: number | null; mass_fractions?: Record<string, number> | null };
export type StudioUnitCase = { inlets: Record<string, StudioUnitStream>; params: Record<string, number>; expected_outlets?: Record<string, StudioExpectedStream> | null; tolerance?: number };
export type StudioUnitContract = { inlets: StudioPort[]; outlets: StudioPort[]; parameters: StudioVariable[]; symbol: string | null; culture_generation: string[] };
export type StudioUnitCreate = StudioUnitContract & { category: "process_unit"; name: string; engineering_question: string };
export type StudioMathNode = { tag: string; text?: string; children?: StudioMathNode[] };
export type StudioStep =
  | { kind: "equation"; target: StudioMathNode; expr: StudioMathNode; line: number }
  | { kind: "code"; text: string; reason: string; line: number }
  | { kind: "conditional"; form: "piecewise"; target: StudioMathNode; cases: { condition: StudioMathNode | null; expr: StudioMathNode }[]; line: number }
  | { kind: "conditional"; form: "branches"; branches: { label: string; condition: StudioMathNode | null; steps: StudioStep[] }[]; line: number };
export type StudioDerived = {
  source_sha256: string; status: "ok" | "syntax_error" | "entry_missing"; unrendered_count: number;
  functions: { name: string; args: string[]; docstring: string | null; steps: StudioStep[] }[];
  module_constants: StudioStep[]; error?: { message: string; line: number | null };
};
export type StudioVersion = { id: string; version_label: string; source_sha256: string; contract_sha256: string; created_at: string };
// Spec 189: geometry builds one build123d solid; facts and artifacts come from the sandbox run.
export type StudioGeometryCase = { inputs: Record<string, number>; volume_min?: number | null; volume_max?: number | null; max_extent?: number | null };
export type StudioGeometryFacts = { valid: boolean; solids: number; volume: number; bbox_min: number[]; bbox_max: number[] };
export type StudioGeometryArtifact = { name: "model.step" | "model.stl" | "model.glb"; sha256: string; size_bytes: number };
export type StudioGeometryCaseResult = { passed: boolean; classification: string; facts: StudioGeometryFacts | null; limit: string | null; limit_value: number | null; preview_id: string | null; artifacts: StudioGeometryArtifact[]; duration_seconds?: number; errors: string[] };
export type StudioCategoryId = "calculation" | "process_unit" | "procedure" | "geometry";
export const STUDIO_KINDS = ["studio_calc_v1", "studio_geometry_v1"] as const;
export const isStudioKind = (kind: string | null | undefined): boolean => (STUDIO_KINDS as readonly string[]).includes(kind ?? "");
export type StudioModel = {
  id: string; workspace_id: string; name: string; engineering_question: string; category: StudioCategoryId; status: string;
  // A calculation declares inputs/outputs; a process unit (spec 190) declares ports and parameters instead.
  inputs: StudioVariable[]; outputs: StudioVariable[]; source: string;
  steps?: ProcedureStep[]; notice?: string; inlets?: StudioPort[]; outlets?: StudioPort[]; parameters?: StudioVariable[]; symbol?: string | null; culture_generation?: string[]; source_sha256: string; contract_sha256: string;
  annotations: string; assumptions: string; annotations_stale: boolean; test_cases: StudioCase[] | StudioUnitCase[] | StudioGeometryCase[] | ProcedureCase[];
  documentation: { derived: StudioDerived; inputs: StudioVariable[]; outputs: StudioVariable[] } & Partial<StudioUnitContract>;
  last_test_report?: StudioReport | null; versions: StudioVersion[]; created_at: string; updated_at: string;
};
export type StudioCategory = { id: string; label: string; available: boolean; reason?: string | null };
export type StudioLibraryEntry = { category?: StudioCategoryId; id: string; model_spec_id: string; name: string; version_label: string; source_sha256: string; contract_sha256: string; created_at: string; scientific_status: "unqualified" };
export type StudioRuntimeStatus = { enabled: boolean; available: boolean; mechanism: string; reason?: string | null };

/** Carries the backend's typed code, for example studio_symbol_invalid. */
export class StudioApiError extends Error {
  constructor(message: string, readonly code: string | null, readonly reasons: string[] = []) { super(message); this.name = "StudioApiError"; }
}
export async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { method, headers: body === undefined ? undefined : { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) {
    const data = await response.json().catch(() => null) as { detail?: { message?: string; code?: string; reasons?: unknown } | { loc?: unknown[]; msg?: string }[] | string } | null;
    const list = Array.isArray(data?.detail) ? data.detail.map((item) => `${(item.loc ?? []).slice(1).join(".")}: ${item.msg ?? "invalid"}`).join("; ") : undefined;
    const detail = typeof data?.detail === "string" ? data.detail : list ?? (data?.detail as { message?: string } | undefined)?.message;
    throw new StudioApiError(detail ?? `Request failed with ${response.status}`, data?.detail && !Array.isArray(data.detail) && typeof data.detail === "object" ? (data.detail as { code?: string }).code ?? null : null,
      Array.isArray((data?.detail as { reasons?: unknown } | undefined)?.reasons) ? ((data!.detail as { reasons: unknown[] }).reasons).filter((item): item is string => typeof item === "string") : []);
  }
  return response.json() as Promise<T>;
}
export const root = (workspaceId: string) => `/workspaces/${encodeURIComponent(workspaceId)}/studio`;
const model = (workspaceId: string, id: string) => `${root(workspaceId)}/models/${encodeURIComponent(id)}`;
export const listStudioCategories = () => request<StudioCategory[]>("/studio/categories");
export const getStudioRuntimeStatus = () => request<StudioRuntimeStatus>("/studio/runtime/status");
/** Parse unsaved Python for read-only mathematics; the server must never execute it. */
export const previewStudioDocumentation = (source: string) => request<StudioDerived>("/studio/documentation/preview", "POST", { source });
export const previewStudioBuildDocumentation = (source: string) =>
  request<StudioDerived>("/studio/documentation/preview", "POST", { source, entry: "build" });
export const listStudioModels = (workspaceId: string) => request<StudioModel[]>(`${root(workspaceId)}/models`);
export const getStudioModel = (workspaceId: string, id: string) => request<StudioModel>(model(workspaceId, id));
export type StudioGeometryCreate = { category: "geometry"; name: string; engineering_question: string; inputs: StudioVariable[] };
export const createStudioModel = (workspaceId: string, payload: Pick<StudioModel, "name" | "engineering_question" | "inputs" | "outputs"> | StudioUnitCreate | StudioGeometryCreate | ProcedureCreate) => request<StudioModel>(`${root(workspaceId)}/models`, "POST", payload);
export const saveStudioModel = (workspaceId: string, id: string, payload: Partial<Pick<StudioModel, "name" | "engineering_question" | "inputs" | "outputs" | "source" | "annotations" | "assumptions">> | Partial<StudioUnitContract & Pick<StudioModel, "name" | "engineering_question" | "source" | "annotations" | "assumptions">> | { name?: string; engineering_question?: string; parameters?: StudioVariable[]; steps?: ProcedureStep[]; annotations?: string; assumptions?: string }) => request<StudioModel>(model(workspaceId, id), "PUT", payload);
export const testStudioModel = (workspaceId: string, id: string, cases: StudioCase[] | StudioUnitCase[] | StudioGeometryCase[] | ProcedureCase[]) => request<StudioReport | ProcedureReport>(`${model(workspaceId, id)}/test`, "POST", { cases });
export const publishStudioModel = (workspaceId: string, id: string) => request<StudioVersion>(`${model(workspaceId, id)}/publish`, "POST", {});
export const reviseStudioModel = (workspaceId: string, id: string) => request<StudioModel>(`${model(workspaceId, id)}/revise`, "POST", {});
export type StudioExample = { workspace: { id: string; name: string; slug: string }; created: boolean; added: string[] };
/** Create or reopen the AI-prepared example workspace (spec 209); existing artifacts are never modified. */
export const openStudioExample = () => request<StudioExample>("/studio/example-workspace", "POST", {});
export const listStudioLibrary = (workspaceId: string) => request<StudioLibraryEntry[]>(`${root(workspaceId)}/library`);
/** Untrusted geometry bytes from the current draft test report; the backend re-verifies each hash. */
export const geometryPreviewUrl = (workspaceId: string, id: string, previewId: string, name: string) => `${API_BASE_URL}${model(workspaceId, id)}/previews/${encodeURIComponent(previewId)}/${name}`;
export const geometryRunArtifactUrl = (workspaceId: string, runId: string, name: string) => `${API_BASE_URL}${root(workspaceId)}/geometry-runs/${encodeURIComponent(runId)}/${name}`;
// #920 item 3 / spec 214: proposal-only Workshop draft from the answers so far; stateless, nothing is persisted or run.
export type WorkshopDraftRequest = { category: StudioCategoryId; step: number; name: string; engineering_question: string; approach: string; assumptions: string; test_intent: string; inputs: StudioVariable[]; outputs: StudioVariable[]; inlets: StudioPort[]; outlets: StudioPort[]; parameters: StudioVariable[] };
export type WorkshopDraft = { step: number; category: StudioCategoryId; origin: "ai" | "deterministic"; reason: string | null; contract: Record<string, unknown> | null; contract_ready: boolean; source: string; acceptable: boolean; explanation: string; ai_job_id: string | null };
export const requestWorkshopDraft = (workspaceId: string, payload: WorkshopDraftRequest) => request<WorkshopDraft>(`${root(workspaceId)}/workshop-draft`, "POST", payload);
