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
