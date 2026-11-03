// GENERATED FILE — DO NOT EDIT.
// Source: backend/app/modules/modeling/models.py::ParameterRead
// Regenerate with: python scripts/generate_frontend_contracts.py

export type ParameterRead = {
  name: string;
  symbol: string | null;
  value: string | null;
  unit: string;
  value_status: "candidate" | "literature" | "measured" | "validated" | "accepted";
  value_min: number | null;
  value_max: number | null;
  source_ref: string | null;
  confidence: number | null;
  status: string;
  notes: string | null;
  supersedes_parameter_id: string | null;
  id: string;
  workspace_id: string;
  created_at: string;
  updated_at: string;
  lifecycle_state: "active" | "inactive" | "superseded" | "archived" | "deleted";
};
