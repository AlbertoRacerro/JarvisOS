// GENERATED FILE — DO NOT EDIT.
// Source: backend/app/modules/ai/thread_models.py
// Regenerate with: python scripts/generate_frontend_contracts.py

export type NumberProvenanceEntry = {
  raw: string;
  value: number;
  status: "verified" | "unverified";
  source_ref: string | null;
  tool_name: string | null;
  route_hint: string | null;
};

export type NumberProvenance = {
  verified_count: number;
  unverified_count: number;
  entries: NumberProvenanceEntry[];
};
