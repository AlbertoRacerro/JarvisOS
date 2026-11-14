// GENERATED FILE — DO NOT EDIT.
// Source: backend/app/modules/project_knowledge/value_history.py
// Regenerate with: python scripts/generate_frontend_contracts.py

export type RunLinkedParameterItem = {
  variable_name: string;
  id: string;
  name: string;
  value: unknown;
  unit: string;
  status: string;
  lifecycle_state: string | null;
  position: number;
  newer_version_exists: boolean;
  source_parameter_updated_at: string | null;
};

export type RunInputsManifestRead = {
  simulation_run_id: string;
  workspace_id: string;
  status: string;
  history_not_recorded: boolean;
  history_recorded: boolean;
  reason: string | null;
  items: RunLinkedParameterItem[];
};

export type ValueVersionItem = {
  version_index: number;
  id: string;
  value: unknown;
  unit: string;
  conditions: Record<string, unknown>;
  status: string;
  validation_state: string;
  action: string | null;
  source: string;
  actor: string | null;
  reason: string | null;
  created_at: string;
  origin: string | null;
};

export type ValueVersionChainRead = {
  value_kind: string;
  value_id: string;
  workspace_id: string;
  intermediate_versions_unknown: boolean;
  versions: ValueVersionItem[];
};

export type BoundRunRef = {
  id: string;
  run_label: string | null;
  status: string;
  created_at: string | null;
};

export type BoundRequirementCheckRef = {
  id: string;
  requirement_id: string;
  outcome: string;
  reason_code: string;
  created_at: string | null;
};

export type ValueComparisonSide = {
  version_index: number | null;
  id: string | null;
  value: unknown;
  unit: string;
  conditions: Record<string, unknown>;
  source: string;
  validation_state: string;
  status: string | null;
  actor: string | null;
  reason: string | null;
  created_at: string | null;
  runs: BoundRunRef[];
  requirement_checks: BoundRequirementCheckRef[];
};

export type ValueComparisonRead = {
  value_kind: string;
  value_id: string;
  version_a: ValueComparisonSide;
  version_b: ValueComparisonSide;
};
