import {
  API_BASE_URL,
  type AISettings,
  type AIStatus,
  type SystemInfoResponse
} from "./client";

export type SettingsSecretStatus = {
  key_present: boolean;
  source: string;
  effective_source: "environment" | "secure_persisted" | "none";
  persisted_state: "absent" | "usable" | "corrupted" | "unavailable";
  storage_mode: "environment" | "secure_persisted" | "none" | "corrupted" | "unavailable";
  last_updated_at?: string | null;
  reason_code?: string | null;
};

export type ProviderCredentialStatus = {
  key_present: boolean;
  effective_source: "environment" | "secure_persisted" | "absent" | "invalid" | "unknown" | "not_required";
  persisted_state: "absent" | "usable" | "corrupted" | "unavailable" | "not_supported";
  reason_code?: string | null;
};

export type ProviderCredentialCapabilities = {
  replace_persisted: boolean;
  delete_persisted: boolean;
};

export type ProviderSettingsEntry = {
  provider_id: string;
  kind: string;
  enabled: boolean;
  requires_network: boolean;
  execution_class: string;
  monthly_token_cap: number;
  monthly_cost_cap_usd: number;
  external_calls_allowed: boolean;
  blocking_reason?: string | null;
  credential: ProviderCredentialStatus;
  credential_capabilities: ProviderCredentialCapabilities;
};

export type ProviderSettings = {
  providers: ProviderSettingsEntry[];
  default_provider_id: string;
  policy_mode: string;
  external_calls_allowed: boolean;
  blocking_reason?: string | null;
  monthly_api_budget_usd: number;
  spend_month_to_date_usd: number;
};

export class SettingsApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(status: number, code: string | null, message: string) {
    super(message);
    this.name = "SettingsApiError";
    this.status = status;
    this.code = code;
  }
}

type PublicErrorDetail = {
  code?: unknown;
  message?: unknown;
};

function boundedText(value: unknown, maxLength: number): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  if (!normalized) return null;
  return normalized.slice(0, maxLength);
}

async function toSettingsError(response: Response): Promise<SettingsApiError> {
  let code: string | null = null;
  let message: string | null = null;

  try {
    const payload = await response.json() as { detail?: unknown };
    if (payload && typeof payload === "object" && payload.detail && typeof payload.detail === "object" && !Array.isArray(payload.detail)) {
      const detail = payload.detail as PublicErrorDetail;
      code = boundedText(detail.code, 96);
      message = boundedText(detail.message, 320);
    }
  } catch {
    // Deliberately ignore unparseable bodies. Never project arbitrary response text.
  }

  return new SettingsApiError(
    response.status,
    code,
    message ?? `Request failed with ${response.status}.`
  );
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);
  if (!response.ok) throw await toSettingsError(response);
  return response.json() as Promise<T>;
}

export function loadAISettings(): Promise<AISettings> {
  return requestJson<AISettings>("/ai/settings");
}

export function loadAIStatus(): Promise<AIStatus> {
  return requestJson<AIStatus>("/ai/status");
}

export function loadProviderSettings(): Promise<ProviderSettings> {
  return requestJson<ProviderSettings>("/ai/provider-settings");
}

export function loadSecretStatus(): Promise<SettingsSecretStatus> {
  return requestJson<SettingsSecretStatus>("/secrets/scaleway/status");
}

export function loadSystemInfo(): Promise<SystemInfoResponse> {
  return requestJson<SystemInfoResponse>("/system/info");
}

export function saveAISetting(payload: Record<string, number | boolean>): Promise<AISettings> {
  return requestJson<AISettings>("/ai/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

export function replaceScalewayCredential(apiKey: string): Promise<SettingsSecretStatus> {
  return requestJson<SettingsSecretStatus>("/secrets/scaleway/api-key", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey })
  });
}

export function removeScalewayCredential(): Promise<SettingsSecretStatus> {
  return requestJson<SettingsSecretStatus>("/secrets/scaleway/api-key", { method: "DELETE" });
}
