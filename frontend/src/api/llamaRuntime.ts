import { API_BASE_URL } from "./client";

export type LlamaStatus = {
  configured: boolean; runtime_reachable: boolean; model_loaded: boolean;
  reason_code: string | null; message: string; model_file: string | null;
  ctx_size: number | null; n_gpu_layers: number | null; vram_bytes: number | null;
  ram_bytes: number | null; spawned_by_jarvis: boolean; crash_count: number;
  last_crash_at: string | null;
};
export type LlamaAction = "start" | "stop" | "restart";
const path = "/local-ai/runtime/llama-cpp";
async function request(action?: LlamaAction): Promise<LlamaStatus> {
  const response = await fetch(`${API_BASE_URL}${path}${action ? `/${action}` : ""}`, action ? { method: "POST" } : undefined);
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") message = body.detail;
    } catch { /* Keep the HTTP status. */ }
    throw new Error(message);
  }
  return response.json() as Promise<LlamaStatus>;
}
export const readLlamaStatus = () => request();
export const controlLlama = (action: LlamaAction) => request(action);

export type LocalModelEntry = {
  id: string; label: string; runtime: "llama_cpp" | "strata";
  qualification: "default" | "qualified" | "under_qualification";
  model_file: string; installed: boolean;
  digest_state: "missing" | "hashing" | "not_verified" | "verified" | "mismatch";
};
export type LocalModels = { registry_present: boolean; models: LocalModelEntry[]; active_id: string | null };
async function modelRequest(path: string, method?: "POST"): Promise<LocalModels | LlamaStatus> {
  const response = await fetch(`${API_BASE_URL}/local-ai/models${path}`, method ? { method } : undefined);
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try { const body: { detail?: string } = await response.json(); if (typeof body.detail === "string") message = body.detail; } catch { /* Keep status. */ }
    throw new Error(message);
  }
  return response.json();
}
export const readLocalModels = () => modelRequest("") as Promise<LocalModels>;
export const activateLocalModel = (id: string) => modelRequest(`/${encodeURIComponent(id)}/activate`, "POST") as Promise<LlamaStatus>;
export const restoreDefaultLocalModel = () => modelRequest("/restore-default", "POST") as Promise<LlamaStatus>;
