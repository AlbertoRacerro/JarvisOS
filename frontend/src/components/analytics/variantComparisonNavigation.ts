const MAX_ID_CODE_POINTS = 256;

export type SourceRunTarget = Readonly<{ workspaceId: string; runId: string }>;

function validId(value: string | null): value is string {
  if (value === null || value.trim().length === 0) return false;
  return Array.from(value).length <= MAX_ID_CODE_POINTS;
}

export function sourceRunHref(workspaceId: string, runId: string): string {
  const params = new URLSearchParams({ workspace: workspaceId, run: runId });
  return `/runs?${params.toString()}`;
}

export function parseSourceRunTarget(search: string): SourceRunTarget | null {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const workspaces = params.getAll("workspace");
  const runs = params.getAll("run");
  if (workspaces.length !== 1 || runs.length !== 1) return null;
  const workspaceId = workspaces[0];
  const runId = runs[0];
  if (!validId(workspaceId) || !validId(runId)) return null;
  return { workspaceId, runId };
}
