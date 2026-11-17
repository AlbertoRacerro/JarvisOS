// GENERATED FILE — DO NOT EDIT.
// Source: backend/app/modules/coding/activity.py
// Regenerate with: python scripts/generate_frontend_contracts.py

export type ActivityProgress = {
  done: number;
  total: number;
  unit: string;
};

export type ActivityLink = {
  kind: "pr" | "run" | "file";
  label: string;
  target: string;
};

export type ActivityRow = {
  key: string;
  kind: string;
  title: string;
  runner: "supervisione" | "modello di lavoro" | "motore locale" | "lavoro notturno";
  started_at: string | null;
  ended_at: string | null;
  state: "running" | "done" | "failed" | "unknown";
  state_word: string;
  progress: ActivityProgress | null;
  step: string | null;
  outcome: string | null;
  links: ActivityLink[];
};

export type ActivityGroup = {
  id: string;
  title: string;
  state: "ok" | "non disponibile";
  reason: string | null;
};

export type ActivityQuotaPool = {
  label: string;
  percent_free: number | null;
};

export type ActivityQuota = {
  state: "ok" | "non disponibile";
  pools: ActivityQuotaPool[];
};

export type ActivityRead = {
  generated_at: string;
  running: number;
  rows: ActivityRow[];
  groups: ActivityGroup[];
  quota: ActivityQuota;
  downloads: ActivityRow[];
  downloads_empty: string;
};

export type ActivityDetailRead = {
  state: "ok" | "refused" | "not_found";
  reason: string | null;
  title: string | null;
  label: string | null;
  lines: string[];
};
