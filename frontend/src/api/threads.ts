import { API_BASE_URL } from "./client";
import type { KnowledgeContextPreview } from "./knowledgeActions";
import type { ActionOutcome, SurfaceRef } from "./workspaceActions";

export type ConversationRoute = {
  route_class: string;
  label: string;
  model_id: string;
  execution_class: "local_compute" | "synthetic" | "agent";
  availability: {
    configured: boolean;
    runtime_reachable: boolean | null;
    model_installed: boolean | null;
    model_loaded: boolean | null;
    qualified: true | false | "unknown";
    reason_code: string | null;
    message: string;
  };
};

export type ThreadSummary = {
  id: string;
  workspace_id: string;
  title: string | null;
  created_at: string;
  last_activity_at: string;
};

export type ThreadInteraction = {
  id: string;
  request_id: string;
  interaction_index: number;
  user_text: string;
  assistant_text: string | null;
  assistant_text_truncated: boolean;
  flow_id: string;
  persistence_state: string;
  persistence_error: string | null;
  flow_state: string;
  terminal_reason: string | null;
  attempt_count: number;
  terminal_attempt_id: string | null;
  route_class?: string | null;
  execution_class?: string | null;
  /** BUBU-2: screenshots sent with the turn, and whether the answering model could not see them. */
  image_count?: number;
  images_unsupported?: boolean;
  model_id?: string | null;
  provider_id?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cost_estimate_usd?: number | null;
  usage_source?: string | null;
  latency_ms?: number | null;
  completed_at?: string | null;
  elapsed_ms?: number | null;
  activity?: string | null;
  surface_summary?: string | null;
  surface_digest?: string | null;
  actions?: ActionOutcome[];
  technical_details?: string | null;
  proposal_ids: string[];
  proposal_count: number;
  proposals_truncated: boolean;
  created_at: string;
  updated_at: string;
};

export type ThreadDetail = ThreadSummary & {
  interactions: ThreadInteraction[];
  has_older: boolean;
};

export type CloudEscalation = {
  id: string;
  state: "held" | "confirmation_required" | "complete" | "partial" | "failed";
  task_family: string;
  quality_floor: number;
  quality_tier: number;
  qualification: string;
  qualification_evidence_ref: string;
  provider_id: string;
  model_id: string;
  derivative_id: string;
  derivative_digest: string;
  projected_cost_usd: string;
  accounted_cost_usd: string;
  accounted_cost_eur: string;
  calculated_usage_cost_eur: string | null;
  pricing_version: string;
  pricing_reviewed_on: string;
  pricing_source_url: string;
  pricing_effective_at: string;
  cost_basis: "hold" | "actual_priced" | "zero_before_network" | "upper_unknown";
  actual_input_tokens: number | null;
  actual_output_tokens: number | null;
  context_digest: string | null;
  source_interaction_id: string;
  fx_date: string;
  eur_usd_rate: string;
  fx_source: string;
  flow_id: string | null;
  ai_job_id: string | null;
  ticket_id: string | null;
  egress_packet_digest: string | null;
  reason_code: string | null;
  response_text: string | null;
  created_at: string;
};

export type CloudDerivative = {
  id: string;
  workspace_id: string;
  content: string;
  content_digest: string;
  status: "draft" | "approved" | "revoked" | "stale";
  effective_level: string;
};

export type ContextSelection = {
  kinds?: string[];
  statuses?: Record<string, string[]> | string[] | null;
  ids?: string[] | null;
  query?: string | null;
  max_items_per_kind?: number;
};

export type ContextPackPreview = {
  context_digest: string | null;
  context_sources_manifest: Array<{ source: string; type?: string | null; id?: string | null }>;
  char_count: number;
  estimated_token_count: number;
  included_count: number;
  dropped_count: number;
  budget_chars: number;
};

export class ThreadsRequestError extends Error {
  readonly status: number;
  readonly detail: string | null;
  constructor(status: number, detail: string | null = null) {
    super(detail ?? `AI thread request failed with ${status}`);
    this.name = "ThreadsRequestError";
    this.status = status;
    this.detail = detail;
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) }
  });
  if (!response.ok) {
    let detail: string | null = null;
    try {
      const body = await response.json() as { detail?: unknown };
      const value = body.detail;
      detail = typeof value === "string" ? value
        : value && typeof value === "object" && "message" in value && typeof value.message === "string" ? value.message
        : null;
    } catch { /* The server may return an empty or non-JSON error. */ }
    throw new ThreadsRequestError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export async function listThreads(workspaceId: string): Promise<ThreadSummary[]> {
  const result = await requestJson<{ threads: ThreadSummary[] }>(
    `/ai/threads?workspace_id=${encodeURIComponent(workspaceId)}`
  );
  return result.threads;
}

export function getConversationOptions(): Promise<{routes: ConversationRoute[]; availability: "configured" | "unavailable"}> {
  return requestJson("/ai/threads/conversation-options");
}

export function getThread(workspaceId: string, threadId: string): Promise<ThreadDetail> {
  return requestJson(
    `/ai/threads/${encodeURIComponent(threadId)}?workspace_id=${encodeURIComponent(workspaceId)}`
  );
}

export function createThread(workspaceId: string, title?: string): Promise<ThreadSummary> {
  return requestJson("/ai/threads", {
    method: "POST",
    body: JSON.stringify({ workspace_id: workspaceId, title: title || null })
  });
}

export function previewThreadContext(
  workspaceId: string,
  selection: ContextSelection
): Promise<ContextPackPreview> {
  return requestJson("/ai/context/packs/preview", {
    method: "POST",
    body: JSON.stringify({ workspace_id: workspaceId, selection })
  });
}

/** DOM-first description of the visible page (BUBU-2); structurally identical to the pageContext type. */
export type ThreadUiContext = {
  route: string;
  title: string;
  targets: { id: string; role: string; name: string; text?: string }[];
};

export async function submitThreadInteraction(
  workspaceId: string,
  threadId: string,
  requestId: string,
  prompt: string,
  context?: { selection: ContextSelection; expectedDigest: string },
  options?: { routeClass: string; knowledgeContext?: KnowledgeContextPreview | null; surfaceContext?: SurfaceRef; ui_context?: ThreadUiContext; images?: string[] }
): Promise<ThreadInteraction> {
  const result = await requestJson<{ interaction: ThreadInteraction }>(
    `/ai/threads/${encodeURIComponent(threadId)}/interactions?workspace_id=${encodeURIComponent(workspaceId)}`,
    {
      method: "POST",
      body: JSON.stringify({
        request_id: requestId,
        prompt,
        ...(options ? { route_class: options.routeClass } : {}),
        ...(options?.surfaceContext ? { surface_context: options.surfaceContext } : {}),
        ...(options?.ui_context ? { ui_context: options.ui_context } : {}),
        ...(options?.images?.length ? { images: options.images } : {}),
        ...(options?.knowledgeContext ? {
          jarvis_context: {
            workspace_id: options.knowledgeContext.workspace_id,
            route: {
              route_id: options.knowledgeContext.route_id,
              canonical_path: {
                "memory-project-basis": "/memory/project-basis",
                "memory-models": "/memory/models",
                "memory-literature": "/memory/literature"
              }[options.knowledgeContext.route_id]
            },
            added_context_refs: options.knowledgeContext.exact_refs
          },
          expected_jarvis_context_digest: options.knowledgeContext.context_digest
        } : {}),
        ...(context
          ? { context_selection: context.selection, expected_context_digest: context.expectedDigest }
          : {})
      })
    }
  );
  return result.interaction;
}

export function listCloudEscalations(workspaceId: string, threadId: string): Promise<CloudEscalation[]> {
  return requestJson(`/ai/threads/${encodeURIComponent(threadId)}/cloud-escalations?workspace_id=${encodeURIComponent(workspaceId)}`);
}

export type EscalationDraft = {
  status: "ready" | "edit_required" | "refused";
  reason_code: string | null;
  reason: string | null;
  source_interaction_id: string;
  text: string;
  text_digest: string | null;
  level: string;
  task_family: string;
  task_family_inferred: boolean;
  family_options: string[];
  candidate: null | { provider_id: string; model_id: string; route_class: string; quality_tier: number; qualification: string; max_cost_usd: string; request_cap_usd: string };
};

export function draftInteractionEscalation(workspaceId: string, threadId: string, interactionId: string, taskFamily?: string, text?: string): Promise<EscalationDraft> {
  const family = taskFamily ? `&task_family=${encodeURIComponent(taskFamily)}` : "";
  return requestJson(`/ai/threads/${encodeURIComponent(threadId)}/interactions/${encodeURIComponent(interactionId)}/escalation-draft?workspace_id=${encodeURIComponent(workspaceId)}${family}`, { method: "POST", body: JSON.stringify({ text: text ?? null }) });
}

export function escalateInteraction(workspaceId: string, threadId: string, interactionId: string, text: string, textDigest: string, taskFamily?: string): Promise<CloudEscalation> {
  return requestJson(`/ai/threads/${encodeURIComponent(threadId)}/interactions/${encodeURIComponent(interactionId)}/escalate?workspace_id=${encodeURIComponent(workspaceId)}`, {
    method: "POST", body: JSON.stringify({ text, text_digest: textDigest, ...(taskFamily ? { task_family: taskFamily } : {}) })
  });
}

export function submitCloudEscalation(
  workspaceId: string, threadId: string,
  request: {request_id: string; source_interaction_id: string; derivative_id: string; task_family: string}
): Promise<CloudEscalation> {
  return requestJson(`/ai/threads/${encodeURIComponent(threadId)}/cloud-escalations?workspace_id=${encodeURIComponent(workspaceId)}`, {
    method: "POST", body: JSON.stringify(request)
  });
}

export function confirmCloudEscalation(workspaceId: string, threadId: string, escalationId: string): Promise<CloudEscalation> {
  return requestJson(`/ai/threads/${encodeURIComponent(threadId)}/cloud-escalations/${encodeURIComponent(escalationId)}/confirm?workspace_id=${encodeURIComponent(workspaceId)}`, {
    method: "POST"
  });
}

export function prepareCloudDerivative(workspaceId: string, sourceRef: string, content: string): Promise<CloudDerivative> {
  return requestJson("/ai/sensitivity/derivatives", {
    method: "POST",
    body: JSON.stringify({
      workspace_id: workspaceId,
      source_refs: [sourceRef],
      content,
      effective_level: "S1",
      transformations: ["Operator removed identifying, confidential, and project-specific detail"]
    })
  });
}

export function approveCloudDerivative(workspaceId: string, derivativeId: string): Promise<CloudDerivative> {
  return requestJson(`/ai/sensitivity/derivatives/${encodeURIComponent(derivativeId)}/approve?workspace_id=${encodeURIComponent(workspaceId)}`, {
    method: "POST", body: JSON.stringify({ reviewer_notes: "Reviewed for one-step governed cloud escalation" })
  });
}

// Spec 157: hand a cloud-safe coding task to a Relay-managed agent from the thread.
// Jarvis decides admission, workspace access and context release; the frontend
// only ever talks to these backend endpoints, never to Relay or a provider directly.
export type RelayStatus = {
  enabled: boolean;
  repository_level: "S0" | "S1" | "S2" | "S3" | "S4";
  access_mode: "repository" | "derivative";
  agents: string[];
  private_domain_data_enabled: boolean;
  blocked_reason: string | null;
};

export type RelayRunState = "queued" | "running" | "completed" | "failed" | "denied";

export type RelayRunRead = {
  id: string;
  thread_id: string;
  source_interaction_id: string | null;
  model: string | null;
  relay_workspace_id: string | null;
  agent: string;
  state: RelayRunState;
  reason_code: string | null;
  relay_session_id: string | null;
  continued_from_session_id: string | null;
  turn_index: number;
  repository_level: string;
  access_mode: "repository" | "derivative";
  released_derivative_ids: string[];
  prompt_source: "operator_attested" | "approved_derivative" | null;
  stop_reason: string | null;
  exit_code: number | null;
  result_text: string | null;
  actions?: ActionOutcome[];
  workspace_path: string | null;
  base_commit: string | null;
  head_commit: string | null;
  change_summary: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export function getRelayStatus(): Promise<RelayStatus> {
  return requestJson("/ai/relay/status");
}

export function listRelayRuns(workspaceId: string, threadId: string): Promise<RelayRunRead[]> {
  return requestJson(`/ai/threads/${encodeURIComponent(threadId)}/relay-runs?workspace_id=${encodeURIComponent(workspaceId)}`);
}

export function getRelayRun(workspaceId: string, threadId: string, runId: string): Promise<RelayRunRead> {
  return requestJson(`/ai/threads/${encodeURIComponent(threadId)}/relay-runs/${encodeURIComponent(runId)}?workspace_id=${encodeURIComponent(workspaceId)}`);
}

export function submitRelayRun(
  workspaceId: string, threadId: string,
  request: { prompt: string; agent: string; cloud_safe_attested: boolean }
): Promise<RelayRunRead> {
  return requestJson(`/ai/threads/${encodeURIComponent(threadId)}/relay-runs?workspace_id=${encodeURIComponent(workspaceId)}`, {
    method: "POST", body: JSON.stringify(request)
  });
}

// Spec 161: Relay is the default, subscription-backed escalation of a finished turn.
// The server screens the source text, names the agent/model, and binds approval to the
// exact text digest; a Relay failure never falls back to a metered provider.
export type RelayEscalationDraft = {
  status: "ready" | "edit_required" | "refused" | "unavailable";
  reason_code: string | null;
  reason: string | null;
  source_interaction_id: string;
  text: string;
  text_digest: string | null;
  agent: string | null;
  model: string | null;
  billing: "subscription";
  /** Screenshots retained with the source turn; offered to the operator, never sent unasked. */
  image_count?: number;
  image_digests?: string[];
};

export function draftRelayEscalation(workspaceId: string, threadId: string, interactionId: string, text?: string, surfaceContext?: SurfaceRef): Promise<RelayEscalationDraft> {
  return requestJson(`/ai/threads/${encodeURIComponent(threadId)}/interactions/${encodeURIComponent(interactionId)}/relay-escalation-draft?workspace_id=${encodeURIComponent(workspaceId)}`, {
    method: "POST", body: JSON.stringify({ text: text ?? null, ...(surfaceContext ? { surface_context: surfaceContext } : {}) })
  });
}

export function escalateWithRelay(workspaceId: string, threadId: string, interactionId: string, text: string, textDigest: string, imageDigests: string[] = []): Promise<RelayRunRead> {
  return requestJson(`/ai/threads/${encodeURIComponent(threadId)}/interactions/${encodeURIComponent(interactionId)}/relay-escalate?workspace_id=${encodeURIComponent(workspaceId)}`, {
    method: "POST", body: JSON.stringify({ text, text_digest: textDigest, ...(imageDigests.length ? { image_digests: imageDigests } : {}) })
  });
}
