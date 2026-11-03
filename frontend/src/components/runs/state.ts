export type RequestContext = Readonly<{ generation: number; identity: string }>;

export function acceptsResponse(request: RequestContext, currentGeneration: number, currentIdentity: string): boolean {
  return request.generation === currentGeneration && request.identity === currentIdentity;
}

export function chooseSelection(currentId: string | null, rows: ReadonlyArray<{ id: string }>): string | null {
  if (currentId && rows.some((row) => row.id === currentId)) return currentId;
  return rows[0]?.id ?? null;
}

export type PayloadProjection =
  | { state: "empty"; text: string }
  | { state: "malformed"; text: string }
  | { state: "ok"; text: string; truncated: boolean };

const MAX_DEPTH = 4;
const MAX_ITEMS = 50;
const MAX_STRING = 2_000;
const MAX_TOTAL = 20_000;
const TRUNCATED = "[presentation truncated]";

function bound(value: unknown, depth: number): unknown {
  if (depth >= MAX_DEPTH && value !== null && typeof value === "object") return TRUNCATED;
  if (typeof value === "string") return value.length > MAX_STRING ? `${value.slice(0, MAX_STRING)}… ${TRUNCATED}` : value;
  if (Array.isArray(value)) {
    const items = value.slice(0, MAX_ITEMS).map((item) => bound(item, depth + 1));
    if (value.length > MAX_ITEMS) items.push(TRUNCATED);
    return items;
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    const limited = entries.slice(0, MAX_ITEMS).map(([key, item]) => [key, bound(item, depth + 1)] as const);
    const result = Object.fromEntries(limited) as Record<string, unknown>;
    if (entries.length > MAX_ITEMS) result.__presentation__ = TRUNCATED;
    return result;
  }
  return value;
}

export function projectPayload(raw: string | null, label: string): PayloadProjection {
  if (raw === null) return { state: "empty", text: `No persisted ${label} payload` };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw) as unknown;
  } catch (error) {
    const message = error instanceof Error ? error.message.slice(0, 240) : "Invalid JSON";
    return { state: "malformed", text: `Payload unavailable / malformed — ${message}` };
  }
  let text = JSON.stringify(bound(parsed, 0), null, 2);
  let truncated = text.includes(TRUNCATED);
  if (text.length > MAX_TOTAL) {
    text = `${text.slice(0, MAX_TOTAL)}\n${TRUNCATED}`;
    truncated = true;
  }
  return { state: "ok", text, truncated };
}

export function visibleRuns<T extends { id: string; run_label: string | null; status: string }>(rows: readonly T[], query: string, status: string): T[] {
  const needle = query.trim().toLocaleLowerCase();
  return rows.filter((row) => {
    const matchesQuery = !needle || row.id.toLocaleLowerCase().includes(needle) || (row.run_label ?? "").toLocaleLowerCase().includes(needle);
    return matchesQuery && (status === "" || row.status === status);
  });
}
