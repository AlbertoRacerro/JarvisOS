import { API_BASE_URL } from "../../api/client";

export const environmentUrl = (workspaceId: string, path: string) =>
  `${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/environment${path}`;

export async function environmentApi<T>(
  workspaceId: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(environmentUrl(workspaceId, path), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  const contentType = response.headers.get("content-type") ?? "";
  const body = contentType.includes("application/json")
    ? await response.json()
    : { detail: { error: await response.text() } };
  if (!response.ok) {
    throw new Error(body.detail?.error ?? body.detail ?? "Environment request failed.");
  }
  return body as T;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Environment operation failed.";
}
