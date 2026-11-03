// Spec 216 contract 4: pure state for the Design CFD run console (cursor-paged events, honest run states).
import type { CfdRun, CfdRunEvent, CfdRunEvents } from "../api/designProject";

export const MAX_CONSOLE_LINES = 400;
const ACTIVE = new Set(["queued", "running", "stopping"]);

export type ResidualRow = { field: string; iteration: number; initial: number | null; final: number | null };
export type ConsoleLine = { seq: number; stream: string; line: string };
export type ConsoleState = {
  runId: string | null; cursor: number; lines: ConsoleLine[]; residuals: Record<string, ResidualRow>; droppedLines: number;
};

export const emptyConsole = (runId: string | null = null): ConsoleState => ({ runId, cursor: 0, lines: [], residuals: {}, droppedLines: 0 });
export const isActiveState = (state: string) => ACTIVE.has(state);
export const canStop = (state: string) => state === "queued" || state === "running";
export const canRetry = (state: string) => !isActiveState(state);

/** Folds one events page into the console; a page for another run or a stale cursor is ignored. */
export function applyEvents(current: ConsoleState, page: CfdRunEvents): ConsoleState {
  if (page.run_id !== current.runId) return current;
  const fresh = page.events.filter((event: CfdRunEvent) => event.seq > current.cursor);
  if (!fresh.length) return current;
  const residuals = { ...current.residuals };
  let lines = current.lines;
  for (const event of fresh) {
    if (event.kind === "log") lines = [...lines, { seq: event.seq, stream: event.stream, line: event.line }];
    else for (const field of Object.keys(event.initial)) {
      residuals[field] = { field, iteration: event.iteration, initial: event.initial[field] ?? null, final: event.final[field] ?? null };
    }
  }
  const dropped = Math.max(0, lines.length - MAX_CONSOLE_LINES);
  return { runId: current.runId, cursor: fresh[fresh.length - 1]!.seq, lines: dropped ? lines.slice(dropped) : lines,
    residuals, droppedLines: current.droppedLines + dropped };
}

/** One truthful sentence per state; stopped and interrupted are never worded as finished. */
export function runStateText(run: Pick<CfdRun, "state" | "message" | "case_status" | "solver_reason" | "mesh_reason">): string {
  switch (run.state) {
    case "queued": return "Queued";
    case "running": return "Running";
    case "stopping": return "Stopping — waiting for the server to confirm the case has ended";
    case "stopped": return "Stopped before completion; no result was recorded";
    case "interrupted": return run.message || "Interrupted; no result was recorded";
    case "failed": return `Failed${run.message || run.solver_reason || run.mesh_reason ? ` — ${run.message || run.solver_reason || run.mesh_reason}` : ""}`;
    case "refused": return `Refused — ${run.mesh_reason || run.solver_reason || "the mesh or solver was not accepted"}; no result was recorded`;
    case "finished": return "Finished";
    default: return String(run.state);
  }
}
