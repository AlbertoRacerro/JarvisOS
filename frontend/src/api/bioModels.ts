import { API_BASE_URL } from "./client";

export type MathNode = { tag: string; text?: string; children?: MathNode[] };
export type BioSymbol = { symbol: string; key: string; meaning: string; unit: string; valid_range: string };
export type BioForm = { id: string; version: string; family: string; equation: MathNode; equation_text: string; symbols: BioSymbol[]; parameters: BioSymbol[]; inputs: BioSymbol[]; applies_to: string; citations: string[] };
export type BioSet = { id: string; name: string; species: string; strain: string; revision: string; digest: string; history: Array<Record<string, unknown>>; values: Record<string, BioValue> };
export type BioValue = { value: number; entered_value?: number; entered_unit?: string; unit: string; validity_range?: Record<string, number | boolean> | string; provenance?: { source_title?: string; source_state?: string; entry_state?: string; locator_kind?: string; locator_start?: number; locator_end?: number }; basis_ref?: Record<string, unknown> | null; state: string; display_state?: string; verification?: Record<string, unknown> | null; state_changed_by?: string; state_changed_at?: string };
export type BioCard = { id: string; name: string; parameter_set_id: string; factors: Record<string, unknown>; mu_max: { value: number; unit: string }; n_source?: "NH3" | "HNO3"; revision: string; digest: string; parameter_set_revision?: string; parameter_set_digest?: string; form_versions?: Record<string, string>; history?: Array<Record<string, unknown>> };
export type BioQuantity = { value: number; unit: string };

export class BioModelsError extends Error {
  constructor(readonly status: number, readonly code: string, message: string, readonly detail: Record<string, unknown> = {}) { super(message); }
}

async function request<T>(workspaceId: string, path: string, method = "GET", body?: Record<string, unknown>): Promise<T> {
  const response = await fetch(`${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/bio-models${path}`, {
    method,
    ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
  });
  const data = await response.json().catch(() => null) as { detail?: { code?: string; message?: string; [key: string]: unknown } } | null;
  if (!response.ok) throw new BioModelsError(response.status, data?.detail?.code ?? "request_failed", data?.detail?.message ?? `Request failed (${response.status})`, data?.detail ?? {});
  return data as T;
}

export const listBioForms = (workspaceId: string) => request<BioForm[]>(workspaceId, "/forms");
export const listBioSets = (workspaceId: string) => request<BioSet[]>(workspaceId, "/sets");
export const listBioCards = (workspaceId: string) => request<BioCard[]>(workspaceId, "/cards");
export const createBioSet = (workspaceId: string, name: string) => request<BioSet>(workspaceId, "/sets", "POST", { name, species: "N. gaditana" });
export const duplicateBioSet = (workspaceId: string, set: BioSet) => request<BioSet>(workspaceId, `/sets/${set.id}/duplicate`, "POST", { expected_revision: set.revision, expected_digest: set.digest });
export const editBioValue = (workspaceId: string, set: BioSet, symbol: string, value: number, unit: string, expectedUnit: string, basisRef?: Record<string, unknown>, validityRange?: Record<string, number | boolean>) =>
  request<BioSet>(workspaceId, `/sets/${set.id}/values/${encodeURIComponent(symbol)}`, "PUT", { expected_revision: set.revision, expected_digest: set.digest, value, unit, expected_unit: expectedUnit, ...(basisRef ? { basis_ref: basisRef } : {}), ...(validityRange ? { validity_range: validityRange } : {}) });
export const verifyBioValue = (workspaceId: string, set: BioSet, symbol: string, locatorConfirmed = false, locatorConfirmation?: string) =>
  request<BioSet>(workspaceId, `/sets/${set.id}/values/${encodeURIComponent(symbol)}/verify`, "POST", { expected_revision: set.revision, expected_digest: set.digest, locator_confirmed: locatorConfirmed, ...(locatorConfirmation ? { locator_confirmation: locatorConfirmation } : {}) });
export const reviewBioValue = (workspaceId: string, set: BioSet, symbol: string, reviewer: string, note: string) =>
  request<BioSet>(workspaceId, `/sets/${set.id}/values/${encodeURIComponent(symbol)}/review`, "POST", { expected_revision: set.revision, expected_digest: set.digest, reviewer, note });
export const createBioCard = (workspaceId: string, name: string, set: BioSet, factors: Record<string, unknown>, muMax: { value: number; unit: string }, nSource: "NH3" | "HNO3") =>
  request<BioCard>(workspaceId, "/cards", "POST", { name, parameter_set_id: set.id, factors, mu_max: muMax, n_source: nSource });
export const evaluateBioCard = (workspaceId: string, card: BioCard, operatingPoint: Record<string, BioQuantity>) =>
  request<{ mu_net: { value: number; unit: string }; breakdown: Record<string, { value: number; unit: string }>; stoichiometry?: { coefficients_mol_per_C_mol: Record<string, number>; yields: Record<string, number> } }>(workspaceId, `/cards/${card.id}/evaluate`, "POST", { operating_point: operatingPoint });
