import type { ParameterRead } from "./generated/modeling";

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export type HealthResponse = {
  status: string;
  app_name: string;
  version: string;
  environment: string;
  data_root: string;
};

export type SystemInfoResponse = {
  status: string;
  app_name: string;
  version: string;
  environment: string;
  data_root: string;
  data_root_exists: boolean;
  paths: Record<string, string>;
  database: {
    engine: string;
    database_file: string;
    configured: boolean;
    ready: boolean;
    initialized: boolean;
  };
  ai: {
    provider: string;
    gateway_configured: boolean;
    provider_configured: boolean;
    provider_calls_enabled: boolean;
    provider_mode: string;
    monthly_budget_usd: number;
    spend_month_to_date_usd: number;
    scaleway_enabled: boolean;
    scaleway_api_key_configured: boolean;
    scaleway_provider_implementation: string;
    scaleway_smoke_test_enabled: boolean;
    scaleway_live_smoke_test_enabled: boolean;
    scaleway_monthly_token_cap: number;
    scaleway_hard_stop_token_cap: number;
    scaleway_free_tier_reference_tokens: number;
    scaleway_input_tokens_month_to_date: number;
    scaleway_output_tokens_month_to_date: number;
    blocking_reason?: string | null;
  };
};

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);

  if (!response.ok) {
    throw new Error(`Request failed with ${response.status}`);
  }

  return response.json() as Promise<T>;
}

async function postJson<T>(path: string, payload: Record<string, unknown> = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  if (!response.ok) {
    throw new Error(`Request failed with ${response.status}`);
  }

  return response.json() as Promise<T>;
}

async function putJson<T>(path: string, payload: Record<string, unknown>): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  if (!response.ok) {
    throw new Error(`Request failed with ${response.status}`);
  }

  return response.json() as Promise<T>;
}

async function deleteJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "DELETE"
  });

  if (!response.ok) {
    throw new Error(`Request failed with ${response.status}`);
  }

  return response.json() as Promise<T>;
}

export function getHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>("/health");
}

export function getSystemInfo(): Promise<SystemInfoResponse> {
  return getJson<SystemInfoResponse>("/system/info");
}

export type Workspace = {
  id: string;
  name: string;
  slug: string;
  description?: string | null;
  status: string;
  created_at: string;
  updated_at: string;
};

export type ModelSpec = {
  id: string;
  workspace_id: string;
  title: string;
  engineering_question: string;
  scope?: string | null;
  status: string;
  maturity_status: string;
  schema_version: number;
  created_at: string;
  updated_at: string;
};

export type Assumption = {
  id: string;
  workspace_id: string;
  statement: string;
  confidence?: number | null;
  status: string;
};

export type Requirement = {
  id: string;
  workspace_id: string;
  statement: string;
  rationale?: string | null;
  status: string;
  notes?: string | null;
  schema_version: number;
  created_at: string;
  updated_at: string;
};

export type Parameter = ParameterRead;

export type SimulationRun = {
  id: string;
  workspace_id: string;
  model_version_id?: string | null;
  run_label?: string | null;
  status: string;
  created_at: string;
};

export type Decision = {
  id: string;
  workspace_id: string;
  title: string;
  decision_text: string;
  rationale?: string | null;
  status: string;
};

export function initializeSystem(): Promise<unknown> {
  return postJson<unknown>("/system/initialize");
}

export function listWorkspaces(options: { includeArchived?: boolean } = {}): Promise<Workspace[]> {
  if (options.includeArchived) return getJson<Workspace[]>("/workspaces?include_archived=true");
  return getJson<Workspace[]>("/workspaces");
}

export function archiveWorkspace(workspaceId: string): Promise<Workspace> {
  return postJson<Workspace>(`/workspaces/${encodeURIComponent(workspaceId)}/archive`);
}

export function restoreWorkspace(workspaceId: string): Promise<Workspace> {
  return postJson<Workspace>(`/workspaces/${encodeURIComponent(workspaceId)}/restore`);
}

export function renameWorkspace(workspaceId: string, name: string): Promise<Workspace> {
  return postJson<Workspace>(`/workspaces/${encodeURIComponent(workspaceId)}/rename`, { name });
}

export function createWorkspace(payload: Record<string, unknown>): Promise<Workspace> {
  return postJson<Workspace>("/workspaces", payload);
}

export function listModelSpecs(workspaceId: string): Promise<ModelSpec[]> {
  return getJson<ModelSpec[]>(`/workspaces/${workspaceId}/model-specs`);
}

export function createModelSpec(workspaceId: string, payload: Record<string, unknown>): Promise<ModelSpec> {
  return postJson<ModelSpec>(`/workspaces/${workspaceId}/model-specs`, payload);
}

export function listAssumptions(workspaceId: string): Promise<Assumption[]> {
  return getJson<Assumption[]>(`/workspaces/${workspaceId}/assumptions`);
}

export function createAssumption(workspaceId: string, payload: Record<string, unknown>): Promise<Assumption> {
  return postJson<Assumption>(`/workspaces/${workspaceId}/assumptions`, payload);
}

export function listRequirements(workspaceId: string): Promise<Requirement[]> {
  return getJson<Requirement[]>(`/workspaces/${workspaceId}/requirements`);
}

export function listParameters(workspaceId: string): Promise<Parameter[]> {
  return getJson<Parameter[]>(`/workspaces/${workspaceId}/parameters`);
}

export function createParameter(workspaceId: string, payload: Record<string, unknown>): Promise<Parameter> {
  return postJson<Parameter>(`/workspaces/${workspaceId}/parameters`, payload);
}

export function listSimulationRuns(workspaceId: string): Promise<SimulationRun[]> {
  return getJson<SimulationRun[]>(`/workspaces/${workspaceId}/simulation-runs`);
}

export function createSimulationRun(workspaceId: string, payload: Record<string, unknown>): Promise<SimulationRun> {
  return postJson<SimulationRun>(`/workspaces/${workspaceId}/simulation-runs`, payload);
}

export function listDecisions(workspaceId: string): Promise<Decision[]> {
  return getJson<Decision[]>(`/workspaces/${workspaceId}/decisions`);
}

export function createDecision(workspaceId: string, payload: Record<string, unknown>): Promise<Decision> {
  return postJson<Decision>(`/workspaces/${workspaceId}/decisions`, payload);
}

export type AISettings = {
  policy_mode: string;
  monthly_api_budget_usd: number;
  api_spend_month_to_date_usd: number;
  paid_ai_enabled: boolean;
  default_ai_provider: string;
  default_ai_model: string;
  provider_mode: string;
  use_fake_provider_when_budget_zero: boolean;
  scaleway_enabled: boolean;
  scaleway_smoke_test_enabled: boolean;
  scaleway_live_smoke_test_enabled: boolean;
  scaleway_monthly_token_cap: number;
  scaleway_hard_stop_token_cap: number;
  scaleway_free_tier_reference_tokens: number;
  scaleway_input_tokens_month_to_date: number;
  scaleway_output_tokens_month_to_date: number;
  usage_total_tokens: number;
  smoke_test_mode_enabled: boolean;
  max_direct_continuations: number;
  max_direct_continuations_min: number;
  max_direct_continuations_max: number;
  direct_continuation_policy_version: string;
  updated_at: string;
};

export type AIStatus = {
  policy_mode: string;
  ai_enabled: boolean;
  active_provider_mode: string;
  provider_mode: string;
  provider_id: string;
  adapter_enabled: boolean;
  fake_provider_enabled: boolean;
  scaleway_enabled: boolean;
  scaleway_api_key_configured: boolean;
  scaleway_provider_implementation: string;
  paid_ai_enabled: boolean;
  monthly_api_budget_usd: number;
  spend_month_to_date_usd: number;
  scaleway_smoke_test_enabled: boolean;
  scaleway_live_smoke_test_enabled: boolean;
  scaleway_monthly_token_cap: number;
  scaleway_hard_stop_token_cap: number;
  scaleway_free_tier_reference_tokens: number;
  scaleway_input_tokens_month_to_date: number;
  scaleway_output_tokens_month_to_date: number;
  usage_total_tokens: number;
  budget_status: string;
  credential_status: string;
  external_calls_allowed: boolean;
  blocking_reason?: string | null;
  default_ai_provider: string;
  default_ai_model: string;
};

export type ModelingDraftResponse = {
  draft: {
    engineering_question: string;
    model_title_suggestion: string;
    model_scope: string;
    proposed_assumptions: string[];
    proposed_parameters: string[];
    expected_inputs: string[];
    expected_outputs: string[];
    missing_information: string[];
    model_weaknesses: string[];
    suggested_next_step: string;
  } | null;
  ai_metadata: {
    provider: string;
    model: string;
    provider_mode: string;
    task_type: string;
    quality_level: string;
    paid_api_call_attempted: boolean;
    blocked_by_budget: boolean;
    blocked_reason?: string | null;
    estimated_cost_usd?: number | null;
    monthly_budget_usd: number;
    spend_month_to_date_usd: number;
    success: boolean;
  };
};

export function getAISettings(): Promise<AISettings> {
  return getJson<AISettings>("/ai/settings");
}

export function updateAISettings(payload: Record<string, unknown>): Promise<AISettings> {
  return putJson<AISettings>("/ai/settings", payload);
}

export function getAIStatus(): Promise<AIStatus> {
  return getJson<AIStatus>("/ai/status");
}

export type ScalewaySecretStatus = {
  key_present: boolean;
  source: string;
  masked_preview?: string | null;
  last_updated_at?: string | null;
  storage_mode: string;
};

export function getScalewaySecretStatus(): Promise<ScalewaySecretStatus> {
  return getJson<ScalewaySecretStatus>("/secrets/scaleway/status");
}

export function setScalewayApiKey(apiKey: string): Promise<ScalewaySecretStatus> {
  return postJson<ScalewaySecretStatus>("/secrets/scaleway/api-key", { api_key: apiKey });
}

export function deleteScalewayApiKey(): Promise<ScalewaySecretStatus> {
  return deleteJson<ScalewaySecretStatus>("/secrets/scaleway/api-key");
}

export function createModelingDraft(payload: Record<string, unknown>): Promise<ModelingDraftResponse> {
  return postJson<ModelingDraftResponse>("/ai/modeling/draft", payload);
}

export type AITaskRunRequest = {
  prompt: string;
  route_class?: string;
  task_kind?: string;
  max_tokens?: number;
  context_blocks?: unknown[];
  include_project_context?: boolean;
  workspace_id?: string;
};

export type AIUsage = {
  provider_id: string;
  model_id: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens?: number | null;
  usage_source: string;
  provider_cost_estimate?: number | null;
  currency?: string | null;
};

export type EscalationProposal = {
  proposal_ledger_id?: string | null;
  proposed_route_class: string;
  provider_id?: string | null;
  model_id?: string | null;
  estimated_cost?: {
    estimated_cost_usd?: number;
    currency?: string;
    input_tokens?: number;
    max_output_tokens?: number;
    label?: string;
  };
  outbound_text: string;
  context_excluded: boolean;
  sensitivity_warning?: string | null;
};

export type AITaskRunResponse = {
  status: string;
  ledger_id: string;
  selected_route_class?: string | null;
  decision_reason: string;
  blocked_reason?: string | null;
  response_text?: string | null;
  provider_id?: string | null;
  model_id?: string | null;
  usage?: AIUsage | null;
  error_type?: string | null;
  error_message?: string | null;
  confirmation_payload?: unknown | null;
  escalation_proposal?: EscalationProposal | null;
  include_project_context?: boolean;
  workspace_id?: string | null;
  context_digest?: string | null;
  context_sources_count?: number;
};

export function runAITask(payload: AITaskRunRequest): Promise<AITaskRunResponse> {
  return postJson<AITaskRunResponse>("/ai/tasks/run", payload as Record<string, unknown>);
}

export type EscalationConfirmResponse = {
  status: string;
  proposal_ledger_id: string;
  execution_ledger_id: string;
  task_response: AITaskRunResponse;
};

export function confirmAITaskEscalation(proposal: EscalationProposal, taskKind = "general"): Promise<EscalationConfirmResponse> {
  return postJson<EscalationConfirmResponse>("/ai/tasks/escalations/confirm", { proposal, task_kind: taskKind });
}

export type SmokeTestTokenMetadata = {
  blocked_by_token_cap: boolean;
  estimated_input_tokens: number;
  estimated_output_tokens: number;
  reported_input_tokens?: number | null;
  reported_output_tokens?: number | null;
  monthly_token_cap: number;
  hard_stop_token_cap: number;
  token_usage_month_to_date: number;
  usage_source: string;
};

export type SmokeTestResult = {
  case_id: string;
  input_excerpt: string;
  expected_class: string;
  local_privacy_class: string;
  provider_reported_class?: string | null;
  fake_classification?: string | null;
  passed: boolean;
  provider_mode: string;
  provider: string;
  smoke_mode: string;
  external_call_attempted: boolean;
  external_call_succeeded: boolean;
  blocking_reason?: string | null;
  response_text?: string | null;
  usage_source: string;
  provider_metadata?: Record<string, unknown> | null;
  token_metadata: SmokeTestTokenMetadata;
};

export type SmokeTestResponse = {
  provider_mode: string;
  smoke_mode: string;
  external_call_attempted: boolean;
  external_call_succeeded: boolean;
  results: SmokeTestResult[];
};

export function runAISmokeTests(payload: Record<string, unknown>): Promise<SmokeTestResponse> {
  return postJson<SmokeTestResponse>("/ai/smoke-tests/run", payload);
}

export type SmokeConsoleResponse = {
  response_text?: string | null;
  provider: string;
  model: string;
  mode: string;
  privacy_class: string;
  blocked_reason?: string | null;
  external_call_attempted: boolean;
  external_call_succeeded: boolean;
  estimated_input_tokens: number;
  estimated_output_tokens: number;
  actual_input_tokens?: number | null;
  actual_output_tokens?: number | null;
  usage_source: string;
  current_month_input_tokens: number;
  current_month_output_tokens: number;
  current_month_total_tokens: number;
  configured_monthly_token_cap: number;
  token_threshold: number;
  token_threshold_percent: number;
  remaining_tokens_to_threshold: number;
};

export function runAISmokeConsole(payload: Record<string, unknown>): Promise<SmokeConsoleResponse> {
  return postJson<SmokeConsoleResponse>("/ai/smoke-console/run", payload);
}


export type BluecadAttempt = {
  id: string;
  candidate_id: string;
  attempt_no: number;
  route_class: string;
  proposal_ai_job_id?: string | null;
  proposal_outcome: string;
  build_outcome?: string | null;
  validation_verdict?: string | null;
  spec_artifact_id?: string | null;
  report_artifact_id?: string | null;
  manifest_artifact_id?: string | null;
  started_at: string;
  finished_at?: string | null;
  error_detail_json?: string | null;
};

export type BluecadCandidate = {
  id: string;
  workspace_id: string;
  brief_text: string;
  brief_digest: string;
  status: string;
  parked_reason?: string | null;
  spec_artifact_id?: string | null;
  glb_artifact_id?: string | null;
  report_artifact_id?: string | null;
  promoted_decision_id?: string | null;
  origin: string;
  parent_candidate_id?: string | null;
  loop_config_json: string;
  created_at: string;
  updated_at: string;
  notes?: string | null;
  attempts: BluecadAttempt[];
};

export type BluecadArtifactRefRead = {
  id: string;
  roles: string[];
  filename: string;
  mime_type: string;
  sha256: string;
  status: string;
  source_ref?: string | null;
  created_at: string;
  content_url: string;
};

export type BluecadEvidenceRefRead = {
  ref: string;
  kind: string;
  subject_ref: string;
  status: string;
  stale?: boolean | null;
  created_at?: string | null;
  summary?: string | null;
};

export type BluecadRunRefRead = {
  ref: string;
  kind: "simulation_run" | "runner_job";
  status?: string | null;
  stale?: boolean | null;
  created_at?: string | null;
  source_ref?: string | null;
};

export type BluecadReadDiagnostic = {
  code: "missing_reference" | "malformed_reference" | "inaccessible_reference" | "unsupported_reference";
  source: string;
  reference: string;
  message: string;
};

export type BluecadCandidateAggregateRead = {
  candidate: BluecadCandidate;
  artifacts: BluecadArtifactRefRead[];
  /** STL (mm), STEP and 3MF (mm) exports built with the candidate's current GLB; roles export.stl / export.step / export.3mf. */
  exports: BluecadArtifactRefRead[];
  evidence: BluecadEvidenceRefRead[];
  runs: BluecadRunRefRead[];
  freshness: "fresh" | "stale" | "unknown" | "mixed";
  diagnostics: BluecadReadDiagnostic[];
};

export type BluecadValidationCheck = {
  id?: string;
  check_id?: string;
  tier?: string | number;
  status?: string;
  verdict?: string;
  detail?: unknown;
  message?: unknown;
  hint?: string | null;
};

export function listBluecadCandidates(workspaceId: string): Promise<BluecadCandidate[]> {
  return getJson<BluecadCandidate[]>(`/workspaces/${workspaceId}/bluecad/candidates`);
}

export function createBluecadCandidate(workspaceId: string, briefText: string): Promise<BluecadCandidate> {
  return postJson<BluecadCandidate>(`/workspaces/${workspaceId}/bluecad/candidates`, { brief_text: briefText });
}

export type BluecadTemplateCreate =
  | { template: "tube"; params: { outer_d_mm: number; wall_t_mm: number; length_mm: number } }
  | {
      template: "manifold";
      params: { outer_d_mm: number; wall_t_mm: number; length_mm: number; branch_count: number; branch_outer_d_mm: number };
    };

export function createBluecadTemplateCandidate(workspaceId: string, payload: BluecadTemplateCreate): Promise<BluecadCandidate> {
  return postJson<BluecadCandidate>(`/workspaces/${workspaceId}/bluecad/candidates/from-template`, payload);
}

export type BluecadGenerationAvailability = {
  route_class: string;
  external_calls_allowed: boolean;
  blocking_reason?: string | null;
};

export function getBluecadGenerationAvailability(workspaceId: string): Promise<BluecadGenerationAvailability> {
  return getJson<BluecadGenerationAvailability>(`/workspaces/${encodeURIComponent(workspaceId)}/bluecad/generation-availability`);
}

export function archiveBluecadCandidate(workspaceId: string, candidateId: string): Promise<BluecadCandidate> {
  return postJson<BluecadCandidate>(`/workspaces/${workspaceId}/bluecad/candidates/${candidateId}/archive`);
}

export function promoteBluecadCandidate(workspaceId: string, candidateId: string): Promise<BluecadCandidate> {
  return postJson<BluecadCandidate>(`/workspaces/${workspaceId}/bluecad/candidates/${candidateId}/promote`);
}

export function getBluecadCandidateAggregate(
  workspaceId: string,
  candidateId: string
): Promise<BluecadCandidateAggregateRead> {
  const encodedWorkspaceId = encodeURIComponent(workspaceId);
  const encodedCandidateId = encodeURIComponent(candidateId);
  return getJson<BluecadCandidateAggregateRead>(
    `/workspaces/${encodedWorkspaceId}/bluecad/candidates/${encodedCandidateId}/aggregate`
  );
}

export type BluecadPrintHandoffCapability = { available: boolean; code: string | null };

export type BluecadPrintHandoffEvent = {
  id: string;
  workspace_id: string;
  artifact_id: string;
  sha256: string;
  handoff_path: string;
  windows_path: string;
  outcome: string;
  created_at: string;
  message: string;
};

export function getBluecadPrintHandoffCapability(workspaceId: string): Promise<BluecadPrintHandoffCapability> {
  return getJson<BluecadPrintHandoffCapability>(
    `/workspaces/${encodeURIComponent(workspaceId)}/bluecad/print-handoff/capability`
  );
}

export async function openBluecadArtifactInSlicer(
  workspaceId: string,
  artifactId: string
): Promise<BluecadPrintHandoffEvent> {
  const response = await fetch(
    `${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/bluecad/artifacts/${encodeURIComponent(artifactId)}/open-in-slicer`,
    { method: "POST" }
  );
  if (!response.ok) {
    let detail = `Request failed with ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: { code?: string; message?: string } };
      if (body.detail?.message) detail = body.detail.message;
    } catch {
      // keep the status-only message
    }
    throw new Error(detail);
  }
  return response.json() as Promise<BluecadPrintHandoffEvent>;
}

export function bluecadArtifactContentUrl(workspaceId: string, artifactId: string): string {
  return `${API_BASE_URL}/workspaces/${workspaceId}/bluecad/artifacts/${artifactId}/content`;
}

export type BluecadStudioReference = {
  id: string; workspace_id: string; candidate_id: string; simulation_run_id: string;
  model_spec_id: string; model_version_id: string; version_label: string;
  source_sha256: string; contract_sha256: string; glb_sha256: string;
  step_sha256: string; stl_sha256: string;
  inputs: Record<string, { value: number; unit: string }>;
  translation_mm: [number, number, number]; rotation_deg: [number, number, number];
  revision: number; valid: boolean; stale: boolean; superseded: boolean;
  finding: string | null; glb_url: string | null;
  scene_id: string; visual_only: boolean; placement_sha256: string;
  provenance: { frame: string };
};

const studioReferencePath = (workspaceId: string, candidateId: string) =>
  `/workspaces/${encodeURIComponent(workspaceId)}/bluecad/candidates/${encodeURIComponent(candidateId)}/studio-references`;

export const listBluecadStudioReferences = (workspaceId: string, candidateId: string) =>
  getJson<BluecadStudioReference[]>(studioReferencePath(workspaceId, candidateId));

export const addBluecadStudioReference = (workspaceId: string, candidateId: string, simulationRunId: string,
  placement: { translation_mm: [number, number, number]; rotation_deg: [number, number, number] } | null = null) =>
  postJson<BluecadStudioReference>(studioReferencePath(workspaceId, candidateId), { simulation_run_id: simulationRunId, ...(placement ?? {}) });

export const moveBluecadStudioReference = (reference: BluecadStudioReference,
  translation_mm: [number, number, number], rotation_deg: [number, number, number]) =>
  putJson<BluecadStudioReference>(`${studioReferencePath(reference.workspace_id, reference.candidate_id)}/${encodeURIComponent(reference.id)}`,
    { expected_revision: reference.revision, translation_mm, rotation_deg });

export const removeBluecadStudioReference = async (reference: BluecadStudioReference) => {
  const response = await fetch(`${API_BASE_URL}${studioReferencePath(reference.workspace_id, reference.candidate_id)}/${encodeURIComponent(reference.id)}?expected_revision=${reference.revision}`, { method: "DELETE" });
  if (!response.ok) throw new Error(`Reference removal failed: ${response.status}`);
};

export async function getBluecadArtifactJson<T>(workspaceId: string, artifactId: string): Promise<T> {
  const response = await fetch(bluecadArtifactContentUrl(workspaceId, artifactId));
  if (!response.ok) {
    throw new Error(`Request failed with ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export type ModelInputVariable = {
  name: string;
  label: string;
  unit: string;
  required: boolean;
  category: "design" | "operating" | "property" | "model_parameter" | "equipment";
  description: string;
  domain?: Record<string, number> | null;
};

export type ModelInputContract = {
  schema_version: 1;
  evaluation_mode: "forward";
  variables: ModelInputVariable[];
};

export type ModelImplementation = {
  id: string;
  workspace_id: string;
  model_spec_id: string;
  version_label: string;
  implementation_artifact_id: string;
  status: string;
  implementation_kind?: string | null;
  script_sha256: string;
  script_path: string;
  created_at: string;
  notes?: string | null;
  input_contract?: ModelInputContract | null;
  input_contract_sha256?: string | null;
};

export type BindingVariablePreview = ModelInputVariable & {
  binding_state: "missing" | "manual" | "parameter" | "invalid";
  value?: number | null;
  source_parameter_id?: string | null;
  errors: string[];
};

export type BindingPreviewResponse = {
  model_version_id: string;
  contract_sha256: string;
  evaluation_mode: "forward";
  structural_input_dof: number;
  bound_input_dof: number;
  unresolved_input_dof: number;
  invalid_binding_count: number;
  state: "incomplete" | "ready" | "invalid";
  variables: BindingVariablePreview[];
  errors: string[];
  normalized_input_set?: Record<string, Record<string, unknown>> | null;
};

export type RunnerJobCreateResponse = {
  runner_job: { id: string; status: string };
  simulation_run: SimulationRun & { input_payload?: string | null };
};

export type RunnerJobRunResponse = {
  runner_job: { id: string; status: string };
  simulation_run: SimulationRun & {
    output_payload?: string | null;
    completed_at?: string | null;
  };
  output?: {
    outputs?: Record<string, { value: unknown; unit: string }>;
    diagnostics?: Record<string, unknown>;
  } | null;
  error?: { code: string; message: string } | null;
};

export function listModelImplementations(workspaceId: string): Promise<ModelImplementation[]> {
  return getJson<ModelImplementation[]>(`/workspaces/${workspaceId}/model-implementations`);
}

export function previewModelBindings(
  workspaceId: string,
  modelVersionId: string,
  bindings: Record<string, Record<string, unknown>>
): Promise<BindingPreviewResponse> {
  return postJson<BindingPreviewResponse>(
    `/workspaces/${workspaceId}/model-implementations/${modelVersionId}/binding-preview`,
    { bindings }
  );
}

export function createRunnerJob(
  workspaceId: string,
  payload: Record<string, unknown>
): Promise<RunnerJobCreateResponse> {
  return postJson<RunnerJobCreateResponse>(`/workspaces/${workspaceId}/runner-jobs`, payload);
}

export function runRunnerJob(runnerJobId: string): Promise<RunnerJobRunResponse> {
  return postJson<RunnerJobRunResponse>(`/runner-jobs/${runnerJobId}/run`);
}
