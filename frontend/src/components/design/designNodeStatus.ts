// Spec 229 acceptance 4: tree badges for the CFD-owned nodes read the same owners as the stage panels
// (study templates, CFD runs, reduced models), and say "unknown" when that read failed, never "empty".
// Every Design node and panel uses one status vocabulary; counts and specifics go in the detail (a tooltip).
import type { DesignNode } from "./DesignComponentTree";

export type NodeState = "empty" | "ready" | "running" | "checking" | "stale" | "failed" | "unknown";
export type NodeBadge = Readonly<{ state: NodeState; detail?: string }>;

export type CfdOwnerRead =
  | Readonly<{ state: "loading" }>
  | Readonly<{ state: "unavailable" }>
  | Readonly<{ state: "ready"; templates: number; runs: readonly Readonly<{ state: string }>[];
      reduced: readonly Readonly<{ status: string; process_unit_pinned: boolean }>[] }>;

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;
const ACTIVE = new Set(["queued", "running", "stopping"]);
const FAILED = new Set(["failed", "refused", "interrupted"]);

export function cfdNodeStatus(read: CfdOwnerRead, node: DesignNode): NodeBadge | undefined {
  // Studies and Results are one node: the study templates and runs live on Results, next to what they produced.
  if (!["Mesh", "Results", "Reduced model"].includes(node)) return undefined;
  if (read.state === "loading") return { state: "checking" };
  if (read.state === "unavailable") return { state: "unknown", detail: "Could not be read" };
  const runs = read.runs.length;
  if (node === "Mesh") return runs ? { state: "ready", detail: `${runs} run mesh${runs === 1 ? "" : "es"}` } : { state: "empty" };
  if (node === "Results") {
    const finished = read.runs.filter((run) => run.state === "finished").length;
    const templates = read.templates ? ` · ${plural(read.templates, "template")}` : "";
    if (finished) return { state: "ready", detail: `${finished} finished · ${plural(runs, "run")}${templates}` };
    if (read.runs.some((run) => ACTIVE.has(run.state))) return { state: "running", detail: "No finished run yet" };
    if (read.runs.some((run) => FAILED.has(run.state))) return { state: "failed", detail: "No finished run" };
    return runs ? { state: "empty", detail: "No finished run" } : read.templates ? { state: "empty", detail: plural(read.templates, "template") } : { state: "empty" };
  }
  if (!read.reduced.length) return { state: "empty" };
  const current = read.reduced.find((item) => item.status === "current");
  return current ? { state: "ready", detail: current.process_unit_pinned ? "Current · pinned on Process unit" : "Current" }
    : { state: "stale", detail: `${read.reduced.length} published · not current` };
}
