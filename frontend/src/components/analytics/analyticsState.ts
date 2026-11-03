import type { ModelImplementation } from "../../api/client";
import type { SimulationRunSummary } from "../../api/runs";
import { reconstructPreviousRunBaseline, type PreviousRunBaseline } from "../engineering/previousRunLoad";

export type AnalyticsRun = Readonly<{
  id: string;
  model_version_id: string | null;
  run_label: string | null;
  status: string;
  output_payload: string | null;
}>;

export type RequestContext = Readonly<{ generation: number; identity: string }>;

export type Observation = Readonly<{ key: string; value: number; unit: string }>;
export type RunProjection = Readonly<{
  runId: string;
  label: string;
  modelVersionId: string | null;
  state: "ok" | "rejected";
  reason: string | null;
  observations: Readonly<Record<string, Observation>>;
  rejectedKeys: Readonly<Record<string, string>>;
}>;
export type ComparisonGroup =
  | Readonly<{ state: "valid"; key: string; unit: string; values: ReadonlyArray<Readonly<{ runId: string; label: string; value: number }>>; min: number; max: number; range: number }>
  | Readonly<{ state: "rejected"; key: string; reason: string }>;
export type ComparisonResult = Readonly<{
  state: "instruction" | "rejected" | "ready";
  message: string | null;
  projections: readonly RunProjection[];
  groups: readonly ComparisonGroup[];
}>;

export type ConfigurationCell = Readonly<{
  runId: string;
  runLabel: string;
  value: number | null;
  displayValue: string;
  delta: number | null;
}>;
export type ConfigurationRow = Readonly<{
  name: string;
  label: string;
  unit: string;
  cells: readonly ConfigurationCell[];
}>;
export type ConfigurationModelChoice = Readonly<{
  familyKey: string;
  familyLabel: string;
  optionLabel: string;
}>;
export type ConfigurationComparison = Readonly<{
  state: "instruction" | "rejected" | "ready";
  message: string | null;
  baselineRunId: string | null;
  modelChoice: ConfigurationModelChoice | null;
  rows: readonly ConfigurationRow[];
}>;

type ConfigurationVariable = Readonly<{
  name: string;
  label: string;
  unit: string;
}>;

export const MAX_SELECTED_RUNS = 6;
export const MAX_OUTPUT_KEYS = 128;
export const MAX_OUTPUT_KEY_LENGTH = 160;
export const MAX_UNIT_LENGTH = 64;
export const MAX_OUTPUT_PAYLOAD_BYTES = 1_048_576;
export const MAX_CONFIGURATION_VARIABLES = 128;
export const MAX_CONFIGURATION_NAME_LENGTH = 160;
export const MAX_CONFIGURATION_PAYLOAD_BYTES = 1_048_576;

export function acceptsWorkspaceResponse(request: RequestContext, currentGeneration: number, currentWorkspace: string): boolean {
  return request.generation === currentGeneration && request.identity === currentWorkspace;
}

function codePointLength(value: string): number {
  return Array.from(value).length;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function labelFor(run: AnalyticsRun): string {
  const label = run.run_label?.trim();
  return Array.from(label || run.id).slice(0, 160).join("");
}

function rejected(run: AnalyticsRun, reason: string): RunProjection {
  return { runId: run.id, label: labelFor(run), modelVersionId: run.model_version_id, state: "rejected", reason, observations: {}, rejectedKeys: {} };
}

export function projectAnalyticsRun(run: AnalyticsRun): RunProjection {
  if (run.status !== "succeeded") return rejected(run, `Run status is ${run.status}; only succeeded runs provide numeric observations.`);
  if (!run.model_version_id || run.model_version_id.trim().length === 0) return rejected(run, "Model version identity is missing.");
  if (run.output_payload === null) return rejected(run, "Persisted output payload is missing.");
  if (new TextEncoder().encode(run.output_payload).length > MAX_OUTPUT_PAYLOAD_BYTES) return rejected(run, "Persisted output payload exceeds the 1 MiB analytics limit.");

  let parsed: unknown;
  try {
    parsed = JSON.parse(run.output_payload) as unknown;
  } catch {
    return rejected(run, "Persisted output payload is malformed JSON.");
  }
  if (!isRecord(parsed) || parsed.schema_version !== 1 || parsed.status !== "succeeded" || !isRecord(parsed.outputs)) {
    return rejected(run, "Persisted output payload is not a supported succeeded schema-v1 result envelope.");
  }

  const entries = Object.entries(parsed.outputs);
  if (entries.length > MAX_OUTPUT_KEYS) return rejected(run, `Persisted output payload exceeds ${MAX_OUTPUT_KEYS} output keys.`);

  const observations: Record<string, Observation> = {};
  const rejectedKeys: Record<string, string> = {};
  for (const [key, raw] of entries) {
    if (key.trim().length === 0 || codePointLength(key) > MAX_OUTPUT_KEY_LENGTH) return rejected(run, "Persisted output key is empty or exceeds the bounded key length.");
    if (!isRecord(raw)) {
      rejectedKeys[key] = "Output is not a scalar result record.";
      continue;
    }
    const value = raw.value;
    const unit = raw.unit;
    if (typeof value !== "number" || !Number.isFinite(value)) {
      rejectedKeys[key] = "Output value is not a finite numeric value.";
      continue;
    }
    if (typeof unit !== "string" || unit.trim().length === 0 || codePointLength(unit) > MAX_UNIT_LENGTH) {
      rejectedKeys[key] = "Output unit is missing or exceeds the bounded unit length.";
      continue;
    }
    observations[key] = { key, value, unit };
  }

  if (Object.keys(observations).length === 0 && Object.keys(rejectedKeys).length === 0) {
    return rejected(run, "Succeeded run has no trustworthy unit-bearing scalar outputs.");
  }
  return { runId: run.id, label: labelFor(run), modelVersionId: run.model_version_id, state: "ok", reason: null, observations, rejectedKeys };
}

export function retainExistingSelection(selectedIds: readonly string[], runs: readonly AnalyticsRun[]): string[] {
  const present = new Set(runs.map((run) => run.id));
  return selectedIds.filter((id) => present.has(id)).slice(0, MAX_SELECTED_RUNS);
}

export function toggleRunSelection(selectedIds: readonly string[], runId: string): string[] {
  if (selectedIds.includes(runId)) return selectedIds.filter((id) => id !== runId);
  if (selectedIds.length >= MAX_SELECTED_RUNS) return [...selectedIds];
  return [...selectedIds, runId];
}

export function normalizeBaselineRunId(selectedIds: readonly string[], currentBaselineRunId: string | null): string | null {
  if (currentBaselineRunId && selectedIds.includes(currentBaselineRunId)) return currentBaselineRunId;
  return selectedIds[0] ?? null;
}

export function compareAnalyticsRuns(runs: readonly AnalyticsRun[]): ComparisonResult {
  const projections = runs.map(projectAnalyticsRun);
  if (runs.length === 0) return { state: "instruction", message: "Select at least two persisted runs to compare.", projections, groups: [] };
  if (runs.length === 1) return { state: "instruction", message: "Select one more persisted run to compare.", projections, groups: [] };
  if (runs.length > MAX_SELECTED_RUNS) return { state: "rejected", message: `A comparison may include at most ${MAX_SELECTED_RUNS} runs.`, projections, groups: [] };

  const rejectedRuns = projections.filter((projection) => projection.state === "rejected");
  if (rejectedRuns.length > 0) {
    return { state: "rejected", message: rejectedRuns.map((run) => `${run.label}: ${run.reason}`).join(" · "), projections, groups: [] };
  }

  const versions = new Set(projections.map((projection) => projection.modelVersionId));
  if (versions.size !== 1) return { state: "rejected", message: "Direct comparison requires one exact model version across all selected runs.", projections, groups: [] };

  const keys = Array.from(new Set(projections.flatMap((projection) => [...Object.keys(projection.observations), ...Object.keys(projection.rejectedKeys)]))).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const groups: ComparisonGroup[] = keys.map((key) => {
    const invalid = projections.find((projection) => projection.rejectedKeys[key]);
    if (invalid) return { state: "rejected", key, reason: `${invalid.label}: ${invalid.rejectedKeys[key]}` };
    const missing = projections.find((projection) => !projection.observations[key]);
    if (missing) return { state: "rejected", key, reason: `${missing.label}: metric is missing from the selected run.` };
    const observations = projections.map((projection) => projection.observations[key]);
    const units = new Set(observations.map((observation) => observation.unit));
    if (units.size !== 1) return { state: "rejected", key, reason: `Exact units are incompatible: ${Array.from(units).join(" vs ")}. No conversion was applied.` };
    const values = observations.map((observation, index) => ({ runId: projections[index].runId, label: projections[index].label, value: observation.value }));
    const numeric = values.map((item) => item.value);
    const min = Math.min(...numeric);
    const max = Math.max(...numeric);
    return { state: "valid", key, unit: observations[0].unit, values, min, max, range: max - min };
  });

  if (groups.length === 0) return { state: "rejected", message: "Selected runs contain no trustworthy comparable observations.", projections, groups: [] };
  return { state: "ready", message: null, projections, groups };
}

function configurationVariables(implementation: ModelImplementation): ConfigurationVariable[] | null {
  const rawContract = implementation.input_contract as unknown;
  if (!isRecord(rawContract) || !Array.isArray(rawContract.variables) || !implementation.input_contract_sha256?.trim()) return null;
  if (rawContract.variables.length > MAX_CONFIGURATION_VARIABLES) return null;

  const variables: ConfigurationVariable[] = [];
  const names = new Set<string>();
  for (const rawVariable of rawContract.variables) {
    if (!isRecord(rawVariable)) return null;
    const name = rawVariable.name;
    const label = rawVariable.label;
    const unit = rawVariable.unit;
    const required = rawVariable.required;
    if (
      typeof name !== "string" ||
      name.trim().length === 0 ||
      codePointLength(name) > MAX_CONFIGURATION_NAME_LENGTH ||
      names.has(name) ||
      typeof unit !== "string" ||
      unit.trim().length === 0 ||
      codePointLength(unit) > MAX_UNIT_LENGTH ||
      typeof required !== "boolean"
    ) return null;
    if (label !== undefined && (typeof label !== "string" || codePointLength(label) > MAX_CONFIGURATION_NAME_LENGTH)) return null;
    names.add(name);
    variables.push({ name, label: typeof label === "string" && label.trim() ? label.trim() : name, unit });
  }
  return variables;
}

function configurationModelChoice(implementation: ModelImplementation): ConfigurationModelChoice | null {
  const rawContract = implementation.input_contract as unknown;
  if (!isRecord(rawContract) || rawContract.schema_version !== 3 || !isRecord(rawContract.semantic_context)) return null;
  const context = rawContract.semantic_context;
  if (
    !Array.isArray(context.applicable_part_kinds) ||
    context.applicable_part_kinds.length > MAX_CONFIGURATION_VARIABLES ||
    !context.applicable_part_kinds.every((item) => typeof item === "string" && item.trim().length > 0 && codePointLength(item) <= MAX_CONFIGURATION_NAME_LENGTH) ||
    typeof context.model_family_key !== "string" ||
    context.model_family_key.trim().length === 0 ||
    codePointLength(context.model_family_key) > MAX_CONFIGURATION_NAME_LENGTH ||
    typeof context.model_family_label !== "string" ||
    context.model_family_label.trim().length === 0 ||
    codePointLength(context.model_family_label) > MAX_CONFIGURATION_NAME_LENGTH ||
    typeof context.model_option_label !== "string" ||
    context.model_option_label.trim().length === 0 ||
    codePointLength(context.model_option_label) > MAX_CONFIGURATION_NAME_LENGTH
  ) return null;
  return {
    familyKey: context.model_family_key,
    familyLabel: context.model_family_label,
    optionLabel: context.model_option_label
  };
}

function configurationReject(message: string, baselineRunId: string | null): ConfigurationComparison {
  return { state: "rejected", message, baselineRunId, modelChoice: null, rows: [] };
}

export function compareEngineeringConfigurations(
  workspaceId: string,
  runs: readonly SimulationRunSummary[],
  implementations: readonly ModelImplementation[],
  requestedBaselineRunId: string | null
): ConfigurationComparison {
  const selectedIds = runs.map((run) => run.id);
  const baselineRunId = normalizeBaselineRunId(selectedIds, requestedBaselineRunId);
  if (runs.length === 0) return { state: "instruction", message: "Select at least two persisted runs to compare engineering configuration.", baselineRunId, modelChoice: null, rows: [] };
  if (runs.length === 1) return { state: "instruction", message: "Select one more persisted run to compare engineering configuration.", baselineRunId, modelChoice: null, rows: [] };
  if (runs.length > MAX_SELECTED_RUNS) return configurationReject(`A comparison may include at most ${MAX_SELECTED_RUNS} runs.`, baselineRunId);

  const modelVersionId = runs[0].model_version_id;
  if (!modelVersionId || runs.some((run) => run.workspace_id !== workspaceId || run.status !== "succeeded" || run.model_version_id !== modelVersionId)) {
    return configurationReject("Engineering configuration requires succeeded runs from this workspace with one exact model version.", baselineRunId);
  }

  const implementation = implementations.find((item) => item.id === modelVersionId && item.workspace_id === workspaceId);
  if (!implementation) return configurationReject("Engineering configuration is unavailable because the exact model version is not available.", baselineRunId);
  const variables = configurationVariables(implementation);
  if (!variables) return configurationReject("Engineering configuration is unavailable because the exact input contract is missing, malformed, or exceeds comparison bounds.", baselineRunId);

  const baselines = new Map<string, PreviousRunBaseline>();
  for (const run of runs) {
    if (run.input_payload === null || new TextEncoder().encode(run.input_payload).length > MAX_CONFIGURATION_PAYLOAD_BYTES) {
      return configurationReject(`${labelFor(run)}: persisted input snapshot is missing or exceeds the 1 MiB comparison limit.`, baselineRunId);
    }
    const reconstructed = reconstructPreviousRunBaseline(workspaceId, run, implementations);
    if (!reconstructed.loadable) return configurationReject(`${labelFor(run)}: ${reconstructed.reason}.`, baselineRunId);
    baselines.set(run.id, reconstructed.baseline);
  }

  if (!baselineRunId || !baselines.has(baselineRunId)) return configurationReject("Comparison baseline is not part of the selected run set.", baselineRunId);

  const rows: ConfigurationRow[] = variables.map((variable) => {
    const baselineBinding = baselines.get(baselineRunId)?.bindings[variable.name];
    const baselineValue = baselineBinding?.value ? Number(baselineBinding.value) : null;
    const cells = runs.map((run): ConfigurationCell => {
      const binding = baselines.get(run.id)?.bindings[variable.name];
      const value = binding?.value ? Number(binding.value) : null;
      const delta = value !== null && baselineValue !== null ? value - baselineValue : null;
      return {
        runId: run.id,
        runLabel: labelFor(run),
        value,
        displayValue: value === null ? "Empty" : String(value),
        delta
      };
    });
    return { name: variable.name, label: variable.label, unit: variable.unit, cells };
  });

  return { state: "ready", message: null, baselineRunId, modelChoice: configurationModelChoice(implementation), rows };
}
