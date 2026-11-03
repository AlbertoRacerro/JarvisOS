export type RequestContext = {
  generation: number;
  workspaceId: string;
  candidateId?: string | null;
  artifactId?: string | null;
};

export type MutationKind = "create" | "archive" | "promote";

export type MutationContext = RequestContext & {
  kind: MutationKind;
};

export type CandidateIdentity = {
  id: string;
  status: string;
};

export function acceptsRequest(current: RequestContext, completed: RequestContext): boolean {
  return (
    current.generation === completed.generation &&
    current.workspaceId === completed.workspaceId &&
    (current.candidateId ?? null) === (completed.candidateId ?? null) &&
    (current.artifactId ?? null) === (completed.artifactId ?? null)
  );
}

export function acceptsMutation(current: RequestContext, completed: MutationContext): boolean {
  return (
    current.generation === completed.generation &&
    current.workspaceId === completed.workspaceId &&
    (current.candidateId ?? null) === (completed.candidateId ?? null)
  );
}

export function revalidateSelection(
  candidates: CandidateIdentity[],
  selectedId: string | null,
  showArchived: boolean
): string | null {
  const visible = candidates.filter((candidate) => showArchived || candidate.status !== "archived");
  if (selectedId && visible.some((candidate) => candidate.id === selectedId)) return selectedId;
  return visible[0]?.id ?? null;
}

export function mutationConflicts(pending: MutationKind | null, _requested: MutationKind): boolean {
  return pending !== null;
}

export function duplicateBrief(sourceBrief: string): { briefText: string; backendMutation: false } {
  return { briefText: sourceBrief, backendMutation: false };
}
