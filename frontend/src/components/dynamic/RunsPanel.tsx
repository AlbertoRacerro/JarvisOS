import { useCallback, useEffect, useState } from "react";
import { cancelDynamicRun, failureText, getDynamicRun, listDynamicRuns, startDynamicRun } from "./dynamicApi";
import { isTerminal, linkBadge, runTitle, staleness, type DynRun, type LinkInfo } from "./dynamicLogic";
import RunResultsView from "./RunResultsView";
import { Refusal, SimulationNotice } from "./SimulationNotice";

// Spec 173 decisions 9 and 12: start, poll, cancel and list runs; staleness mirrors the backend `current` flag; re-run is offered, never automatic.
export function useDynamicRuns(workspaceId: string, draftId: string, revision: string) {
  const [runs, setRuns] = useState<DynRun[]>([]);
  const [problem, setProblem] = useState<{ code: string | null; message: string } | null>(null);
  // False until the first read settles, so "no run yet" is never shown before the runs were read.
  const [loaded, setLoaded] = useState(false);
  const refresh = useCallback(async () => {
    try {
      const list = await listDynamicRuns(workspaceId, draftId, 30);
      setRuns(await Promise.all(list.map((run) => getDynamicRun(workspaceId, draftId, run.job_id).catch(() => run))));
      setProblem(null);
    } catch (cause) { setProblem(failureText(cause)); } finally { setLoaded(true); }
  }, [workspaceId, draftId]);
  useEffect(() => { void refresh(); }, [refresh, revision]);
  const polling = runs.some((run) => !isTerminal(run.status));
  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(() => { void refresh(); }, 1500);
    return () => window.clearInterval(timer);
  }, [polling, refresh]);
  return { runs, refresh, problem, loaded };
}

type Props = { workspaceId: string; draftId: string; headSeq: number; scenarios: string[]; runs: DynRun[]; refresh(): Promise<void>; runLinks: Record<string, LinkInfo>; problem: { code: string | null; message: string } | null };

export default function RunsPanel({ workspaceId, draftId, headSeq, scenarios, runs, refresh, runLinks, problem: listProblem }: Props) {
  const [scenarioId, setScenarioId] = useState(scenarios[0] ?? "");
  const [selected, setSelected] = useState<string | null>(null);
  const [problem, setProblem] = useState<{ code: string | null; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setScenarioId((current) => (scenarios.includes(current) ? current : scenarios[0] ?? "")); }, [scenarios]);
  const act = async (action: () => Promise<void>) => { setBusy(true); setProblem(null); try { await action(); await refresh(); } catch (cause) { setProblem(failureText(cause)); } finally { setBusy(false); } };
  const start = (id: string) => act(async () => { const started = await startDynamicRun(workspaceId, draftId, id); setSelected(started.job_id); });
  const chosen = runs.find((run) => run.job_id === selected) ?? null;

  return <div className="dyn-section" aria-label="Runs and results">
    <SimulationNotice />
    <div className="dyn-row">
      <label>Scenario to run<select aria-label="Scenario to run" value={scenarioId} onChange={(event) => setScenarioId(event.target.value)}>
        {scenarios.length ? null : <option value="">No scenario yet</option>}{scenarios.map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
      <button type="button" disabled={busy || !scenarioId} onClick={() => void start(scenarioId)}>Start run</button>
      <button type="button" disabled={busy} onClick={() => void refresh()}>Refresh runs</button>
    </div>
    <p className="dyn-hint">Start runs the saved draft revision {headSeq}. It never starts by itself after an edit.</p>
    {problem ? <Refusal code={problem.code} message={problem.message} /> : null}{listProblem ? <Refusal code={listProblem.code} message={listProblem.message} /> : null}
    {runs.length ? <ul className="dyn-runs" aria-label="Dynamic runs">{runs.map((run) => {
      const state = staleness(run, headSeq); const link = runLinks[run.job_id] ?? null; const badge = linkBadge(link);
      return <li key={run.job_id} data-job={run.job_id} data-status={run.status} data-staleness={state.state} className={selected === run.job_id ? "is-selected" : ""}>
        <strong>{runTitle(run)}</strong> · <span data-run-status>{run.status}</span>{!isTerminal(run.status) && typeof run.progress === "number" ? ` ${Math.round(run.progress * 100)} %` : ""}
        {" "}· <span className={`dyn-stale dyn-stale--${state.state}`} title={state.detail}>{state.label}</span>
        {badge ? <span className={`dyn-badge dyn-badge--${badge.tone}`}> · {badge.text}</span> : null}
        <span className="dyn-actions">
          <button type="button" aria-label={`View results of run ${run.job_id.slice(0, 8)}`} onClick={() => setSelected(run.job_id)}>View results</button>
          {!isTerminal(run.status) ? <button type="button" disabled={busy} aria-label={`Cancel run ${run.job_id.slice(0, 8)}`} onClick={() => void act(async () => { await cancelDynamicRun(workspaceId, draftId, run.job_id); })}>Cancel run</button> : null}
          {state.state === "stale" && isTerminal(run.status) ? <button type="button" disabled={busy} aria-label={`Re-run scenario ${run.scenario_id}`} onClick={() => void start(run.scenario_id)}>Re-run</button> : null}
        </span></li>;
    })}</ul> : <p>No dynamic run exists for this draft yet.</p>}
    {chosen ? <RunResultsView workspaceId={workspaceId} draftId={draftId} run={chosen} headSeq={headSeq} link={runLinks[chosen.job_id] ?? null} onRerun={() => void start(chosen.scenario_id)} /> : null}
  </div>;
}
