import { API_BASE_URL } from "./client";

export type MemorySearchItem = {
  owner: string;
  authority_owner: string;
  object_type: string;
  title: string;
  location: string | null;
  excerpt: string;
  relative_path?: string | null;
  heading?: string | null;
};

export type DocumentFolderInfo = {
  registered: boolean;
  folder_name: string | null;
};

export type FolderRefreshReason = {
  path: string;
  reason: string;
};

export type FolderRefreshResult = {
  included: number;
  skipped: number;
  changed: number;
  deleted: number;
  reasons: FolderRefreshReason[];
};

export type RetrievalEvalQuestion = {
  id?: string;
  query: string;
  expected_ids: string[];
};

export type RetrievalEvalPayload = {
  questions: RetrievalEvalQuestion[];
  mode?: "lexical" | "hybrid" | "vector";
};

export type RetrievalEvalResult = {
  run_id: string;
  evaluator_id: string;
  status: string;
  verdict: "usable" | "usable_with_warnings" | "unchecked" | "not_usable";
  outputs: Array<{ name: string; value: { value: number; unit: string } }>;
};

export class MemorySearchError extends Error {
  status: number;
  code?: string;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.name = "MemorySearchError";
    this.status = status;
    this.code = code;
  }
}

async function requestJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, options);
  if (!response.ok) {
    let code: string | undefined;
    try {
      const errorBody = await response.json();
      if (errorBody?.detail?.code) {
        code = errorBody.detail.code;
      } else if (errorBody?.code) {
        code = errorBody.code;
      }
    } catch {
      // Non-JSON error
    }
    throw new MemorySearchError(response.status, `Request failed with ${response.status}`, code);
  }
  return response.json() as Promise<T>;
}

const base = (workspaceId: string) => `/workspaces/${encodeURIComponent(workspaceId)}/memory`;

export function searchMemory(workspaceId: string, q: string): Promise<MemorySearchItem[]> {
  return requestJson<MemorySearchItem[]>(`${base(workspaceId)}/search?q=${encodeURIComponent(q)}`);
}

export function getRegisteredFolder(workspaceId: string): Promise<DocumentFolderInfo> {
  return requestJson<DocumentFolderInfo>(`${base(workspaceId)}/documents`);
}

export function registerFolder(workspaceId: string, folder: string): Promise<DocumentFolderInfo> {
  return requestJson<DocumentFolderInfo>(`${base(workspaceId)}/documents`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ folder }),
  });
}

export function refreshFolder(workspaceId: string): Promise<FolderRefreshResult> {
  return requestJson<FolderRefreshResult>(`${base(workspaceId)}/documents/refresh`, {
    method: "POST",
  });
}

export function removeFolder(workspaceId: string): Promise<DocumentFolderInfo> {
  return requestJson<DocumentFolderInfo>(`${base(workspaceId)}/documents`, {
    method: "DELETE",
  });
}

export function runRetrievalEval(
  workspaceId: string,
  payload: RetrievalEvalPayload
): Promise<RetrievalEvalResult> {
  return requestJson<RetrievalEvalResult>(`${base(workspaceId)}/retrieval-eval`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}
