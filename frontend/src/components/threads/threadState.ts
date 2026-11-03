import type { ThreadSummary } from "../../api/threads";

export type RequestContext = Readonly<{
  generation: number;
  workspaceId: string | null;
  threadId: string | null;
}>;

export type ThreadState = Readonly<{
  workspaceGeneration: number;
  detailGeneration: number;
  submitGeneration: number;
  workspaceId: string | null;
  selectedThreadId: string | null;
}>;

export const initialThreadState = (): ThreadState => ({
  workspaceGeneration: 0,
  detailGeneration: 0,
  submitGeneration: 0,
  workspaceId: null,
  selectedThreadId: null
});

export function withWorkspace(state: ThreadState, workspaceId: string | null): ThreadState {
  return {
    workspaceGeneration: state.workspaceGeneration + 1,
    detailGeneration: state.detailGeneration + 1,
    submitGeneration: state.submitGeneration + 1,
    workspaceId,
    selectedThreadId: null
  };
}

export function withThread(state: ThreadState, threadId: string | null): ThreadState {
  return {
    ...state,
    detailGeneration: state.detailGeneration + 1,
    submitGeneration: state.submitGeneration + 1,
    selectedThreadId: threadId
  };
}

export const listContext = (state: ThreadState): RequestContext => ({
  generation: state.workspaceGeneration,
  workspaceId: state.workspaceId,
  threadId: null
});

export const detailContext = (state: ThreadState): RequestContext => ({
  generation: state.detailGeneration,
  workspaceId: state.workspaceId,
  threadId: state.selectedThreadId
});

export const submitContext = (state: ThreadState): RequestContext => ({
  generation: state.submitGeneration,
  workspaceId: state.workspaceId,
  threadId: state.selectedThreadId
});

export function contextMatches(current: RequestContext, captured: RequestContext): boolean {
  return current.generation === captured.generation
    && current.workspaceId === captured.workspaceId
    && current.threadId === captured.threadId;
}

export function orderThreads(threads: readonly ThreadSummary[]): ThreadSummary[] {
  return [...threads].sort((left, right) => {
    const activity = right.last_activity_at.localeCompare(left.last_activity_at);
    return activity || left.id.localeCompare(right.id);
  });
}

export function chooseThread(
  threads: readonly ThreadSummary[],
  preferredId: string | null
): string | null {
  if (preferredId && threads.some((thread) => thread.id === preferredId)) return preferredId;
  return orderThreads(threads)[0]?.id ?? null;
}

export function boundedText(value: string | null | undefined, limit = 160): string {
  if (!value) return "—";
  return value.length <= limit ? value : `${value.slice(0, limit)}…`;
}
