import type { ModelImplementation } from "../../api/client";
import type { SimulationRunDetail } from "../../api/runs";

export type PreviousRunBinding = Readonly<{ value: string; parameterId: "" }>;
export type PreviousRunBindingMap = Record<string, PreviousRunBinding>;

export type PreviousRunLoadReason =
  | "Run did not succeed"
  | "Model version is no longer available"
  | "Run snapshot is incomplete"
  | "Run snapshot does not match this model contract"
  | "Units do not match the current contract"
  | "Current engineering target is incompatible";

export type PreviousRunBaseline = Readonly<{
  workspaceId: string;
  runId: string;
  runLabel: string;
  modelVersionId: string;
  contractDigest: string;
  bindings: PreviousRunBindingMap;
}>;

export type PreviousRunLoadability =
  | Readonly<{ loadable: true; baseline: PreviousRunBaseline }>
  | Readonly<{ loadable: false; reason: PreviousRunLoadReason }>;

type ContractVariable = Readonly<{
  name: string;
  unit: string;
  required: boolean;
}>;

type InputContract = Readonly<{
  schema_version: number;
  variables: ContractVariable[];
}>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function inputContractOf(implementation: ModelImplementation): InputContract | null {
  const raw = implementation.input_contract as unknown;
  if (!isRecord(raw) || !Number.isInteger(raw.schema_version) || !Array.isArray(raw.variables)) return null;

  const variables: ContractVariable[] = [];
  for (const item of raw.variables) {
    if (
      !isRecord(item) ||
      typeof item.name !== "string" ||
      !item.name ||
      typeof item.unit !== "string" ||
      !item.unit ||
      typeof item.required !== "boolean"
    ) return null;
    variables.push({ name: item.name, unit: item.unit, required: item.required });
  }
  return { schema_version: raw.schema_version as number, variables };
}

function parsePayload(raw: string | null): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function reconstructPreviousRunBaseline(
  workspaceId: string,
  run: SimulationRunDetail,
  implementations: readonly ModelImplementation[]
): PreviousRunLoadability {
  if (run.workspace_id !== workspaceId || run.status !== "succeeded") {
    return { loadable: false, reason: "Run did not succeed" };
  }
  if (!run.model_version_id) {
    return { loadable: false, reason: "Model version is no longer available" };
  }

  const implementation = implementations.find(
    (item) => item.id === run.model_version_id && item.workspace_id === workspaceId
  );
  if (!implementation) {
    return { loadable: false, reason: "Model version is no longer available" };
  }

  const contract = inputContractOf(implementation);
  if (!contract || !implementation.input_contract_sha256) {
    return { loadable: false, reason: "Run snapshot does not match this model contract" };
  }

  const payload = parsePayload(run.input_payload);
  if (!payload) return { loadable: false, reason: "Run snapshot is incomplete" };

  const byName = new Map(contract.variables.map((variable) => [variable.name, variable]));
  for (const name of Object.keys(payload)) {
    if (!byName.has(name)) {
      return { loadable: false, reason: "Run snapshot does not match this model contract" };
    }
  }

  const bindings: PreviousRunBindingMap = {};
  for (const variable of contract.variables) {
    const rawItem = payload[variable.name];
    if (rawItem === undefined) {
      if (variable.required) return { loadable: false, reason: "Run snapshot is incomplete" };
      bindings[variable.name] = { value: "", parameterId: "" };
      continue;
    }
    if (!isRecord(rawItem) || typeof rawItem.value !== "number" || !Number.isFinite(rawItem.value)) {
      return { loadable: false, reason: "Run snapshot is incomplete" };
    }
    if (rawItem.unit !== variable.unit) {
      return { loadable: false, reason: "Units do not match the current contract" };
    }

    // Historical linked-source ids/revisions are evidence only. They never recreate
    // current Parameter authority when a previous run becomes the working baseline.
    bindings[variable.name] = { value: String(rawItem.value), parameterId: "" };
  }

  return {
    loadable: true,
    baseline: {
      workspaceId,
      runId: run.id,
      runLabel: run.run_label?.trim() || "Unnamed run",
      modelVersionId: implementation.id,
      contractDigest: implementation.input_contract_sha256,
      bindings
    }
  };
}
