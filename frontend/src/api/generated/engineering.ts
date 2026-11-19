// GENERATED FILE — DO NOT EDIT.
// Source: backend/app/modules/engineering/operator_models.py
// Regenerate with: python scripts/generate_frontend_contracts.py

export type EvaluatorRead = {
  evaluator_id: string;
  backend_kind: string | null;
  backend_name: string | null;
  backend_version: string | null;
  fidelity: string | null;
  state: string;
  reason_code: string | null;
};

export type CapabilityRead = {
  capability_id: string;
  state: "available" | "unavailable" | "not_configured";
  reason_code: string | null;
};

export type CaseVerdictRead = {
  id: string;
  workspace_id: string;
  case_id: string;
  run_id: string;
  quantity: string;
  computed_value: number;
  computed_unit: string;
  published_value: number;
  published_unit: string;
  rel_tol: number;
  deviation: number;
  verdict: "pass" | "fail";
  status: "recorded" | "accepted" | "superseded";
  rationale: string | null;
  accepted_by: string | null;
  accepted_at: string | null;
  created_at: string;
  updated_at: string;
};
