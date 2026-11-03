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
