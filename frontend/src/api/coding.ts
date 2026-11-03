import { API_BASE_URL } from "./client";

export const CODING_REPOSITORY = import.meta.env.VITE_CODING_REPOSITORY ?? "AlbertoRacerro/JarvisOS";
export const CODING_TARGET_REF = import.meta.env.VITE_CODING_TARGET_REF ?? "master";

export type RepositoryTruthResult = Readonly<{
  provider: string;
  repository: string;
  operation: string;
  requested_ref: string | null;
  resolved_sha: string | null;
  partial: boolean;
  payload: Record<string, unknown>;
  observed_at: string;
}>;

export type RuntimeTruth = Readonly<{
  startup: Record<string, unknown>;
  live: Record<string, unknown>;
  remote: Record<string, unknown> | null;
  alignment: "aligned" | "local_behind" | "divergent" | "unknown";
  reason?: string | null;
  worktree_changed_since_start: boolean;
  semantic_delta: Record<string, unknown>;
  observer_status: "ok" | "degraded" | "unavailable";
  remote_status: string;
}>;

export type ControlRoomSpecRef = Readonly<{
  spec_id: string;
  name: string | null;
  title: string | null;
  status: string | null;
}>;

export type ControlRoomRecentItem = Readonly<{
  sha: string;
  merged_at: string;
  subject: string;
  pr_number: number | null;
  branch: string | null;
  kind: string;
  spec_ids: string[];
  maintenance: boolean;
  specs: ControlRoomSpecRef[];
}>;

export type ControlRoomUpcomingItem = Readonly<{
  spec_id: string;
  status: string;
  name: string;
  title: string;
  description: string;
  dependencies: Readonly<{ spec_id: string; status: string | null }>[];
  dependencies_merged: boolean;
  implementation_prs: number[];
}>;

export type ControlRoom = Readonly<{
  repository: string;
  target_ref: string;
  observed_at: string;
  runtime: RuntimeTruth | null;
  source: Readonly<{ git_ref: string; sha: string | null; status_path: string }>;
  recent_work: Readonly<{ status: "available" | "unavailable"; limit: number; items: ControlRoomRecentItem[] }>;
  upcoming: Readonly<{
    status: "available" | "unavailable";
    authorized: ControlRoomUpcomingItem[];
    planned_ready: ControlRoomUpcomingItem[];
    planned_waiting: ControlRoomUpcomingItem[];
    blocked: ControlRoomUpcomingItem[];
  }>;
  warnings: string[];
}>;

export type CodingActionResult = Readonly<{
  state: string;
  reason?: string;
  [key: string]: unknown;
}>;

export type CodingContextPreview = Readonly<{
  state: string;
  reason?: string;
  repository?: string;
  base_sha?: string;
  target_paths?: string[];
  added_context_refs?: unknown[];
  context_digest?: string;
  context_sources_manifest?: unknown[];
}>;

export class CodingRequestError extends Error {
  constructor(readonly status: number, readonly code: string) {
    super(`Coding request failed: ${code}`);
  }
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") search.set(key, String(value));
  });
  return search.toString();
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);
  if (!response.ok) {
    let code = `http_${response.status}`;
    try {
      const body = await response.json() as { detail?: { code?: string } };
      code = body.detail?.code ?? code;
    } catch {
      // Preserve the deterministic HTTP fallback code.
    }
    throw new CodingRequestError(response.status, code);
  }
  return response.json() as Promise<T>;
}

export function readRepositoryRef(repository = CODING_REPOSITORY, ref = CODING_TARGET_REF): Promise<RepositoryTruthResult> {
  return requestJson(`/api/coding/repository/ref?${query({ repository, ref })}`);
}

export function readRepositoryTree(repository: string, ref: string, path = ""): Promise<RepositoryTruthResult> {
  return requestJson(`/api/coding/repository/tree?${query({ repository, ref, path })}`);
}

export function readRepositoryFile(repository: string, ref: string, path: string): Promise<RepositoryTruthResult> {
  return requestJson(`/api/coding/repository/file?${query({ repository, ref, path })}`);
}

export function readSafeGithubUrl(repository: string, commitSha: string, path?: string): Promise<RepositoryTruthResult> {
  return requestJson(`/api/coding/repository/url?${query({ repository, commit_sha: commitSha, path })}`);
}

export function searchRepository(repository: string, ref: string, literal: string): Promise<RepositoryTruthResult> {
  return requestJson(`/api/coding/repository/search?${query({ repository, ref, literal })}`);
}

export function readPullRequest(repository: string, prNumber: number): Promise<RepositoryTruthResult> {
  return requestJson(`/api/coding/repository/pull-request?${query({ repository, pr_number: prNumber })}`);
}

export function readChecks(repository: string, prNumber: number, expectedHeadSha: string): Promise<RepositoryTruthResult> {
  return requestJson(`/api/coding/repository/checks?${query({ repository, pr_number: prNumber, expected_head_sha: expectedHeadSha })}`);
}

export function readReviews(repository: string, prNumber: number, expectedHeadSha: string): Promise<RepositoryTruthResult> {
  return requestJson(`/api/coding/repository/reviews?${query({ repository, pr_number: prNumber, expected_head_sha: expectedHeadSha })}`);
}

export function readRuntimeTruth(repository = CODING_REPOSITORY, targetRef = CODING_TARGET_REF): Promise<RuntimeTruth> {
  return requestJson(`/api/coding/runtime-truth?${query({ repository, target_ref: targetRef })}`);
}

export function readControlRoom(repository = CODING_REPOSITORY, targetRef = CODING_TARGET_REF): Promise<ControlRoom> {
  return requestJson(`/api/coding/control-room?${query({ repository, target_ref: targetRef })}`);
}

export function readPipelineState(repository: string, prNumber: number, specId: string): Promise<Record<string, unknown>> {
  return requestJson(`/api/coding/pipeline-state?${query({ repository, pr_number: prNumber, spec_id: specId })}`);
}

type ExactCodingTarget = {
  workspace_id: string;
  repository: string;
  base_ref: string;
  base_sha: string;
  target_paths: string[];
};

export function inspectCodingTarget(payload: ExactCodingTarget): Promise<CodingActionResult> {
  return requestJson("/api/coding/actions/inspect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

export function previewCodingContext(payload: ExactCodingTarget): Promise<CodingContextPreview> {
  return requestJson("/api/coding/actions/context-preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

export function suggestCodingModification(payload: ExactCodingTarget & {
  intent: string;
  added_context_refs?: unknown[];
  expected_context_digest?: string | null;
  expected_checks?: string[];
}): Promise<CodingActionResult> {
  return requestJson("/api/coding/actions/suggest-modification", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

export type RelayMonitorItem = Readonly<{
  kind: "message" | "tool" | "output" | "edit" | "error" | "done" | "turn" | "mission" | "gap";
  mark: string;
  text: string;
  detail?: string;
  failed?: boolean;
  session: string | null;
  turn: string | null;
  offset: number;
}>;

export type RelayMonitorPage = Readonly<{
  state: "ok" | "unknown" | "refused";
  reason?: string;
  mission?: string | null;
  items?: readonly RelayMonitorItem[];
  cursor?: string | null;
  more?: boolean;
}>;

export type RelayMonitorSnapshot = Readonly<{
  state: string;
  reason?: string | null;
  at?: string;
  owner?: boolean | null;
  stop?: boolean;
  mission?: Readonly<{ id: string; task?: string; label?: string | null; agent?: string | null }> | null;
  turn?: Readonly<{ turn?: string; status?: string | null; at?: string | null; reason?: string | null; next_step?: string | null; running?: boolean }>;
  roadmap?: Readonly<{ enabled: boolean; cycle: number | null; paused: string | null }>;
  focus?: string | null;
  question?: string | null;
  problems?: readonly string[];
  incidents?: readonly string[];
}>;

export function readRelayMonitor(repository = CODING_REPOSITORY): Promise<RelayMonitorSnapshot> {
  return requestJson(`/api/coding/relay-monitor?${query({ repository })}`);
}

export type ProviderQuotaWindow = Readonly<{
  id: string;
  label: string | null;
  kind: "five_hour" | "weekly" | "frontier" | "pool";
  used_percent: number;
  remaining_percent: number;
  resets_at: string | null;
  exhausted: boolean;
  stale: boolean;
}>;

export type ProviderQuota = Readonly<{
  provider: "claude" | "codex" | "antigravity";
  label: string;
  source: string | null;
  status: "ok" | "unavailable" | "unauthenticated";
  reason?: string;
  as_of?: string | null;
  windows: readonly ProviderQuotaWindow[];
}>;

export type ProviderQuotas = Readonly<{
  state: "ok";
  read_at: string;
  next_read_at?: string;
  providers: readonly ProviderQuota[];
}>;

export function readProviderQuotas(repository = CODING_REPOSITORY, refresh = false): Promise<ProviderQuotas> {
  return requestJson(`/api/coding/provider-quotas?${query({ repository, refresh: refresh ? "true" : undefined })}`);
}

export function readRelayMonitorTimeline(cursor: string | null, repository = CODING_REPOSITORY): Promise<RelayMonitorPage> {
  return requestJson(`/api/coding/relay-monitor/timeline?${query({ repository, cursor: cursor ?? undefined })}`);
}
