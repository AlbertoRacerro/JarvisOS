import { useEffect } from "react";
import type { CfdRun } from "../api/designProject";
import { designTarget } from "../app/designTargets";
import type { DesignPanelState } from "../app/workspaceActionSurface";
import { DesignStatus } from "../components/design/DesignStatus";
import type { NodeBadge } from "../components/design/designNodeStatus";
import { regimeLabel, runLabel, shortWhen } from "../components/design/designText";
import CfdFieldSlicePanel from "./CfdFieldSlicePanel";
import { comparedRuns, newestFirst, runBadge, toggleCompared } from "./cfdResultsCompare";

/** `null` while the runs are read; `unavailable` when the read failed. */
export type CfdRunsRead = CfdRun[] | null | "unavailable";

const study = (run: CfdRun) => { const regime = regimeLabel(run.regime); return `${regime.charAt(0).toUpperCase()}${regime.slice(1)}`; };
function inputs(run: CfdRun): string {
  const measured = run.measurement?.status === "measured" ? run.measurement : null;
  return [typeof measured?.reynolds === "number" ? `Re ${Math.round(measured.reynolds)}` : null,
    typeof measured?.mesh_level === "number" ? `mesh level ${String(measured.mesh_level).replace("-", "−")}` : null,
    shortWhen(run.finished_at ?? run.queued_at)].filter(Boolean).join(" · ");
}
const readBadge = (runs: CfdRunsRead): NodeBadge => runs === null ? { state: "checking" } : runs === "unavailable" ? { state: "unknown", detail: "Could not be read" } : { state: "empty" };

/** Inspector: every run of the component as a compact row; checked runs are stacked in the viewport. */
export function CfdRunPicker({ runs, chosen, onChoose }: Readonly<{ runs: CfdRunsRead; chosen: readonly string[] | null; onChoose(next: string[]): void }>) {
  const list = Array.isArray(runs) ? newestFirst(runs) : [];
  const shown = new Set(Array.isArray(runs) ? comparedRuns(runs, chosen).map((run) => run.run_id) : []);
  const badge = readBadge(runs);
  return <fieldset className="design-project__compare" {...designTarget("design.field.run")}>
    <legend>Compare runs</legend>
    {list.length ? <ul>{list.map((run) => { const status = runBadge(run); return <li key={run.run_id}>
      <label title={run.run_id}><input type="checkbox" checked={shown.has(run.run_id)}
        onChange={() => onChoose(toggleCompared(list, chosen, run.run_id))} /> {runLabel(run)}</label>
      <DesignStatus state={status.state} title={status.detail} />
    </li>; })}</ul>
      : <p><DesignStatus state={badge.state} title={badge.detail} /></p>}
  </fieldset>;
}

/** Viewport: one block per compared run, stacked; without one, the same block reads empty. */
export function CfdResultBlocks({ projectId, versionId, componentId, runs, chosen, onFieldState }: Readonly<{
  projectId: string; versionId: string; componentId: string; runs: CfdRunsRead; chosen: readonly string[] | null;
  onFieldState?: (state: DesignPanelState) => void;
}>) {
  const shown = Array.isArray(runs) ? comparedRuns(runs, chosen) : [];
  const emptyState: DesignPanelState = runs === null ? "loading" : runs === "unavailable" ? "unavailable" : "empty";
  useEffect(() => { if (!shown.length) onFieldState?.(emptyState); }, [shown.length, emptyState, onFieldState]);
  if (!shown.length) {
    const badge = readBadge(runs);
    return <div className="design-project__results">
      <article className="design-project__result" aria-label="Run">
        <div className="design-project__result-head"><h3>Run</h3><DesignStatus state={badge.state} title={badge.detail} /></div>
        <CfdFieldSlicePanel projectId={projectId} versionId={versionId} componentId={componentId} run={null} primary />
      </article>
    </div>;
  }
  return <div className="design-project__results">
    {shown.map((run, index) => { const status = runBadge(run); return <article key={run.run_id} className="design-project__result" aria-label={runLabel(run)}>
      <div className="design-project__result-head">
        <h3 title={run.run_id}>{study(run)}</h3><span>{inputs(run)}</span><DesignStatus state={status.state} title={status.detail} />
      </div>
      <CfdFieldSlicePanel projectId={projectId} versionId={versionId} componentId={componentId} run={run} primary={index === 0} onFieldState={index === 0 ? onFieldState : undefined} />
    </article>; })}
  </div>;
}
