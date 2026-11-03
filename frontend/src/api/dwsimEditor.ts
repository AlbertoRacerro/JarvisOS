import { API_BASE_URL } from "./client";
import type {
  CommandResult,
  EditorCaseRead,
  EditorCommand,
  EditorProjectionRead,
  RevisionRead,
} from "./generated/dwsimEditor";

export type EditorErrorBody = {
  code: string;
  message: string;
  current_revision?: string;
};
export class DwsimEditorError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly currentRevision?: string,
  ) {
    super(message);
    this.name = "DwsimEditorError";
  }
}

const base = (workspaceId: string) =>
  `/workspaces/${encodeURIComponent(workspaceId)}/process/dwsim`;
const casePath = (workspaceId: string, caseId: string) =>
  `${base(workspaceId)}/cases/${encodeURIComponent(caseId)}`;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);
  if (!response.ok) {
    let detail: EditorErrorBody = {
      code: `http_${response.status}`,
      message: `DWSIM editor request failed (${response.status})`,
    };
    try {
      const body = (await response.json()) as { detail?: EditorErrorBody };
      if (body.detail?.code) detail = body.detail;
    } catch {
      /* Keep the status-based message when the server has no JSON body. */
    }
    throw new DwsimEditorError(
      response.status,
      detail.code,
      detail.message,
      detail.current_revision,
    );
  }
  return response.json() as Promise<T>;
}

export const listDwsimCases = (workspaceId: string) =>
  request<EditorCaseRead[]>(`${base(workspaceId)}/cases`);
export const createDwsimCase = (workspaceId: string, name: string) =>
  request<EditorCaseRead>(
    `${base(workspaceId)}/cases?name=${encodeURIComponent(name)}`,
    { method: "POST" },
  );
export const importDwsimCase = async (workspaceId: string, file: File) =>
  request<EditorCaseRead>(
    `${base(workspaceId)}/cases/import?filename=${encodeURIComponent(file.name)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: await file.arrayBuffer(),
    },
  );
export const getDwsimProjection = (workspaceId: string, caseId: string) =>
  request<EditorProjectionRead>(casePath(workspaceId, caseId));
export const listDwsimRevisions = (workspaceId: string, caseId: string) =>
  request<RevisionRead[]>(`${casePath(workspaceId, caseId)}/revisions`);
export const runDwsimCommand = (
  workspaceId: string,
  caseId: string,
  command: EditorCommand,
) =>
  request<CommandResult>(`${casePath(workspaceId, caseId)}/commands`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });
export const restoreDwsimRevision = (
  workspaceId: string,
  caseId: string,
  expectedRevision: string,
  sourceRevision: string,
) =>
  request<CommandResult>(
    `${casePath(workspaceId, caseId)}/restore?expected_revision=${encodeURIComponent(expectedRevision)}&source_revision=${encodeURIComponent(sourceRevision)}`,
    { method: "POST" },
  );
export const dwsimRevisionDownloadUrl = (
  workspaceId: string,
  caseId: string,
  revision: string,
) =>
  `${API_BASE_URL}${casePath(workspaceId, caseId)}/revisions/${encodeURIComponent(revision)}/download`;
