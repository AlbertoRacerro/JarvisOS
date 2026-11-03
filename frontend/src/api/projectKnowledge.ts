import { API_BASE_URL } from "./client";

export type ProjectKnowledgeOperation = {
  operation_id?: string | null;
  owner_kind: "requirement" | "decision" | "assumption" | "parameter" | "model_spec" | "requirement_applicability";
  operation_kind: "create" | "update" | "retire" | "set_applicability" | "retire_applicability";
  owner_id?: string | null;
  expected_updated_at?: string | null;
  provisional_ref?: string | null;
  fields: Record<string, unknown>;
  source_refs?: string[];
  proposal_id?: string | null;
  dependency_add?: [string, string, string][];
  dependency_remove?: [string, string, string][];
};

export type ProjectKnowledgeDraft = {
  id: string;
  workspace_id: string;
  parent_revision_id: string | null;
  parent_kind: "reconciled" | "working";
  revision_token: string;
  operations: ProjectKnowledgeOperation[];
  preview_digest: string | null;
  created_at: string;
  updated_at: string;
};

export type ProjectKnowledgeImpact = {
  draft_id: string;
  draft_revision_token: string;
  parent_kind: "reconciled" | "working";
  parent_revision_id: string | null;
  ancestor_revision_ids: string[];
  affected_refs: string[];
  owner_tokens: Record<string, string>;
  applicability_refs: string[];
  recomputation_required: string[];
  diagnostics: string[];
  complete: boolean;
  digest: string;
};

export type ProjectKnowledgeRevision = {
  id: string;
  workspace_id: string;
  parent_revision_id: string | null;
  parent_kind: "reconciled" | "working";
  state: "working" | "discarded" | "superseded" | "reconciled";
  change_set_digest: string;
  operations: ProjectKnowledgeOperation[];
  projected_state_digest: string;
  origin: string;
  created_at: string;
  accepted_at: string;
  superseded_by_revision_id: string | null;
  reconciled_snapshot_id: string | null;
};

export type ProjectKnowledgeRevalidation = {
  working_revision_id: string;
  complete: boolean;
  mandatory_requirement_ids: string[];
  current_validation_ids: string[];
  blocking_requirement_ids: string[];
  known_fail_requirement_ids: string[];
  recomputation_required: string[];
  selected_validation_set_digest: string;
  diagnostics: string[];
};

export type ProjectKnowledgeApproval = {
  request_id: string;
  state: "pending" | "success" | "failed";
  outcome: string | null;
  working_revision_id: string | null;
  failure_code?: string | null;
};

export type ProjectKnowledgeReconcile = {
  request_id: string;
  state: "pending" | "success" | "failed";
  outcome: string | null;
  resulting_snapshot_id: string | null;
  canonical_id_map: Record<string, string>;
  failure_code?: string | null;
};

type ApiErrorBody = { detail?: { code?: string; message?: string } | string };

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);
  if (!response.ok) {
    let detail = `Request failed with ${response.status}`;
    try {
      const body = (await response.json()) as ApiErrorBody;
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail?.message) detail = `${body.detail.code ? `${body.detail.code}: ` : ""}${body.detail.message}`;
    } catch {
      // Preserve the bounded status-only fallback when the backend did not return JSON.
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

function jsonInit(method: "POST" | "PUT", payload: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  };
}

export function listProjectKnowledgeRevisions(workspaceId: string): Promise<ProjectKnowledgeRevision[]> {
  return requestJson<ProjectKnowledgeRevision[]>(`/project-knowledge/workspaces/${workspaceId}/revisions`);
}

export function createProjectKnowledgeDraft(payload: {
  workspace_id: string;
  parent_kind: "reconciled" | "working";
  parent_revision_id: string | null;
  operations: ProjectKnowledgeOperation[];
}): Promise<ProjectKnowledgeDraft> {
  return requestJson<ProjectKnowledgeDraft>("/project-knowledge/drafts", jsonInit("POST", payload));
}

export function previewProjectKnowledgeImpact(workspaceId: string, draftId: string): Promise<ProjectKnowledgeImpact> {
  return requestJson<ProjectKnowledgeImpact>(`/project-knowledge/workspaces/${workspaceId}/drafts/${draftId}/impact`);
}

export function approveProjectKnowledgeDraft(payload: {
  workspace_id: string;
  approval_request_key: string;
  draft_id: string;
  expected_draft_revision_token: string;
  expected_preview_digest: string;
  origin: string;
}): Promise<ProjectKnowledgeApproval> {
  return requestJson<ProjectKnowledgeApproval>("/project-knowledge/approvals", jsonInit("POST", payload));
}

export function getProjectKnowledgeRevalidation(workspaceId: string, revisionId: string): Promise<ProjectKnowledgeRevalidation> {
  return requestJson<ProjectKnowledgeRevalidation>(`/project-knowledge/workspaces/${workspaceId}/revisions/${revisionId}/revalidation`);
}

export function changeProjectKnowledgeRevisionState(
  revisionId: string,
  payload: { workspace_id: string; action: "discard" | "supersede"; superseded_by_revision_id?: string | null }
): Promise<ProjectKnowledgeRevision> {
  return requestJson<ProjectKnowledgeRevision>(`/project-knowledge/revisions/${revisionId}/state`, jsonInit("POST", payload));
}

export function reconcileProjectKnowledge(payload: {
  workspace_id: string;
  idempotency_key: string;
  working_revision_id: string;
  expected_target_snapshot_id: string | null;
  expected_target_digest: string;
  expected_selected_validation_set_digest: string;
  known_fail_acknowledgement?: string | null;
  policy_identity?: string | null;
}): Promise<ProjectKnowledgeReconcile> {
  return requestJson<ProjectKnowledgeReconcile>("/project-knowledge/reconcile", jsonInit("POST", payload));
}
