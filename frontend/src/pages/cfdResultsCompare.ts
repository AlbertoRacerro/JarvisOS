// CFD results comparison: which runs a component has, how each reads as a status, and which are shown by default.
// Pure helpers so the selection rules are tested without rendering.
import type { NodeBadge } from "../components/design/designNodeStatus";

type RunLike = Readonly<{ run_id: string; state: string; queued_at: string; finished_at?: string }>;

const ACTIVE = new Set(["queued", "running", "stopping"]);
const FAILED = new Set(["failed", "refused", "interrupted"]);
const when = (run: RunLike) => { const time = Date.parse(run.finished_at ?? run.queued_at); return Number.isNaN(time) ? 0 : time; };

/** Newest first; the server order is kept for runs without a readable time. */
export function newestFirst<T extends RunLike>(runs: readonly T[]): T[] {
  return runs.map((run, index) => ({ run, index })).sort((a, b) => when(b.run) - when(a.run) || a.index - b.index).map((item) => item.run);
}

/** One status word per run; the raw state stays in the tooltip. */
export function runBadge(run: RunLike): NodeBadge {
  if (run.state === "finished") return { state: "ready", detail: "Finished" };
  if (ACTIVE.has(run.state)) return { state: "running", detail: run.state };
  if (FAILED.has(run.state)) return { state: "failed", detail: run.state };
  return { state: "empty", detail: run.state };
}

/** By default the latest finished run is compared; without one, the latest run. */
export function defaultCompareSelection(runs: readonly RunLike[]): string[] {
  const ordered = newestFirst(runs);
  const pick = ordered.find((run) => run.state === "finished") ?? ordered[0];
  return pick ? [pick.run_id] : [];
}

/** The runs shown, in list order: the operator's choice once made, otherwise the default; unknown ids are dropped. */
export function comparedRuns<T extends RunLike>(runs: readonly T[], chosen: readonly string[] | null): T[] {
  const ids = new Set(chosen ?? defaultCompareSelection(runs));
  return newestFirst(runs).filter((run) => ids.has(run.run_id));
}

/** Toggle one run in the comparison, starting from what is shown now. */
export function toggleCompared(runs: readonly RunLike[], chosen: readonly string[] | null, runId: string): string[] {
  const current = comparedRuns(runs, chosen).map((run) => run.run_id);
  return current.includes(runId) ? current.filter((id) => id !== runId) : [...current, runId];
}
