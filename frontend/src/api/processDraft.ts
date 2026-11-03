import { API_BASE_URL } from "./client";
import type { MathNode } from "./bioModels";

// Spec 155: Jarvis-owned process draft. Quantities travel as value + unit; the server converts.
export type DraftQuantity = { value: number; unit: string };
export type StoredQuantity = DraftQuantity & { si: number };
export type Endpoint = { unit: string; port: number };
export type RoutePoint = { x: number; y: number };
export type OptionValue = string | number | boolean;
export type DraftObject = {
  id: string;
  kind: "unit" | "stream";
  type: string;
  tag: string;
  x: number;
  y: number;
  mode?: string | null;
  owner?: "dwsim" | string;
  culture_rule?: "pass_through" | "refuse" | string;
  params?: Record<string, StoredQuantity>;
  // Spec 158: enum/bool unit options and the kinetic reactions a reactor uses.
  options?: Record<string, OptionValue>;
  reactions?: string[];
  model?: { card_id: string; card_revision: string; card_digest: string } | null;
  source?: Endpoint | null;
  target?: Endpoint | null;
  spec?: {
    temperature?: StoredQuantity;
    pressure?: StoredQuantity;
    mass_flow?: StoredQuantity;
    molar_flow?: StoredQuantity;
    vapor_fraction?: StoredQuantity;
    composition?: Record<string, number>;
    composition_basis?: "mass" | "mole";
    duty?: StoredQuantity;
    culture?: Record<string, StoredQuantity>;
  };
  // Layout-only orthogonal route waypoints; never materialized, never stale results.
  route?: RoutePoint[];
  flip_x?: boolean;
  flip_y?: boolean;
  // Spec 190: a placed StudioUnit pins a published version (`model` holds StudioPin) and embeds the verified contract.
  studio?: StudioPlacement;
};
/** Spec 190 pin of a placed StudioUnit; both hashes are verified against the published artifact. */
export type StudioPin = { version_id: string; source_sha256: string; contract_sha256: string };
export type StudioContractPort = { name: string; label: string };
export type StudioContractParam = { name: string; label: string; unit: string; description: string; min?: number | null; max?: number | null };
export type StudioPlacement = {
  model_spec_id: string; name: string; version_label: string; version_number: number;
  contract: { inlets: StudioContractPort[]; outlets: StudioContractPort[]; parameters: StudioContractParam[]; symbol: string | null; culture_generation: string[] };
};
/** Palette row for a published Studio unit (registry projection `studio_units`). */
export type StudioPaletteUnit = {
  type: "StudioUnit"; label: string; owner: string; culture_rule: string; model_spec_id: string; version_id: string; version_label: string; version_number: number;
  source_sha256: string; contract_sha256: string; inlets: string[]; inlet_labels: string[]; outlets: string[]; outlet_labels: string[]; required_inlets: number;
  params: { key: string; label: string; unit: string; description: string; minimum: number | null; maximum: number | null }[];
  symbol: string | null; culture_generation: string[]; limits: string; steady_only: boolean;
};
/** Per placed unit pin status (draft projection `studio_units`). */
export type StudioPinStatus = { tag: string; name: string; version_label: string; version_number: number; version_id: string;
  latest_version_id: string | null; latest_version_number: number | null; update_available: boolean; limits: string };
/** Per custom unit provenance of one stored run (results view `studio_units`); cache_hits and sandbox_seconds may be absent. */
export type StudioRunProvenance = { version_id: string; version_label: string; source_sha256: string; contract_sha256: string; call_count: number;
  cache_hits?: number; sandbox_seconds?: number; duration_s?: number; runtime_classifications?: string[]; version_number: number | null; name: string | null;
  superseded: boolean; latest_version_number: number | null; limits: string };
/** Spec 158 typed kinetic reaction over declared compounds. */
export type DraftReaction = {
  id?: string;
  name: string;
  stoichiometry: Record<string, number>;
  orders?: Record<string, number>;
  base_reactant?: string | null;
  phase?: string | null;
  basis?: string | null;
  A_forward?: StoredQuantity | DraftQuantity | number | null;
  E_forward?: StoredQuantity | DraftQuantity | number | null;
  rate_law?: { form: "monod" | "haldane"; substrate: string; v_max: DraftQuantity | StoredQuantity; k_s: DraftQuantity | StoredQuantity;
    k_i?: DraftQuantity | StoredQuantity; inhibitions: { kind: "competitive" | "noncompetitive"; inhibitor: string; k_i: DraftQuantity | StoredQuantity }[];
    temperature?: { activation_energy: DraftQuantity | StoredQuantity; reference_temperature: DraftQuantity | StoredQuantity } };
  provenance?: { kind: "literature" | "measurement" | "operator_estimate" | "synthetic"; citation?: string; note?: string };
  validity?: { temperature_min?: DraftQuantity | StoredQuantity; temperature_max?: DraftQuantity | StoredQuantity; substrate_max?: DraftQuantity | StoredQuantity };
  [field: string]: unknown;
};
export type Finding = {
  severity: "blocker" | "error" | "warning" | "info" | string;
  code: string;
  object: string;
  field?: string;
  message: string;
  fix?: string;
  source: "jarvis" | "dwsim";
};
export type ProposalChange = {
  target: string;
  target_id: string;
  kind: "unit" | "stream";
  property: string;
  current: DraftQuantity | string | Record<string, number> | null;
  proposed: DraftQuantity | string | Record<string, number>;
};
export type Proposal = {
  proposal_id: string;
  draft_id: string;
  base_revision: string;
  status: "pending" | "approved" | "rejected";
  state: "pending" | "stale" | "approved" | "rejected";
  changes: ProposalChange[];
  rationale: string;
  source: string;
  created_at: string;
  applied_revision: string | null;
};
/** Spec 181: the single backend run outcome; the UI never derives one from raw statuses. */
export type OutcomeState = "validated" | "converged" | "non_converged" | "failed" | "cancelled";
export type RunOutcome = {
  state: OutcomeState;
  label: string;
  reason: string | null;
  message: string | null;
  failing: { tag: string | null; stage: string | null; error: string | null }[];
  residual: number | null;
  iterations: number | null;
  worst_tear: string | null;
  results_available: "none" | "converged" | "last_iterate";
  artifact: string | null;
};
export type ResultsState = {
  state: "none" | "current" | "stale";
  run_id?: string;
  draft_revision?: string;
  edits_since?: number;
  materialization_fingerprint?: string;
  last_attempt: { run_id: string; action: string; status: string; draft_revision: string; started_at: string; outcome?: RunOutcome } | null;
  outcome?: RunOutcome | null;
};
/** A results-view quantity: a value with its unit, or null with why (spec 181 decision 3). */
export type ViewQuantity =
  | { value: number; unit: string; label?: string; status?: undefined; reason?: undefined }
  | { value: null; status: "unavailable" | "not_computed" | "failed"; reason: string; unit?: undefined; label?: undefined };
export type ViewStream = {
  role: "feed" | "product" | "internal";
  from: string | null;
  to: string | null;
  temperature: ViewQuantity;
  pressure: ViewQuantity;
  mass_flow: ViewQuantity;
  molar_flow: ViewQuantity;
  volumetric_flow: ViewQuantity;
  vapor_fraction: ViewQuantity;
  mass_fractions: Record<string, ViewQuantity>;
  culture: Record<string, ViewQuantity> | null;
  culture_failure?: string | null;
  owner: string | null;
  state_source: string | null;
};
export type ViewUnit = {
  type: string;
  calculated: boolean | null;
  error: string | null;
  quantities: Record<string, ViewQuantity>;
  inlets: string[];
  outlets: string[];
  owner?: string;
  fidelity?: string;
};
export type Kpi = {
  id: string;
  label: string;
  value: number | null;
  unit: string;
  status: "available" | "unavailable" | "not_applicable";
  reason: string | null;
  definition: string;
  sources: string[];
};
export type BalanceRow = { in?: number; out?: number; residual?: number; residual_kg_s?: number; tolerance?: number; unit?: string;
  passed?: boolean; status?: string; reason?: string; error?: string; boundary_kg_s?: Record<string, number> };
export type ResultsView = {
  run_id: string;
  draft_revision: string;
  outcome: RunOutcome;
  label: "current" | "last_iterate" | "not_solved";
  value_label: string | null;
  streams: Record<string, ViewStream>;
  units: Record<string, ViewUnit>;
  boundary: { inputs: string[]; outputs: string[]; totals: { mass_flow_in: ViewQuantity; mass_flow_out: ViewQuantity;
    by_compound_in: Record<string, ViewQuantity>; by_compound_out: Record<string, ViewQuantity> } };
  balances: { mass: BalanceRow; culture: Record<string, BalanceRow> | null; unit_balances: Record<string, Record<string, BalanceRow>> | null };
  findings: Finding[];
  kpis: Kpi[];
  studio_units?: Record<string, StudioRunProvenance>;
};
export type DraftProjection = {
  workspace_id: string;
  draft_id: string;
  name: string;
  revision: string;
  seq: number;
  compounds: string[];
  property_package: string | null;
  /** Spec 183 operator solver settings (document.solver); absent means defaults. */
  solver?: Record<string, unknown> | null;
  /** Spec 173: stored typed scenarios, schedules and controllers; absent when the draft has none. Editing goes through patchDraft ops only. */
  dynamic?: { scenarios: Record<string, Record<string, unknown>>; schedules: Record<string, Record<string, unknown>>; controllers: Record<string, Record<string, unknown>> };
  objects: DraftObject[];
  reactions?: Record<string, DraftReaction>;
  kinetics_scripts?: Record<string, KineticsScript>;
  findings: Finding[];
  studio_units?: Record<string, StudioPinStatus>;
  /** Pre-run Q and HRT for a PBR fed directly by a feed stream (backend feed basis); absent otherwise. */
  pbr_feed_basis?: Record<string, PbrFeedBasis>;
  results: ResultsState;
  proposals: Proposal[];
  dwsim?: {
    run_id?: string;
    action?: string;
    status?: string;
    check_findings?: Finding[];
    solve_errors?: string[];
    failed_objects?: { tag?: string; error: string }[];
    error?: string | null;
    error_step?: string | null;
    dwsim_message?: string | null;
    outcome?: Pick<RunOutcome, "state" | "label" | "reason" | "message" | "residual" | "iterations" | "worst_tear" | "results_available">;
  } | null;
};
export type RegistryOption = string | { value: string | boolean; label: string };
export type RegistryParam = {
  key: string;
  label: string;
  kind: string;
  modes: string[];
  default: number | string | boolean | null;
  // Present for enum/bool options; quantity params use registry.quantity_units[kind].display.
  options?: RegistryOption[];
  minimum?: number | null;
  maximum?: number | null;
  classification?: "input" | "result" | "advanced";
  // Inspector grouping from the registry projection (spec 170); absent for other units.
  group?: string | null;
};
export type RegistryMode = string | { key: string; label: string };
export type RegistryUnit = {
  type: string;
  label: string;
  owner: string;
  culture_rule: string;
  inlets: string[];
  outlets: string[];
  energy_inlets?: string[];
  energy_outlets?: string[];
  energy_spec_modes?: string[];
  required_inlets: number;
  modes: RegistryMode[];
  params: RegistryParam[];
  // Reactors reference kinetic reactions (spec 158).
  reactions?: boolean;
};
export type RegistryReactionField = { key: string; label: string; kind: string; options?: RegistryOption[] };
export type RegistryReactions = {
  phases?: RegistryOption[];
  bases?: RegistryOption[];
  fields?: RegistryReactionField[];
  [key: string]: unknown;
};
/** Spec 180 closed rate-law forms; the registry projection of backend kinetics.form_table(). */
export type KineticsParam = { key?: string; symbol: string; label?: string; kind: string; domain: string; minimum: number; maximum: number; unit: string };
export type KineticsFormId = "power_law_arrhenius" | "monod" | "haldane";
export type KineticsRegistry = {
  version: string;
  forms: { id: KineticsFormId; label: string; explanation: string; parameters: (KineticsParam & { key: string })[]; equation: MathNode }[];
  inhibition: { max_terms: number; kinds: ("noncompetitive" | "competitive")[]; k_i: KineticsParam; explanation: string };
  temperature: { activation_energy: KineticsParam; reference_temperature: KineticsParam; explanation: string };
  provenance_kinds: ("literature" | "measurement" | "operator_estimate" | "synthetic")[];
  unsupported: string[];
};
export type KineticsScript = { reaction_id: string; script_title: string; native_reaction_id?: string; script_text: string | null; compilable?: boolean };
export type KineticsRecord = {
  verified: boolean; code?: string | null; reaction_id?: string; form?: string; base_reactant?: string; script_title?: string;
  native_reaction_id?: string; kinetics_version?: string; residual?: number | null; tolerance?: number | null; conversion?: number | null;
  extent_kmol_h?: number | null; rate_inlet?: number | null; rate_outlet?: number | null;
  summary?: { outlet_concentrations_kmol_m3?: Record<string, number>; residence_time_h?: number; outlet_temperature_K?: number };
  findings?: { code: string; severity: string; message: string }[];
};
export type DraftRegistry = {
  compiler_version: string;
  compounds: string[];
  property_packages: string[];
  quantity_units: Record<string, { si: string; display: string[] }>;
  pbr_t2_params?: RegistryParam[];
  stream_specs: { key: string; label: string; kind: string }[];
  units: RegistryUnit[];
  /** Spec 190: latest published process-unit version per Studio model of this workspace. */
  studio_units?: StudioPaletteUnit[];
  reactions?: RegistryReactions;
  kinetics?: KineticsRegistry;
  unsupported: Record<string, string>;
};
/** One DWSIM-reported property, captured at solve time; units verbatim from DWSIM. */
export type ResultProperty = {
  id: string;
  name: string;
  group: "conditions" | "phases" | "composition" | "thermodynamic" | "transport" | "equilibrium" | "other" | string;
  unit: string;
  value: number | string | boolean | null;
  specification: boolean;
};
export type StreamResult = {
  owner?: "dwsim" | "jarvis_bio";
  state_source?: string;
  temperature_K: number | null;
  pressure_Pa: number | null;
  mass_flow_kg_s: number | null;
  molar_flow_mol_s?: number | null;
  vapor_fraction: number | null;
  mass_fractions: Record<string, number>;
  display?: Record<string, DraftQuantity>;
  properties?: ResultProperty[];
};
export type MaterializationDiff = { path: string; expected: unknown; actual: unknown };
export type DraftRun = {
  run_id: string;
  retry_of?: string;
  retry_settings_diff?: SettingsDiffRow[];
  outcome?: RunOutcome;
  action: "validate" | "run";
  status: string;
  draft_revision: string;
  compiler_version: string;
  dwsim_version: string;
  mcp_sha256: string;
  started_at: string;
  finished_at?: string;
  expected_fingerprint?: string;
  materialization_fingerprint?: string;
  materialization_diffs?: MaterializationDiff[];
  dwsim_check?: { ready: boolean; findings: Finding[] };
  solve?: { ok: boolean; errors: unknown[]; failed_objects: { tag: string; error: string }[] };
  streams?: Record<string, StreamResult>;
  culture?: Record<string, CultureResult>;
  culture_findings?: Finding[];
  mixed_solve?: {
    status: string;
    reason?: string;
    version?: number;
    method?: string;
    solver?: Record<string, unknown>;
    diagnostics?: import("../components/process/solverSettings").SolveDiagnostics;
    culture_only?: boolean;
    history?: { iteration: number; omega: number; method?: string; q_hat?: number | null; q_hat_stable?: number | null;
      classification?: string; estimated_error_normalized?: number | null; estimate_valid?: boolean; events?: string[]; max_normalized_residual: number | null; worst_tear?: string;
      non_finite?: string; pattern_mismatch_fields?: string[];
      worst_field?: string; residuals?: Record<string, Record<string, number | null>>;
      normalized_residuals?: Record<string, Record<string, number | null>> }[];
    partition?: { segments?: { id: number; units: string[]; level: number }[]; consumed?: string[] };
    limits?: { iterations?: number; wall_s?: number; reserve_s?: number };
    failed_segment?: string;
    failed_units?: string[];
    message?: string | null;
    diagnosis?: string;
    errors?: unknown;
    elapsed_s?: Record<string, number>;
  };
  units?: Record<string, UnitResult>;
  mass_balance?: { status: string; residual_kg_s?: number; boundary_kg_s?: Record<string, number>; error?: string };
  compile_seconds?: number;
  error?: string;
  error_detail?: Record<string, unknown>;
};
export type ReportedValue = { value: string | number; units?: string; label?: string };
export type PbrCirculationOutputs = {
  circulation_flow_m3_h?: ReportedValue;
  pass_transit_time_s?: ReportedValue;
  circulation_to_throughflow_ratio?: ReportedValue;
};
/** Balance row per culture field; `generation_allowance` is the declared generation tolerance (spec 170). */
export type UnitBalanceRow = { in: number; out: number; generated?: number; residual: number; tolerance: number;
  generation_allowance?: number; unit: string; passed: boolean };
export type PbrFeedBasis = { basis: "feed"; volume_m3: number; volume_flow_m3_h: number; hrt_d: number; density_kg_m3: number; mass_flow_kg_s: number };
export type PbrModelPin = { card_id: string; card_name?: string; card_revision: string; card_digest: string; set_id?: string;
  set_name?: string; set_revision?: string; set_digest?: string; form_versions?: Record<string, string>; n_source?: string };
export type PbrNumerics = {
  map_evaluations?: number; bracket?: { value: [number, number]; units?: string };
  periodicity?: Record<string, { residual: number; tolerance: number; units?: string }>;
  z_identity_max?: { value: number; units?: string }; wall_time_s?: number;
};
export type UnitResult = {
  owner?: "dwsim" | "jarvis_bio"; calculated: boolean; error?: string; evaluator?: string; version?: number; model_version?: string;
  fidelity?: string; caveats?: string[]; findings?: Finding[]; label?: string; branch?: "productive" | "washout" | string;
  model_pin?: PbrModelPin; numerics?: PbrNumerics; unit_balances?: Record<string, UnitBalanceRow>;
  kinetics?: KineticsRecord;
  reported: Record<string, ReportedValue> & PbrCirculationOutputs; properties?: ResultProperty[];
};
export type CultureResult = {
  owner: "jarvis";
  propagation_version: string;
  status: "completed" | "failed";
  density_kg_m3?: number;
  values: Record<string, { mass_specific: number | null; si?: number; display?: DraftQuantity | null; reason?: string }>;
  unit_balances?: Record<string, { in: number; out: number; residual: number; tolerance: number; unit: "kg/s" | "mol/s"; passed: boolean }>;
  fidelity: string;
  pH_reason?: string | null;
  caveats?: string[];
  finding?: string;
  message?: string;
};
export type DraftSummary = { draft_id: string; name: string; revision: string; updated_at: string };
export type RevisionSummary = {
  seq: number;
  revision: string;
  parent_revision: string | null;
  actor: string;
  created_at: string;
  ops: string[];
};
export type DraftOp = Record<string, unknown> & { op: string };

export class DraftApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly detail: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "DraftApiError";
  }
}

const base = (workspaceId: string) => `/workspaces/${encodeURIComponent(workspaceId)}/process/drafts`;
const draftPath = (workspaceId: string, draftId: string) => `${base(workspaceId)}/${encodeURIComponent(draftId)}`;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!response.ok) {
    let detail: Record<string, unknown> = { code: `http_${response.status}`, message: `Request failed (${response.status})` };
    try {
      const body = (await response.json()) as { detail?: Record<string, unknown> };
      if (body.detail && typeof body.detail === "object") detail = body.detail;
    } catch {
      /* Keep the status-based message when the server has no JSON body. */
    }
    throw new DraftApiError(response.status, String(detail.code), String(detail.message), detail);
  }
  return response.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const getDraftRegistry = (workspaceId: string) => request<DraftRegistry>(`${base(workspaceId)}/registry`);
export const listDrafts = (workspaceId: string) => request<DraftSummary[]>(base(workspaceId));
export const createDraft = (workspaceId: string, name: string) => post<DraftProjection>(base(workspaceId), { name });
export const getDraft = (workspaceId: string, draftId: string) => request<DraftProjection>(draftPath(workspaceId, draftId));
export const patchDraft = (workspaceId: string, draftId: string, expectedRevision: string, ops: DraftOp[]) =>
  post<DraftProjection>(`${draftPath(workspaceId, draftId)}/patch`, { expected_revision: expectedRevision, ops });
export const listDraftRevisions = (workspaceId: string, draftId: string) =>
  request<RevisionSummary[]>(`${draftPath(workspaceId, draftId)}/revisions`);
export const restoreDraftRevision = (workspaceId: string, draftId: string, expectedRevision: string, sourceRevision: string) =>
  post<DraftProjection>(`${draftPath(workspaceId, draftId)}/restore`, {
    expected_revision: expectedRevision,
    source_revision: sourceRevision,
  });
export const executeDraft = (workspaceId: string, draftId: string, revision: string, action: "validate" | "run") =>
  post<{ run: DraftRun; draft: DraftProjection }>(
    `${draftPath(workspaceId, draftId)}/revisions/${encodeURIComponent(revision)}/${action}`,
  );
// Spec 210: background solves with a live iteration log, safe stop, retry and comparison.
export type SettingsDiffRow = { key: string; a: unknown; b: unknown };
export type SolveEvent = {
  seq: number; emitted_at: string; iteration: number; method?: string; omega?: number;
  max_normalized_residual: number | null; worst_tear?: string; worst_field?: string;
  q_hat?: number | null; q_hat_stable?: number | null; classification?: string;
  estimated_error_normalized?: number | null; estimate_valid?: boolean; events?: string[]; non_finite?: string;
};
export type SolveState = "running" | "stopping" | "finished" | "failed" | "interrupted";
export type SolveStatus = {
  run_id: string; draft_id: string; draft_revision: string; state: SolveState; live: boolean;
  started_at: string; finished_at?: string; stop_requested_at?: string; status?: string; reason?: string | null;
  message?: string; iterations_observable: boolean; solver?: Record<string, unknown> | null;
  retry_of?: string; retry_settings_diff?: SettingsDiffRow[]; events_truncated?: boolean;
  events: SolveEvent[]; cursor: number; stop?: "requested" | "noop";
};
export type ComparisonSide = {
  run_id: string; draft_revision: string; document_sha256: string; started_at: string; status: string; outcome: RunOutcome;
  reason?: string | null; method?: string | null; iterations: number; final_max_normalized_residual: number | null;
  results_label: string; retry_of?: string | null; residuals: { iteration: number; max_normalized_residual: number | null }[];
};
export type RunComparison = {
  a: ComparisonSide; b: ComparisonSide; same_revision: boolean; process_content_differs: boolean;
  process_content_digests: [string, string];
  settings_diff: SettingsDiffRow[]; only_in_a: string[]; only_in_b: string[];
  streams: { stream: string; fields: { field: string; unit: string; a: number | null; b: number | null; delta: number | null }[] }[];
};
export const startSolve = (workspaceId: string, draftId: string, revision: string, retryOf?: string) =>
  post<SolveStatus>(`${draftPath(workspaceId, draftId)}/solves`, { revision, ...(retryOf ? { retry_of: retryOf } : {}) });
export const getSolve = (workspaceId: string, draftId: string, runId: string, after = 0) =>
  request<SolveStatus>(`${draftPath(workspaceId, draftId)}/solves/${encodeURIComponent(runId)}?after=${after}`);
export const stopSolve = (workspaceId: string, draftId: string, runId: string) =>
  post<SolveStatus>(`${draftPath(workspaceId, draftId)}/solves/${encodeURIComponent(runId)}/stop`);
export const listSolves = (workspaceId: string, draftId: string) =>
  request<Omit<SolveStatus, "events" | "cursor" | "live">[]>(`${draftPath(workspaceId, draftId)}/solves`);
export const listDraftRuns = (workspaceId: string, draftId: string) =>
  request<Pick<DraftRun, "run_id" | "action" | "status" | "draft_revision" | "started_at" | "outcome">[]>(`${draftPath(workspaceId, draftId)}/runs`);
export const compareRuns = (workspaceId: string, draftId: string, a: string, b: string) =>
  request<RunComparison>(`${draftPath(workspaceId, draftId)}/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
export const getDraftRun = (workspaceId: string, draftId: string, runId: string) =>
  request<DraftRun>(`${draftPath(workspaceId, draftId)}/runs/${encodeURIComponent(runId)}`);
export const getRunResults = (workspaceId: string, draftId: string, runId: string) =>
  request<ResultsView>(`${draftPath(workspaceId, draftId)}/runs/${encodeURIComponent(runId)}/results`);
export const listProposals = (workspaceId: string, draftId: string) =>
  request<Proposal[]>(`${draftPath(workspaceId, draftId)}/proposals`);
export const approveProposal = (workspaceId: string, draftId: string, proposalId: string, acceptedChanges?: number[]) =>
  post<{ proposal: Proposal; draft: DraftProjection | null }>(
    `${draftPath(workspaceId, draftId)}/proposals/${encodeURIComponent(proposalId)}/approve`,
    { accepted_changes: acceptedChanges ?? null },
  );
export const rejectProposal = (workspaceId: string, draftId: string, proposalId: string) =>
  post<{ proposal: Proposal; draft: DraftProjection | null }>(
    `${draftPath(workspaceId, draftId)}/proposals/${encodeURIComponent(proposalId)}/reject`,
  );

// Draft changes made outside the Process page (Sidecar approvals) announce themselves here.
export const DRAFT_CHANGED_EVENT = "jarvis:process-draft-changed";
export const announceDraftChanged = (draftId: string) =>
  window.dispatchEvent(new CustomEvent(DRAFT_CHANGED_EVENT, { detail: { draftId } }));

export const formatQuantity = (value: unknown): string => {
  if (value == null) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "object" && "value" in (value as object) && "unit" in (value as object)) {
    const quantity = value as DraftQuantity;
    return `${quantity.value} ${unitLabel(quantity.unit)}`;
  }
  return Object.entries(value as Record<string, number>)
    .map(([name, fraction]) => `${name} ${fraction}`)
    .join(", ");
};

export const modeKey = (mode: RegistryMode) => (typeof mode === "string" ? mode : mode.key);
export const modeLabel = (mode: RegistryMode) => (typeof mode === "string" ? mode.replace(/_/g, " ") : mode.label);
export const optionValue = (option: RegistryOption) => (typeof option === "string" ? option : option.value);
export const optionLabel = (option: RegistryOption) =>
  typeof option === "string" ? option : option.label;

export const unitLabel = (unit: string) =>
  ({ degC: "°C", percent: "%", "kg/h": "kg/h", "kg/s": "kg/s", "t/h": "t/h", "kmol/[m3.h]": "kmol/(m³·h)", "mol/[m3.s]": "mol/(m³·s)",
    "mol/[L.h]": "mol/(L·h)", "mol/m3": "mol/m³", "kmol/m3": "kmol/m³" })[unit] ?? unit;
