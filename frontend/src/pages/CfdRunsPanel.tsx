import { useCallback, useEffect, useRef, useState } from "react";
import { designTarget } from "../app/designTargets";
import { getComponentCfdRunEvents, listComponentCfdRuns, startComponentCfdRun, stopComponentCfdRun,
  type CfdRun, type StudyTemplate } from "../api/designProject";
import "../components/process/SolverConsole.css";
import { Empty } from "../components/design/DesignStatus";
import { regimeLabel, runLabel } from "../components/design/designText";
import NumericInput from "../components/ui/NumericInput";
import { applyEvents, canRetry, canStop, emptyConsole, isActiveState, runStateText, type ConsoleState } from "./cfdRunConsole";
import { shortDigest } from "./studyTemplateForm";

/** `onRunsChange` hears every runs list this panel reads, so the results comparison follows runs started or finished here. */
type Props = { projectId: string; versionId: string; componentId: string; templates: StudyTemplate[]; onRunsChange?(runs: CfdRun[]): void };

const POLL_MS = 1000;
const message = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback;
const fmt = (value: number | null) => value == null ? "n/a" : Number(value).toPrecision(4);

/** Spec 216 contract 4: start, follow, stop and retry background pipe-case runs. Runs are evidence only. */
export default function CfdRunsPanel({ projectId, versionId, componentId, templates, onRunsChange }: Props) {
  const heard = useRef(onRunsChange);
  heard.current = onRunsChange;
  const [runs, setRuns] = useState<CfdRun[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [view, setView] = useState<ConsoleState>(emptyConsole);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cursor = useRef(0);
  const scope = `${projectId}/${versionId}/${componentId}`;

  const refresh = useCallback(async () => {
    try { const list = await listComponentCfdRuns(projectId, versionId, componentId); setRuns(list); heard.current?.(list); }
    catch (cause) { setRuns((current) => current ?? []); setError(message(cause, "CFD runs are unavailable.")); }
  }, [projectId, versionId, componentId]);
  useEffect(() => { setRuns(null); setSelectedId(null); setError(null); void refresh(); }, [scope, refresh]);
  useEffect(() => { if (runs && !selectedId && runs.length) setSelectedId(runs[0]!.run_id); }, [runs, selectedId]);

  const selected = runs?.find((run) => run.run_id === selectedId) ?? null;
  const following = selected !== null && isActiveState(selected.state);

  // Replay the selected run's events from the start, then follow by cursor until the server reports a terminal state.
  useEffect(() => {
    cursor.current = 0;
    setView(emptyConsole(selectedId));
    if (!selectedId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const page = await getComponentCfdRunEvents(projectId, versionId, componentId, selectedId, cursor.current);
        if (!active) return;
        cursor.current = Math.max(cursor.current, page.cursor);
        setView((current) => applyEvents(current, page));
        if (page.events.length >= 500) { void poll(); return; }  // a full page: more is waiting
        if (!page.live) { void refresh(); return; }
        setRuns((list) => list && list.map((run) => run.run_id === selectedId && run.state !== page.state ? { ...run, state: page.state } : run));
      } catch (cause) {
        if (!active) return;
        setError(message(cause, "Following the run failed."));
      }
      if (active) timer = setTimeout(() => void poll(), POLL_MS);
    };
    void poll();
    return () => { active = false; if (timer) clearTimeout(timer); };
  }, [projectId, versionId, componentId, selectedId, refresh]);

  const act = async (work: () => Promise<CfdRun>) => {
    setBusy(true); setError(null);
    try {
      const run = await work();
      setSelectedId(run.run_id);
    } catch (cause) { setError(message(cause, "The CFD run request failed.")); }
    finally { setBusy(false); await refresh(); }
  };
  const [ranksText, setRanksText] = useState("1");
  const ranks = Math.min(64, Math.max(1, Math.trunc(Number(ranksText)) || 1));
  const start = (regime: string, retryOf?: string) => act(() => startComponentCfdRun(projectId, versionId, componentId, regime, retryOf, ranks));
  const stop = (runId: string) => act(() => stopComponentCfdRun(projectId, versionId, componentId, runId));
  const anyActive = runs?.some((run) => isActiveState(run.state)) ?? false;
  const rows = Object.values(view.residuals);

  return <section aria-label="CFD runs" className="design-project__runs" {...designTarget("design.panel.cfd-runs")}>
    <h4 title="A run executes a saved template once on this machine">CFD runs</h4>
    {error ? <p role="alert" className="design-project__error">{error}</p> : null}
    <label>MPI ranks <NumericInput min={1} max={64} integer value={ranksText} disabled={busy} onValueChange={setRanksText} /></label>
    <div className="design-project__context-links">{templates.length ? templates.map((item) => <button key={item.regime} type="button" disabled={busy || anyActive}
      title={anyActive ? "One case runs at a time" : `Template ${shortDigest(item.template_digest)}`} onClick={() => void start(item.regime)}>Run {regimeLabel(item.regime)}</button>)
      : <button type="button" disabled title="Record a study template first">Run</button>}</div>
    {runs === null ? <p>Loading CFD runs…</p>
      : <ul className="design-project__reduced-list" aria-label="CFD run list">{!runs.length ? <li className="design-project__reduced-item"><Empty /></li>
        : runs.map((run) => <li key={run.run_id} className="design-project__reduced-item" title={run.run_id}>
          <button type="button" aria-pressed={run.run_id === selectedId} onClick={() => setSelectedId(run.run_id)}>{runLabel(run)}</button>{" "}
          {run.state}{run.processors && run.processors > 1 ? ` · ${run.processors} ranks` : ""}
          {run.retry_of ? <> · <button type="button" title={run.retry_of} onClick={() => setSelectedId(run.retry_of)} disabled={!runs.some((item) => item.run_id === run.retry_of)}>retry of…</button></> : null}
        </li>)}</ul>}
    {selected ? <section className={`solver-console solver-console--${selected.state}`} aria-label="CFD run console">
      <header>
        <strong title={selected.run_id}>{runLabel(selected)}</strong>
        <span role="status" aria-live="polite" className="solver-console-state">{runStateText(selected)}</span>
        {canStop(selected.state) || selected.state === "stopping" ? <button type="button" className="solver-stop" disabled={busy || selected.state === "stopping"}
          onClick={() => void stop(selected.run_id)}>{selected.state === "stopping" ? "Stopping…" : "Stop"}</button> : null}
        {canRetry(selected.state) ? <button type="button" disabled={busy || anyActive} onClick={() => void start(selected.regime, selected.run_id)}>Retry</button> : null}
      </header>
      <p className="draft-hint">{selected.qualifies_publication ? "This run is marked as qualifying." : "Evidence only: this run does not qualify publication."}</p>
      {selected.state === "finished" || selected.solver_status ? <p>Mesh {selected.mesh_status ?? "n/a"} · solver {selected.solver_status ?? "n/a"} · {selected.iterations ?? 0} iterations ·
        {selected.residual_converged ? " residuals converged" : " residuals not converged"}</p> : null}
      {rows.length ? <table className="solver-diff" aria-label="Latest residual per field">
        <caption>Latest residual per field{following ? " (live)" : ""}</caption>
        <thead><tr><th>Field</th><th>Iteration</th><th>Initial</th><th>Final</th></tr></thead>
        <tbody>{rows.map((row) => <tr key={row.field}><td>{row.field}</td><td>{row.iteration}</td><td>{fmt(row.initial)}</td><td>{fmt(row.final)}</td></tr>)}</tbody>
      </table> : following ? <p className="draft-hint">Waiting for the first solver iteration…</p> : null}
      <pre aria-label="CFD run log" tabIndex={0} style={{ maxHeight: "16rem", overflow: "auto", whiteSpace: "pre-wrap" }}>
        {view.lines.map((item) => `${item.stream === "stderr" ? "[err] " : ""}${item.line}`).join("\n") || (following ? "Waiting for output…" : "No log lines were recorded.")}
      </pre>
      {view.droppedLines ? <p className="draft-hint">The oldest {view.droppedLines} log lines are hidden in this view.</p> : null}
      {selected.events_truncated ? <p className="draft-hint">The run log reached its size bound; later output was not kept.</p> : null}
    </section> : null}
  </section>;
}
