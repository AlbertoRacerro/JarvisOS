import { useEffect, useState } from "react";
import {
  compareRuns,
  listDraftRuns,
  type DraftRun,
  type RunComparison,
  type SettingsDiffRow,
  type SolveEvent,
  type SolveStatus,
} from "../../api/processDraft";
import { fieldLabel } from "./resultsLayout";
import { Empty } from "./RunResults";
import { classificationText, methodLabel, stopReasonText } from "./solverSettings";
import "./SolverConsole.css";

type Series = { key: string; label: string; dashed?: boolean; points: { iteration: number; value: number | null }[] };

const W = 560;
const H = 180;
const PAD = { left: 48, right: 56, top: 12, bottom: 28 };

const fmt = (value: number | null | undefined, digits = 4) => (value == null ? "empty" : Number(value).toPrecision(digits));

/** Spec 210: normalized residual per iteration on a log axis, with the convergence tolerance (1) as a reference line. */
export function ResidualChart({ series, title }: Readonly<{ series: Series[]; title: string }>) {
  const [hover, setHover] = useState<number | null>(null);
  const values = series.flatMap((line) => line.points.map((point) => point.value)).filter((value): value is number => value != null && value > 0);
  const iterations = series.flatMap((line) => line.points.map((point) => point.iteration));
  const maxIteration = Math.max(2, ...iterations);
  const lo = Math.floor(Math.log10(Math.min(0.1, ...values)));
  const hi = Math.ceil(Math.log10(Math.max(10, ...values)));
  const x = (iteration: number) => PAD.left + ((iteration - 1) / (maxIteration - 1)) * (W - PAD.left - PAD.right);
  const y = (value: number) => PAD.top + ((hi - Math.log10(value)) / (hi - lo)) * (H - PAD.top - PAD.bottom);
  const decades = Array.from({ length: hi - lo + 1 }, (_, index) => lo + index);
  const xTicks = Array.from(new Set([1, Math.round(maxIteration / 2), maxIteration])).filter((tick) => tick >= 1);
  const path = (line: Series) => {
    let d = "";
    let pen = false;
    for (const point of line.points) {
      if (point.value == null || point.value <= 0) { pen = false; continue; }
      d += `${pen ? "L" : "M"}${x(point.iteration).toFixed(1)},${y(point.value).toFixed(1)}`;
      pen = true;
    }
    return d;
  };
  const move = (event: React.MouseEvent<SVGRectElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const ratio = (event.clientX - box.left) / box.width;
    setHover(Math.min(maxIteration, Math.max(1, Math.round(1 + ratio * (maxIteration - 1)))));
  };
  const last = series.map((line) => [...line.points].reverse().find((point) => point.value != null && point.value > 0));
  return (
    <figure className="solver-chart" aria-label={title}>
      {series.length > 1 && (
        <div className="solver-chart-legend" role="list" aria-label={`${title} series`}>
          {series.map((line, index) => (
            <span role="listitem" key={line.key}>
              <i className={line.dashed ? "is-dashed" : ""} style={{ borderColor: `var(--color-chart-series-${index + 1})` }} />{line.label}
            </span>
          ))}
        </div>
      )}
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}: ${series.map((line) => `${line.label} ${line.points.length} iterations, last ${fmt(line.points[line.points.length - 1]?.value)}`).join("; ")}`}>
        {decades.map((decade) => (
          <g key={decade}>
            <line className="solver-chart-grid" x1={PAD.left} x2={W - PAD.right} y1={y(10 ** decade)} y2={y(10 ** decade)} />
            <text className="solver-chart-tick" x={PAD.left - 6} y={y(10 ** decade) + 4} textAnchor="end">{`1e${decade}`}</text>
          </g>
        ))}
        <line className="solver-chart-tolerance" x1={PAD.left} x2={W - PAD.right} y1={y(1)} y2={y(1)} />
        <text className="solver-chart-tick" x={W - PAD.right + 4} y={y(1) + 4}>tolerance</text>
        {xTicks.map((tick) => (
          <text key={tick} className="solver-chart-tick" x={x(tick)} y={H - 8} textAnchor="middle">{tick}</text>
        ))}
        <text className="solver-chart-tick" x={W - PAD.right} y={H - 8} textAnchor="start" dx={6}>iteration</text>
        {series.map((line, index) => (
          <g key={line.key}>
            <path d={path(line)} fill="none" stroke={`var(--color-chart-series-${index + 1})`} strokeWidth={2}
              strokeDasharray={line.dashed ? "6 4" : undefined} strokeLinejoin="round" strokeLinecap="round" />
            {last[index] && (
              <circle cx={x(last[index]!.iteration)} cy={y(last[index]!.value!)} r={4} fill={`var(--color-chart-series-${index + 1})`}
                stroke="var(--color-chart-bg)" strokeWidth={2} />
            )}
            {series.length > 1 && last[index] && (
              <text className="solver-chart-label" x={x(last[index]!.iteration) + 8} y={y(last[index]!.value!) + 4}>{line.label}</text>
            )}
          </g>
        ))}
        {hover != null && <line className="solver-chart-crosshair" x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={H - PAD.bottom} />}
        <rect x={PAD.left} y={PAD.top} width={W - PAD.left - PAD.right} height={H - PAD.top - PAD.bottom} fill="transparent"
          onMouseMove={move} onMouseLeave={() => setHover(null)} />
      </svg>
      {hover != null && (
        <figcaption className="solver-chart-tooltip" role="status">
          Iteration {hover}: {series.map((line) => `${line.label} ${fmt(line.points.find((point) => point.iteration === hover)?.value)}`).join(" · ")}
        </figcaption>
      )}
    </figure>
  );
}

/** A few words for the console header; the longer explanation, and any engine message, is the tooltip. */
const stateText = (solve: SolveStatus, events: SolveEvent[]): { text: string; title?: string } => {
  if (solve.state === "stopping") return { text: "Stop requested", title: "Ends at the next iteration boundary" };
  if (solve.state === "running" && solve.events_truncated) return { text: "Running · log full", title: "Later iterations are not shown live; the run record keeps them" };
  if (solve.state === "running") return { text: events.length ? `Live · iteration ${events[events.length - 1]!.iteration}` : "Running" };
  if (solve.state === "interrupted") return { text: "Interrupted", title: "No result was recorded" };
  if (solve.state === "failed") return { text: "Failed", title: solve.message ?? undefined };
  return solve.status === "completed" ? { text: "Recorded · converged" }
    : { text: "Recorded · not converged", title: stopReasonText(solve.reason) };
};

export function SettingsDiff({ rows, caption }: Readonly<{ rows: SettingsDiffRow[]; caption: string }>) {
  if (!rows.length) return <p className="draft-hint">{caption}: no solver-setting difference.</p>;
  const show = (value: unknown) => (value == null ? "default" : typeof value === "object" ? JSON.stringify(value) : String(value));
  return (
    <table className="solver-diff">
      <caption>{caption}</caption>
      <thead><tr><th>Setting</th><th>Before</th><th>After</th></tr></thead>
      <tbody>{rows.map((row) => <tr key={row.key}><td>{row.key}</td><td>{show(row.a)}</td><td>{show(row.b)}</td></tr>)}</tbody>
    </table>
  );
}

/**
 * Spec 210: live console of one background solve. Only a running solve with arriving events is called live.
 * Without a solve (`solve` null, before the first run) or before the first iteration it shows the same readout
 * with `empty` values; a run DWSIM converges internally has no iterations to show, so its values are `n/a`.
 */
export function SolverConsole({ solve, events, onStop, streamName = (tag) => tag }: Readonly<{ solve: SolveStatus | null; events: SolveEvent[];
  onStop: () => void; streamName?: (tag: string) => string }>) {
  const latest = events.length ? events[events.length - 1] : undefined;
  const elapsed = latest && solve ? Math.max(0, (Date.parse(latest.emitted_at) - Date.parse(solve.started_at)) / 1000) : null;
  const running = solve?.state === "running" || solve?.state === "stopping";
  const internal = solve != null && !solve.iterations_observable;
  const none = <Empty word={internal ? "n/a" : "empty"} />;
  const state = solve ? stateText(solve, events) : { text: "No run yet" };
  return (
    <section className={`solver-console solver-console--${solve?.state ?? "idle"}`} aria-label="Solver console">
      <header>
        <strong>Solver console</strong>
        <span role="status" aria-live="polite" className="solver-console-state" title={state.title}>{state.text}</span>
        {running && (
          <button type="button" className="solver-stop" onClick={onStop} disabled={solve?.state === "stopping"}
            title="Stops at the next iteration boundary; a running DWSIM or Studio evaluation is never interrupted">
            {solve?.state === "stopping" ? "Stopping…" : "Stop"}
          </button>
        )}
      </header>
      {!internal && events.length > 0 && (
        <ResidualChart title={running && !solve?.events_truncated ? "Residual per iteration (live)" : "Residual per iteration (recorded)"}
          series={[{ key: "run", label: "Max normalized residual", points: events.map((event) => ({ iteration: event.iteration, value: event.max_normalized_residual })) }]} />
      )}
      <dl className="draft-solver-diagnostics" aria-label="Current iteration diagnostics">
        <div><dt>Iteration</dt><dd>{latest ? `${latest.iteration}${elapsed != null ? ` · ${elapsed.toFixed(1)} s` : ""}` : none}</dd></div>
        <div><dt>Max normalized residual</dt><dd>{!latest ? none : latest.max_normalized_residual == null
          ? `no finite value${latest.non_finite ? ` (${latest.non_finite})` : ""}` : fmt(latest.max_normalized_residual, 5)} <small>(converged ≤ 1)</small></dd></div>
        <div><dt>Worst tear</dt><dd>{latest?.worst_tear
          ? <span title={`${latest.worst_tear}${latest.worst_field ? ` · ${latest.worst_field}` : ""}`}>{streamName(latest.worst_tear)}{latest.worst_field ? ` · ${fieldLabel(latest.worst_field)}` : ""}</span>
          : none}</dd></div>
        {/* The method itself is chosen in the settings above; only a non-unit relaxation factor is run-specific. */}
        {latest?.omega != null && latest.omega !== 1 ? <div><dt>Relaxation ω</dt><dd>{fmt(latest.omega, 3)}</dd></div> : null}
        <div><dt>Contraction estimate</dt><dd>{latest?.q_hat_stable == null ? none : `${fmt(latest.q_hat_stable, 3)} · ${classificationText(latest.classification)}`}</dd></div>
        <div><dt>Error estimate</dt><dd>{latest?.estimate_valid ? fmt(latest.estimated_error_normalized) : none}</dd></div>
        {latest?.events?.length ? <div><dt>Events</dt><dd>{latest.events.join(", ").replace(/_/g, " ")}</dd></div> : null}
      </dl>
      {internal && <p className="results-footnote">n/a: DWSIM converges this run internally.</p>}
      {solve?.events_truncated && <p className="draft-hint">Iteration log full; the run record keeps the full history.</p>}
      {solve?.retry_of && <SettingsDiff rows={solve.retry_settings_diff ?? []} caption={`Retry of run ${solve.retry_of.slice(0, 8)}`} />}
    </section>
  );
}

type RunRow = Pick<DraftRun, "run_id" | "action" | "status" | "draft_revision" | "started_at" | "outcome">;

/** Spec 210: server-side comparison of two runs of the draft. */
export function RunComparisonPanel({ workspaceId, draftId, refreshKey, streamName = (tag) => tag }: Readonly<{ workspaceId: string; draftId: string;
  refreshKey: string; streamName?: (tag: string) => string }>) {
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [comparison, setComparison] = useState<RunComparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void listDraftRuns(workspaceId, draftId).then((rows) => {
      const solved = rows.filter((row) => row.action === "run");
      setRuns(solved);
      setB(solved[0]?.run_id || "");
      setA(solved[1]?.run_id || "");
    }).catch(() => undefined);
  }, [workspaceId, draftId, refreshKey]);
  useEffect(() => {
    setComparison(null);
    setError(null);
    if (!a || !b || a === b) return;
    let active = true;
    void compareRuns(workspaceId, draftId, a, b)
      .then((result) => { if (active) setComparison(result); })
      .catch((cause) => { if (active) setError(String(cause?.message ?? cause)); });
    return () => { active = false; };
  }, [workspaceId, draftId, a, b]);
  if (runs.length < 2) return null;
  const label = (row: RunRow) => `${row.run_id.slice(0, 8)} · rev ${row.draft_revision.split(":")[0]} · ${row.outcome?.label ?? row.status}`;
  return (
    <details className="solver-compare">
      <summary>Compare runs</summary>
      <div className="solver-compare-pick">
        <label className="draft-field draft-field--inline"><span>Run A</span>
          <select aria-label="Run A" value={a} onChange={(event) => setA(event.target.value)}>{runs.map((row) => <option key={row.run_id} value={row.run_id}>{label(row)}</option>)}</select></label>
        <label className="draft-field draft-field--inline"><span>Run B</span>
          <select aria-label="Run B" value={b} onChange={(event) => setB(event.target.value)}>{runs.map((row) => <option key={row.run_id} value={row.run_id}>{label(row)}</option>)}</select></label>
      </div>
      {a === b && <p className="draft-hint">Pick two different runs.</p>}
      {error && <p className="draft-hint" role="alert">{error}</p>}
      {comparison && (
        <>
          <p className="draft-hint" role="note">
            {comparison.same_revision ? "Same draft revision." : comparison.process_content_differs
              ? "The process content differs between the two revisions: result differences are not due to the solver alone."
              : "Same process content; only solver settings differ between the revisions."}
          </p>
          <table className="solver-diff">
            <thead><tr><th /><th>Run A</th><th>Run B</th></tr></thead>
            <tbody>
              <tr><td>Run</td><td>{comparison.a.run_id.slice(0, 8)}</td><td>{comparison.b.run_id.slice(0, 8)}{comparison.b.retry_of ? ` (retry of ${comparison.b.retry_of.slice(0, 8)})` : ""}</td></tr>
              <tr><td>Draft revision</td><td>{comparison.a.draft_revision}</td><td>{comparison.b.draft_revision}</td></tr>
              <tr><td>Document digest</td><td title={comparison.a.document_sha256}>{comparison.a.document_sha256.slice(0, 12)}…</td><td title={comparison.b.document_sha256}>{comparison.b.document_sha256.slice(0, 12)}…</td></tr>
              <tr><td>Process content digest</td><td title={comparison.process_content_digests[0]}>{comparison.process_content_digests[0].slice(0, 12)}…</td><td title={comparison.process_content_digests[1]}>{comparison.process_content_digests[1].slice(0, 12)}…</td></tr>
              <tr><td>Outcome</td><td>{comparison.a.outcome.label}</td><td>{comparison.b.outcome.label}</td></tr>
              <tr><td>Stop reason</td><td>{stopReasonText(comparison.a.reason)}</td><td>{stopReasonText(comparison.b.reason)}</td></tr>
              <tr><td>Method</td><td>{methodLabel(comparison.a.method)}</td><td>{methodLabel(comparison.b.method)}</td></tr>
              <tr><td>Iterations</td><td>{comparison.a.iterations}</td><td>{comparison.b.iterations}</td></tr>
              <tr><td>Final max residual</td><td>{fmt(comparison.a.final_max_normalized_residual)}</td><td>{fmt(comparison.b.final_max_normalized_residual)}</td></tr>
              <tr><td>Results</td><td>{comparison.a.results_label}</td><td>{comparison.b.results_label}</td></tr>
            </tbody>
          </table>
          <SettingsDiff rows={comparison.settings_diff} caption="Solver settings, A → B" />
          {(comparison.a.residuals.length > 0 || comparison.b.residuals.length > 0) && (
            <ResidualChart title="Residual per iteration, A and B" series={[
              { key: "a", label: "A", points: comparison.a.residuals.map((row) => ({ iteration: row.iteration, value: row.max_normalized_residual })) },
              { key: "b", label: "B", dashed: true, points: comparison.b.residuals.map((row) => ({ iteration: row.iteration, value: row.max_normalized_residual })) },
            ]} />
          )}
          {comparison.streams.length > 0 && (
            <table className="solver-diff">
              <caption>Stream results (B − A)</caption>
              <thead><tr><th>Stream</th><th>Quantity</th><th>A</th><th>B</th><th>Δ</th></tr></thead>
              <tbody>{comparison.streams.flatMap((stream) => stream.fields.map((field) => (
                <tr key={`${stream.stream}-${field.field}`}><td title={stream.stream}>{streamName(stream.stream)}</td><td>{fieldLabel(field.field)} ({field.unit})</td>
                  <td>{fmt(field.a, 6)}</td><td>{fmt(field.b, 6)}</td><td>{fmt(field.delta, 4)}</td></tr>
              )))}</tbody>
            </table>
          )}
          {(comparison.only_in_a.length > 0 || comparison.only_in_b.length > 0) && (
            <p className="draft-hint">Streams only in A: {comparison.only_in_a.join(", ") || "none"} · only in B: {comparison.only_in_b.join(", ") || "none"}</p>
          )}
        </>
      )}
    </details>
  );
}
