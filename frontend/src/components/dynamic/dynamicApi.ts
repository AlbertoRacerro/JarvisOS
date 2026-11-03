import { StudioApiError, request } from "../../api/studio";
import { DraftApiError } from "../../api/processDraft";
import { listProcedureLinks, listProcedureRuns, type ProcedureLink } from "../../api/studioProcedure";
import { collectSeries, type DynRun, type LinkInfo, type Manifest, type SeriesData, type SeriesFetch } from "./dynamicLogic";

// Spec 173: the 172 dynamic run routes, read-only apart from start and cancel. No new route; the base is the draft's dynamic API.
export type EnvProfile = { profile_id: string; digest: string; name: string; start: string; end: string; channels: Record<string, string>; integrity_error?: string };
const base = (workspaceId: string, draftId: string) => `/workspaces/${encodeURIComponent(workspaceId)}/process/drafts/${encodeURIComponent(draftId)}/dynamic`;
export const listDynamicRuns = (ws: string, draftId: string, limit = 50) => request<DynRun[]>(`${base(ws, draftId)}/runs?limit=${limit}`);
export const getDynamicRun = (ws: string, draftId: string, jobId: string) => request<DynRun>(`${base(ws, draftId)}/runs/${encodeURIComponent(jobId)}`);
export const startDynamicRun = (ws: string, draftId: string, scenarioId: string) => request<{ job_id: string; status: string }>(`${base(ws, draftId)}/runs`, "POST", { scenario_id: scenarioId });
export const cancelDynamicRun = (ws: string, draftId: string, jobId: string) => request<DynRun>(`${base(ws, draftId)}/runs/${encodeURIComponent(jobId)}/cancel`, "POST", {});
export const getDynamicManifest = (ws: string, draftId: string, jobId: string) => request<Manifest>(`${base(ws, draftId)}/runs/${encodeURIComponent(jobId)}/manifest`);
export type SeriesQuery = { channels?: string[]; start_s?: number; end_s?: number; max_points?: number };
export const getDynamicSeries = (ws: string, draftId: string, jobId: string, query: SeriesQuery) => {
  const params = new URLSearchParams();
  if (query.channels?.length) params.set("channels", query.channels.join(","));
  if (query.start_s !== undefined) params.set("start_s", String(query.start_s));
  if (query.end_s !== undefined) params.set("end_s", String(query.end_s));
  params.set("max_points", String(query.max_points ?? 5000));
  return request<SeriesData>(`${base(ws, draftId)}/runs/${encodeURIComponent(jobId)}/series?${params}`);
};
export const seriesFetcher = (ws: string, draftId: string, jobId: string): SeriesFetch => (query) => getDynamicSeries(ws, draftId, jobId, query);
export const exportRunRows = (ws: string, draftId: string, jobId: string, channels: string[]) => collectSeries(seriesFetcher(ws, draftId, jobId), channels);
export const listEnvProfiles = (ws: string) => request<EnvProfile[]>(`/workspaces/${encodeURIComponent(ws)}/environment/profiles`);

/** 191 link info, read-only. Never written from here. */
export const listDraftLinks = (ws: string, draftId: string): Promise<LinkInfo[]> => listProcedureLinks(ws, draftId).then((links: ProcedureLink[]) => links);
export const listRunLinks = (ws: string, draftId: string) => listProcedureRuns(ws, draftId).then((rows) => Object.fromEntries(rows.map((row) => [row.job.job_id, row.procedure_link])));
export const failureText = (cause: unknown): { code: string | null; message: string } =>
  cause instanceof StudioApiError || cause instanceof DraftApiError ? { code: cause.code, message: cause.message } : { code: null, message: cause instanceof Error ? cause.message : "Request failed" };
