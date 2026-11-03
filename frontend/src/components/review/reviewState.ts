export type ReviewRecordKind = "assumption" | "parameter" | "decision";
export type ReviewRecordStatus = "proposed" | "accepted" | "rejected" | "superseded";
export type ReviewStatusFilter = ReviewRecordStatus | "all";

export type ReviewRecordShape = Readonly<{
  id: string;
  record_kind: ReviewRecordKind;
  status: ReviewRecordStatus;
  created_at: string;
  title: string | null;
  statement: string | null;
  name: string | null;
  supersedes_parameter_id: string | null;
}>;

export type ReviewRequestContext = Readonly<{
  generation: number;
  workspaceId: string;
  statusFilter: ReviewStatusFilter;
}>;

export type ReviewMutationContext = Readonly<{
  generation: number;
  workspaceId: string;
  recordKind: ReviewRecordKind;
  recordId: string;
}>;

export type PromotionRoute = "generic" | "replacement";

export function orderedRecords<T extends ReviewRecordShape>(records: readonly T[]): T[] {
  return [...records].sort((a, b) => {
    const created = b.created_at.localeCompare(a.created_at);
    if (created !== 0) return created;
    const kind = a.record_kind.localeCompare(b.record_kind);
    if (kind !== 0) return kind;
    return a.id.localeCompare(b.id);
  });
}

export function recordLabel(record: ReviewRecordShape): string {
  const candidate = record.title ?? record.name ?? record.statement;
  const trimmed = candidate?.trim();
  if (trimmed) return trimmed;
  return `${record.record_kind} ${record.id.slice(0, 12)}`;
}

export function acceptsReviewRequest(current: ReviewRequestContext | null, completed: ReviewRequestContext): boolean {
  return Boolean(
    current
      && current.generation === completed.generation
      && current.workspaceId === completed.workspaceId
      && current.statusFilter === completed.statusFilter
  );
}

export function acceptsReviewMutation(current: ReviewMutationContext | null, completed: ReviewMutationContext): boolean {
  return Boolean(
    current
      && current.generation === completed.generation
      && current.workspaceId === completed.workspaceId
      && current.recordKind === completed.recordKind
      && current.recordId === completed.recordId
  );
}

export function retainedSelection(records: readonly ReviewRecordShape[], selectedId: string | null): string | null {
  if (selectedId && records.some((record) => record.id === selectedId)) return selectedId;
  return records[0]?.id ?? null;
}

export function nextAfterRemoval(records: readonly ReviewRecordShape[], removedId: string): string | null {
  const index = records.findIndex((record) => record.id === removedId);
  if (index < 0) return records[0]?.id ?? null;
  return records[index + 1]?.id ?? records[index - 1]?.id ?? null;
}

export function promotionRoute(record: ReviewRecordShape): PromotionRoute {
  return record.record_kind === "parameter" && Boolean(record.supersedes_parameter_id) ? "replacement" : "generic";
}

export function isActionable(record: ReviewRecordShape): boolean {
  return record.status === "proposed";
}
