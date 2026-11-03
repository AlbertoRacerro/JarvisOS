import { useState, type ReactNode } from "react";

import { unitLabel, type BalanceRow, type Kpi, type ResultsView, type RunOutcome, type ViewQuantity } from "../../api/processDraft";
import { outcomeHeadline, type ResultsLayout } from "./resultsLayout";
import { stopReasonText } from "./solverSettings";
import "./RunResults.css";

/** Spec 181 toolbar state. Only `converged` uses a success colour; `ready` is an editor state, not a result. */
export type RunState = "blocked" | "ready" | "running" | "validated" | "converged" | "non_converged" | "failed" | "cancelled";

const RUN_STATE_TEXT: Record<RunState, string> = {
  blocked: "Blocked", ready: "Ready to run", running: "Running…", validated: "Validated (not solved)",
  converged: "Converged", non_converged: "Not converged", failed: "Failed", cancelled: "Cancelled",
};

export function runState(busy: "validate" | "run" | null, blockerCount: number, outcome: RunOutcome | null | undefined,
  outcomeIsForThisRevision: boolean): RunState {
  if (busy) return "running";
  if (outcome && outcomeIsForThisRevision) return outcome.state;
  return blockerCount ? "blocked" : "ready";
}

/** Outcome reason codes in words; solver stop reasons share the solver settings wording. */
const REASONS: Record<string, string> = {
  NATIVE_RECYCLE_NOT_CONVERGED: "a Recycle missed its tolerance",
  DWSIM_SOLVE_NOT_CONVERGED: "DWSIM did not converge",
  cancelled: "cancelled",
};
export const reasonText = (reason: string | null | undefined) =>
  !reason ? "" : REASONS[reason] ?? (/^[A-Z0-9_]+$/.test(reason) ? reason.toLowerCase().replace(/_/g, " ") : stopReasonText(reason));

export function RunStateIndicator({ state, blockerCount, reason }: Readonly<{ state: RunState; blockerCount: number; reason?: string | null }>) {
  const text = state === "blocked" ? `${blockerCount} blocker${blockerCount === 1 ? "" : "s"}` : RUN_STATE_TEXT[state];
  // The reason stays in the tooltip: the run panel states it once, in words.
  return (
    <span className={`run-state run-state--${state}`} data-testid="run-state" data-state={state} role="status"
      title={reason ? `${RUN_STATE_TEXT[state]} · ${reasonText(reason)}` : RUN_STATE_TEXT[state]}>
      <span className="run-state__dot" aria-hidden="true" />{text}
    </span>
  );
}

const number = (value: number) => (Math.abs(value) >= 1e5 || (value !== 0 && Math.abs(value) < 1e-3) ? value.toExponential(3) : String(Number(value.toPrecision(5))));

/** The one word for a value that has no number yet; the reason, when known, is its tooltip. */
export function Empty({ reason, word = "empty" }: Readonly<{ reason?: string | null; word?: string }>) {
  return <span className="view-q view-q--empty" title={reason ?? undefined}>{word}</span>;
}

/** A value or `empty`; a failed value says so, a last iterate is marked, a real zero is a number. */
export function QuantityText({ q }: Readonly<{ q: ViewQuantity | undefined | null }>) {
  if (!q) return <span className="view-q view-q--absent">—</span>;
  if (q.value === null)
    return q.status === "failed" ? <span className="view-q view-q--failed" title={q.reason}>failed</span> : <Empty reason={q.reason} />;
  return <span className={`view-q${q.label ? " view-q--last-iterate" : ""}`} title={q.label}>{number(q.value)} {unitLabel(q.unit)}</span>;
}

export type StatusTone = "ok" | "warn" | "danger" | "idle";

/** One status line with a coloured dot: the single place that says why a table is empty. */
export function StatusLine({ tone, children, testId }: Readonly<{ tone: StatusTone; children: ReactNode; testId?: string }>) {
  return (
    <p className={`results-status results-status--${tone}`} role="status" data-testid={testId}>
      <span className="results-status__dot" aria-hidden="true" />{children}
    </p>
  );
}

const OUTCOME_TONE: Record<RunOutcome["state"], StatusTone> = {
  converged: "ok", validated: "idle", non_converged: "warn", failed: "danger", cancelled: "idle",
};

/**
 * The run outcome, said once: a headline, the few numbers that matter, Retry from the caller, and every
 * engine text, machine reason and stage code folded into Details. `details` adds run-specific engine output.
 */
export function OutcomeDetails({ outcome, streamName = (tag) => tag, details }: Readonly<{ outcome: RunOutcome;
  streamName?: (tag: string) => string; details?: ReactNode }>) {
  const message = outcome.message?.trim() || null;
  const failing = outcome.failing.filter((item) => item.tag || item.stage || item.error);
  const hasDetails = Boolean(outcome.reason || message || failing.length || outcome.artifact || details);
  return (
    <div className={`run-outcome run-outcome--${outcome.state}`} data-testid="run-outcome">
      <StatusLine tone={OUTCOME_TONE[outcome.state]}><span className="run-outcome__headline">{outcomeHeadline(outcome, streamName, reasonText)}</span></StatusLine>
      {(outcome.iterations !== null || outcome.state !== "validated") && (
        <dl className="run-outcome__facts">
          {outcome.iterations !== null && <div><dt>Iterations</dt><dd>{outcome.iterations}</dd></div>}
          {outcome.iterations !== null && <div><dt>Max normalized residual</dt><dd>{outcome.residual !== null ? number(outcome.residual) : <Empty />}</dd></div>}
          {outcome.worst_tear && <div><dt>Worst tear</dt><dd title={outcome.worst_tear}>{streamName(outcome.worst_tear)}</dd></div>}
          <div><dt>Results</dt><dd>{outcome.results_available === "converged" ? "converged"
            : outcome.results_available === "last_iterate" ? "last iterate, not a solution" : <Empty />}</dd></div>
        </dl>
      )}
      {hasDetails && (
        <details className="run-outcome__details">
          <summary>Details</summary>
          <dl>
            {outcome.reason && <div><dt>Reason</dt><dd><code>{outcome.reason}</code></dd></div>}
            {message && <div><dt>Message</dt><dd className="run-outcome__message">{message}</dd></div>}
            {failing.length > 0 && <div><dt>Failing</dt><dd><ul>{failing.map((item, index) => (
              <li key={index}><strong>{item.tag ?? "Flowsheet"}</strong>{item.stage ? ` (${item.stage})` : ""}
                {item.error && item.error.trim() !== message ? `: ${item.error}` : ""}</li>
            ))}</ul></dd></div>}
            {outcome.artifact && <div><dt>Solved case</dt><dd><code>{outcome.artifact}</code></dd></div>}
          </dl>
          {details}
        </details>
      )}
    </div>
  );
}

/** The status line above a results view: converged, stale, last iterate, not solved, or no run yet. */
function ViewStatus({ view, stale, staleNote }: Readonly<{ view: ResultsView | null; stale: boolean; staleNote?: string }>) {
  if (!view) return <StatusLine tone="idle" testId="results-banner">No run yet</StatusLine>;
  const where = `run ${view.run_id.slice(0, 8)} · revision ${view.draft_revision.split(":")[0]}`;
  if (view.label === "current")
    return <StatusLine tone={stale ? "warn" : "ok"} testId="results-banner">{stale ? `Stale · ${staleNote ?? where}` : `Converged · ${where}`}</StatusLine>;
  if (view.label === "last_iterate")
    return <StatusLine tone="warn" testId="results-banner">Not converged · last iterate, not a solution · {where}</StatusLine>;
  return <StatusLine tone={OUTCOME_TONE[view.outcome.state]} testId="results-banner">{view.outcome.label} · {where}</StatusLine>;
}

const pretty = (key: string) => key.replace(/_/g, " ");
const STREAM_FIELDS = ["temperature", "pressure", "mass_flow", "molar_flow", "volumetric_flow", "vapor_fraction"] as const;

/** The selected stream's or unit's results (spec 181 results navigator): the same fields before, during and after a run. */
export function ObjectResults({ view, layout, compounds, kind, tag, stale, onSelectTag }: Readonly<{ view: ResultsView | null;
  layout: ResultsLayout; compounds: string[]; kind: "stream" | "unit"; tag: string; stale: boolean; onSelectTag: (tag: string) => void }>) {
  if (kind === "stream") {
    const stream = view?.streams[tag];
    const ends = stream ?? layout.streams.find((item) => item.tag === tag);
    const reported = Object.keys(stream?.mass_fractions ?? {});
    const fractions = reported.length ? reported : compounds;
    return (
      <section className="object-results" aria-label={`Results for ${tag}`} data-testid="object-results">
        <ViewStatus view={view} stale={stale} />
        <p className="object-results__links">
          {ends?.from ? <button type="button" onClick={() => onSelectTag(ends.from as string)}>from {ends.from}</button> : <span>feed</span>}
          {" → "}
          {ends?.to ? <button type="button" onClick={() => onSelectTag(ends.to as string)}>to {ends.to}</button> : <span>product</span>}
          {stream?.owner && <span className="object-results__owner"> · {stream.owner === "jarvis_bio" ? "Jarvis" : "DWSIM"}{stream.state_source ? ` (${pretty(stream.state_source)})` : ""}</span>}
        </p>
        <dl className="object-results__grid">
          {STREAM_FIELDS.map((key) => <div key={key}><dt>{pretty(key)}</dt><dd>{stream ? <QuantityText q={stream[key]} /> : <Empty />}</dd></div>)}
        </dl>
        {fractions.length > 0 && <>
          <h4>Composition (mass fraction)</h4>
          <dl className="object-results__grid">
            {fractions.map((name) => <div key={name}><dt>{name}</dt><dd>{stream?.mass_fractions[name] ? <QuantityText q={stream.mass_fractions[name]} /> : <Empty />}</dd></div>)}
          </dl>
        </>}
        {stream?.culture && <>
          <h4>Culture</h4>
          {stream.culture_failure && <p className="draft-dwsim-error">{stream.culture_failure}</p>}
          <dl className="object-results__grid">
            {Object.entries(stream.culture).map(([name, q]) => <div key={name}><dt>{name}</dt><dd><QuantityText q={q} /></dd></div>)}
          </dl>
        </>}
      </section>
    );
  }
  const unit = view?.units[tag];
  const ports = unit ?? layout.units.find((item) => item.tag === tag);
  return (
    <section className="object-results" aria-label={`Results for ${tag}`} data-testid="object-results">
      <ViewStatus view={view} stale={stale} />
      <p className="object-results__links">
        {ports?.inlets.map((name) => <button key={`in-${name}`} type="button" onClick={() => onSelectTag(name)}>in {name}</button>)}
        {ports?.outlets.map((name) => <button key={`out-${name}`} type="button" onClick={() => onSelectTag(name)}>out {name}</button>)}
      </p>
      {unit && (unit.calculated !== null || unit.fidelity) && (
        <p className="object-results__status">
          {unit.calculated === false ? <strong className="view-q--failed" title={unit.error ?? undefined}>Not calculated</strong>
            : unit.calculated ? "Calculated" : null}
          {unit.fidelity ? `${unit.calculated !== null ? " · " : ""}${unit.fidelity}` : ""}
        </p>
      )}
      {unit && Object.keys(unit.quantities).length > 0 ? (
        <dl className="object-results__grid">
          {Object.entries(unit.quantities).map(([key, q]) => (
            <div key={key}><dt>{q.label ?? pretty(key)}</dt><dd><QuantityText q={q} /></dd></div>
          ))}
        </dl>
      ) : <dl className="object-results__grid"><div><dt>Quantities</dt><dd><Empty /></dd></div></dl>}
    </section>
  );
}

/** KPIs the model never computes (spec 181: CO₂ rows are always unavailable in T1). */
const NOT_MODELLED_KPIS = new Set(["co2_feed", "co2_uptake", "co2_per_biomass"]);
const notModelled = (kpi: Kpi) => kpi.status === "not_applicable" || NOT_MODELLED_KPIS.has(kpi.id);

function KpiTable({ kpis }: Readonly<{ kpis: Kpi[] }>) {
  const carbon = kpis.some((kpi) => NOT_MODELLED_KPIS.has(kpi.id));
  return (
    <>
      <table className="results-table" data-testid="kpi-table">
        <thead><tr><th>KPI</th><th>Value</th><th>Definition</th></tr></thead>
        <tbody>{kpis.map((kpi) => (
          <tr key={kpi.id} data-status={kpi.status} data-kpi={kpi.id}>
            <td>{kpi.label}</td>
            <td>{kpi.value !== null ? `${number(kpi.value)} ${kpi.unit === "1" ? "" : unitLabel(kpi.unit)}`
              : <Empty reason={kpi.reason} word={notModelled(kpi) ? "n/a" : "empty"} />}</td>
            <td className="results-table__definition">{notModelled(kpi) ? "—" : kpi.definition}</td>
          </tr>
        ))}</tbody>
      </table>
      {kpis.some(notModelled) && <p className="results-footnote">n/a: not modelled{carbon ? "; carbon is assumed supplied and non-limiting" : ""}.</p>}
    </>
  );
}

function BalanceTable({ rows }: Readonly<{ rows: Record<string, BalanceRow> }>) {
  return (
    <table className="results-table">
      <thead><tr><th>Quantity</th><th>In</th><th>Out</th><th>Residual</th><th>Tolerance</th><th>Passed</th></tr></thead>
      <tbody>{Object.entries(rows).map(([name, row]) => {
        const why = row.reason ?? row.error;
        const cell = (value: number | undefined | null, suffix = "") => (value == null ? <Empty reason={why} /> : `${number(value)}${suffix}`);
        return (
          <tr key={name}><td>{pretty(name)}</td><td>{cell(row.in)}</td><td>{cell(row.out)}</td>
            <td>{cell(row.residual, row.unit ? ` ${row.unit}` : "")}</td><td>{cell(row.tolerance)}</td>
            <td>{row.passed == null ? <Empty reason={why} /> : row.passed ? "yes" : "no"}</td></tr>
        );
      })}</tbody>
    </table>
  );
}

const TABS = ["Summary", "Boundary", "Streams", "Balances & findings"] as const;
const BOUNDARY_EMPTY: ViewQuantity = { value: null, status: "unavailable", reason: "no run yet" };

/**
 * The Results tab: KPIs, boundary in/out, full stream table with composition, balances and findings. The tables
 * keep their rows without a solved run (`view` null before the first run); one status line says why cells are empty.
 */
export function ResultsWorkspace({ view, layout, stale, staleNote, compounds, onSelectTag }: Readonly<{ view: ResultsView | null;
  layout: ResultsLayout; stale: boolean; staleNote?: string; compounds: string[]; onSelectTag: (tag: string) => void }>) {
  const [tab, setTab] = useState<(typeof TABS)[number]>("Summary");
  const streamTags = [...new Set([...layout.streams.map((item) => item.tag), ...Object.keys(view?.streams ?? {})])].sort();
  const streamRow = (tag: string) => {
    const stream = view?.streams[tag];
    const role = stream?.role ?? layout.streams.find((item) => item.tag === tag)?.role;
    return (
      <tr key={tag}>
        <td><button type="button" className="results-link" onClick={() => onSelectTag(tag)}>{tag}</button></td>
        <td>{role ?? "—"}</td>
        {(["temperature", "pressure", "mass_flow", "volumetric_flow"] as const).map((key) => <td key={key}>{stream ? <QuantityText q={stream[key]} /> : <Empty />}</td>)}
        {compounds.map((name) => <td key={name}>{stream ? <QuantityText q={stream.mass_fractions[name]} /> : <Empty />}</td>)}
        <td>{stream ? <QuantityText q={stream.culture?.biomass} /> : <Empty />}</td>
      </tr>
    );
  };
  const boundary = view?.boundary ?? {
    inputs: layout.streams.filter((item) => item.role === "feed").map((item) => item.tag),
    outputs: layout.streams.filter((item) => item.role === "product").map((item) => item.tag),
    totals: { mass_flow_in: BOUNDARY_EMPTY, mass_flow_out: BOUNDARY_EMPTY, by_compound_in: {}, by_compound_out: {} },
  };
  const total = (q: ViewQuantity | undefined) => <QuantityText q={q ?? BOUNDARY_EMPTY} />;
  const mass = view?.label === "not_solved" ? undefined : view?.balances.mass;
  const kpis = view?.kpis ?? layout.kpis;
  return (
    <section className={`results-workspace results-workspace--${view?.label ?? "none"}`} aria-label="Run results" data-testid="results-workspace">
      <ViewStatus view={view} stale={stale} staleNote={staleNote} />
      <div className="results-tabs" role="tablist" aria-label="Results sections">
        {TABS.map((name) => (
          <button key={name} type="button" role="tab" aria-selected={tab === name} className={tab === name ? "is-active" : ""} onClick={() => setTab(name)}>{name}</button>
        ))}
      </div>
      {tab === "Summary" && <KpiTable kpis={kpis} />}
      {tab === "Boundary" && (
        <table className="results-table" data-testid="boundary-table">
          <thead><tr><th>Direction</th><th>Streams</th><th>Total mass flow</th>{compounds.map((name) => <th key={name}>{name}</th>)}</tr></thead>
          <tbody>
            <tr><td>Inputs</td><td>{boundary.inputs.join(", ") || "—"}</td><td>{total(boundary.totals.mass_flow_in)}</td>
              {compounds.map((name) => <td key={name}>{total(boundary.totals.by_compound_in[name])}</td>)}</tr>
            <tr><td>Outputs</td><td>{boundary.outputs.join(", ") || "—"}</td><td>{total(boundary.totals.mass_flow_out)}</td>
              {compounds.map((name) => <td key={name}>{total(boundary.totals.by_compound_out[name])}</td>)}</tr>
          </tbody>
        </table>
      )}
      {tab === "Streams" && (
        <div className="results-scroll">
          <table className="results-table" data-testid="stream-table">
            <thead><tr><th>Stream</th><th>Role</th><th>T</th><th>P</th><th>Mass flow</th><th>Volumetric flow</th>
              {compounds.map((name) => <th key={name}>w {name}</th>)}<th>Biomass</th></tr></thead>
            <tbody>{streamTags.map(streamRow)}</tbody>
          </table>
        </div>
      )}
      {tab === "Balances & findings" && (
        <div className="results-balances">
          <h4>Mass balance</h4>
          {!mass || (mass.status && mass.status !== "calculated" && mass.in === undefined)
            ? <BalanceTable rows={{ carrier_mass: { reason: mass?.reason, error: mass?.error } }} />
            : mass.residual_kg_s !== undefined ? <p>Residual {number(mass.residual_kg_s)} kg/s</p> : <BalanceTable rows={{ carrier_mass: mass }} />}
          {view?.balances.culture && <><h4>Culture balances (whole flowsheet)</h4><BalanceTable rows={view.balances.culture} /></>}
          {view?.balances.unit_balances && Object.entries(view.balances.unit_balances).map(([tag, rows]) => (
            <details key={tag}><summary>Unit balance · {tag}</summary><BalanceTable rows={rows} /></details>
          ))}
          <h4>Findings</h4>
          {view?.findings.length ? (
            <ul className="draft-findings">{view.findings.map((finding, index) => (
              <li key={index} data-severity={finding.severity}><strong>{finding.object || "Flowsheet"}</strong> {finding.message}</li>
            ))}</ul>
          ) : <p className="draft-hint">{view ? "none" : <Empty />}</p>}
        </div>
      )}
    </section>
  );
}
