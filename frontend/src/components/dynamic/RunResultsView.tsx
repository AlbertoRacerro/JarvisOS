import { useCallback, useEffect, useMemo, useState } from "react";
import { KPI_COLUMNS } from "../studio/procedureLogic";
import { exportRunRows, failureText, getDynamicManifest, getDynamicSeries } from "./dynamicApi";
import {
  buildCsv, channelLabel, chips, defaultChannels, ExportRefused, fmtKpi, fmtNum, groupChannelsByUnit, isTerminal, kpis, provenanceRows, SNAPSHOT_CAVEAT, staleness,
  type DynRun, type LinkInfo, type Manifest, type SeriesData,
} from "./dynamicLogic";
import { Refusal, SimulationNotice } from "./SimulationNotice";
import { SyncCharts } from "./SyncCharts";

// Spec 173 decisions 9-13 and 15: the single results view. The Dynamic panel and the 191 test bench both render a run through it.
type Problem = { code: string | null; message: string } | null;
type Range = [number, number];

export type RunResultsViewProps = {
  workspaceId: string; draftId: string; run: DynRun; headSeq: number; link?: LinkInfo | null; onRerun?: () => void;
  onLoaded?: (jobId: string, manifest: Manifest | null, overview: SeriesData | null) => void;
};

export default function RunResultsView({ workspaceId, draftId, run, headSeq, link, onRerun, onLoaded }: RunResultsViewProps) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [picked, setPicked] = useState<string[] | null>(null);
  const [overview, setOverview] = useState<SeriesData | null>(null);
  const [view, setView] = useState<SeriesData | null>(null);
  const [range, setRange] = useState<Range | null>(null);
  const [problem, setProblem] = useState<Problem>(null);
  const [exporting, setExporting] = useState<string | null>(null);
  const terminal = isTerminal(run.status);
  const state = staleness(run, headSeq);
  const units = useMemo(() => manifest?.channels ?? {}, [manifest]);
  const names = useMemo(() => picked ?? defaultChannels(units), [picked, units]);
  const unitTags = useMemo(() => (manifest?.units ?? []).map((unit) => unit.tag), [manifest]);
  const wanted = useMemo(() => {
    const extra = [...unitTags.map((tag) => `${tag}_X`), "harvest_X_kg"].filter((name) => name in units);
    return [...new Set([...names, ...extra])];
  }, [names, unitTags, units]);

  useEffect(() => {
    let alive = true; setManifest(null); setPicked(null); setOverview(null); setView(null); setRange(null); setProblem(null);
    if (!terminal) return;
    void getDynamicManifest(workspaceId, draftId, run.job_id).then((next) => { if (alive) setManifest(next); }).catch((cause) => {
      // A failed or cancelled run may keep only partial series; the manifest can be absent. Say so instead of hiding it.
      if (alive) setProblem(run.status === "succeeded" ? failureText(cause) : null);
    });
    return () => { alive = false; };
  }, [workspaceId, draftId, run.job_id, run.status, terminal]);

  useEffect(() => {
    if (!terminal || !wanted.length) return;
    let alive = true;
    void getDynamicSeries(workspaceId, draftId, run.job_id, { channels: wanted, max_points: 5000 }).then((next) => { if (alive) setOverview(next); }).catch((cause) => { if (alive) setProblem(failureText(cause)); });
    return () => { alive = false; };
  }, [workspaceId, draftId, run.job_id, terminal, wanted]);

  useEffect(() => { if (overview) onLoaded?.(run.job_id, manifest, overview); }, [overview, manifest, run.job_id, onLoaded]);

  useEffect(() => {
    if (!overview) { setView(null); return; }
    if (!range) { setView(overview); return; }
    let alive = true;
    const timer = window.setTimeout(() => {
      void getDynamicSeries(workspaceId, draftId, run.job_id, { channels: names, start_s: range[0], end_s: range[1], max_points: 5000 })
        .then((next) => { if (alive) setView(next); }).catch((cause) => { if (alive) setProblem(failureText(cause)); });
    }, 250);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [overview, range, workspaceId, draftId, run.job_id, names]);

  const bounds = useMemo<Range>(() => { const t = overview?.channels.t_s ?? [0, 1]; return [t[0] ?? 0, t[t.length - 1] ?? 1]; }, [overview]);
  const exportCsv = useCallback(async () => {
    setExporting("Reading every row of the run…"); setProblem(null);
    try {
      const channels = Object.keys(units).filter((name) => name !== "t_s");
      const { columns, rows } = await exportRunRows(workspaceId, draftId, run.job_id, channels);
      const text = buildCsv({ jobId: run.job_id, snapshotDigest: manifest?.snapshot_digest ?? run.snapshot_digest ?? "n/a", fidelity: manifest?.fidelity ?? "T1", scenarioId: run.scenario_id, channels, units, columns });
      const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = `dynamic-${run.scenario_id}-${run.job_id.slice(0, 8)}.csv`; anchor.click();
      URL.revokeObjectURL(url);
      setExporting(`Exported ${rows} rows by ${channels.length + 1} columns to ${anchor.download}.`);
    } catch (cause) {
      setExporting(null);
      setProblem(cause instanceof ExportRefused ? { code: "export_refused", message: cause.message } : failureText(cause));
    }
  }, [workspaceId, draftId, run, manifest, units]);

  const chipRows = chips(manifest, run);
  const cards = kpis(manifest, overview);
  const actionEvents = (manifest?.event_log ?? []).filter((entry) => entry.type === "actions");
  const partial = run.status !== "succeeded" && terminal;
  const groups = groupChannelsByUnit(units);
  const campaign = manifest?.campaign?.loops ?? {};

  return <section className="dyn-results" data-job={run.job_id} data-run-status={run.status} data-staleness={state.state} aria-label={`Results of run ${run.job_id.slice(0, 8)}`}>
    <h4>Run {run.job_id.slice(0, 8)} · scenario {run.scenario_id} · {run.status}</h4>
    <SimulationNotice />
    <p className={`dyn-stale dyn-stale--${state.state}`} role="status" data-staleness-label={state.state}><strong>{state.label}.</strong> {state.detail}{" "}
      {state.state === "stale" && onRerun ? <button type="button" onClick={onRerun}>Re-run this scenario</button> : null}</p>
    <p className="dyn-hint">{SNAPSHOT_CAVEAT}</p>
    {link ? <p className="studio-link-provenance dyn-link">Procedure {link.version_label} · schedule {link.schedule_id} · {link.state === "modified" ? "modified, no longer the pinned procedure" : link.state}</p> : null}
    {run.error ? <Refusal code={run.error.code} message={run.error.message ?? "The run failed."} /> : null}
    {partial ? <p className="dyn-warning" role="status">Partial data: this run {run.status === "failed" ? "failed" : run.status}; any series below is what was computed before it stopped and is not a complete result.</p> : null}
    {problem ? <Refusal code={problem.code} message={problem.message} /> : null}
    {!terminal ? <p role="status">The run is {run.status}{typeof run.progress === "number" ? ` (${Math.round(run.progress * 100)} %)` : ""}. Results appear when it settles.</p> : null}

    <ul className="dyn-chips" aria-label="Run checks">{chipRows.map((chip) => <li key={chip.id} className={`dyn-chip dyn-chip--${chip.tone}`} data-chip={chip.id}><strong>{chip.label}:</strong> {chip.text}</li>)}</ul>
    <h5>Key figures</h5>
    <ul className="dyn-kpis" aria-label="Key figures">{cards.map((card) => <li key={card.id} data-kpi={card.id} className={card.value === null ? "is-missing" : ""}><span>{card.label}</span><strong>{fmtKpi(card)}</strong></li>)}</ul>
    {overview && overview.stride > 1 ? <p className="dyn-hint">Mean biomass is computed from the {overview.returned_points} sampled points of {overview.total_points}.</p> : null}
    <details className="dyn-provenance"><summary>Provenance and model-card qualification</summary>
      <dl>{provenanceRows(manifest, run).map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}</dl>
      <p className="dyn-hint">Profile and model-card digests are shown as the run used them. Whether they changed since is not claimed here.</p></details>

    {terminal && Object.keys(units).length ? <>
      <h5>Time series</h5>
      <fieldset className="dyn-channels"><legend>Series channels, grouped by unit</legend>
        {groups.map((group) => <div key={group.unit} role="group" aria-label={`Channels in ${group.unit}`}><strong>{group.unit}</strong>
          {group.names.map((name) => <label key={name}><input type="checkbox" aria-label={`Channel ${name}`} checked={names.includes(name)}
            onChange={(event) => setPicked(event.target.checked ? [...names, name] : names.filter((item) => item !== name))} />{channelLabel(name, group.unit)}</label>)}</div>)}</fieldset>
      {view ? <>
        <p className="dyn-hint" data-series-note={view.stride > 1 ? "strided" : "full"}>{view.stride > 1
          ? `Strided view: every ${view.stride}th of ${view.total_points} points in this range is drawn. Narrow the range to read every point.`
          : `All ${view.total_points} points in this range are drawn${range ? "" : ` (${view.returned_points} returned)`}.`}</p>
        <SyncCharts series={view} units={units} names={names} range={range} onRange={setRange} bounds={bounds} />
      </> : <p role="status">Loading series…</p>}
      <div className="dyn-actions"><button type="button" onClick={() => void exportCsv()} disabled={exporting !== null && exporting.startsWith("Reading")}>Export CSV</button>
        {exporting ? <span role="status" data-export-status>{exporting}</span> : null}</div>
      <p className="dyn-hint">The export reads every row in windows of at most 5000 points and carries job id, snapshot digest, fidelity and the Unqualified notice. A strided or truncated export is refused.</p>
    </> : null}

    {manifest ? <>
      <h5>Event log ({manifest.event_log?.length ?? 0} events, {actionEvents.length} action events)</h5>
      <div className="dyn-table-scroll"><table aria-label="Event log"><thead><tr><th scope="col">#</th><th scope="col">Time [s]</th><th scope="col">Type</th><th scope="col">What happened</th><th scope="col">Balance</th></tr></thead>
        <tbody>{(manifest.event_log ?? []).map((entry, index) => <tr key={index}><td>{index + 1}</td><td>{fmtNum(entry.time_s)}</td>
          <td>{String(entry.type ?? "")}{entry.conditional ? " (state-triggered)" : ""}</td>
          <td>{Array.isArray(entry.actions) ? (entry.actions as Record<string, unknown>[]).map((record) => String(record.type)).join(", ") : String(entry.target ?? entry.observed ?? "")}</td>
          <td>{(entry.balance as { closed?: boolean } | undefined)?.closed === false ? "OPEN" : entry.type === "actions" ? "closed" : "n/a"}</td></tr>)}</tbody></table></div>
      <p>Balances: {chipRows.find((chip) => chip.id === "balance")?.text}</p>
      {Object.entries(campaign).map(([loop, data]) => <div key={loop}><h5>Per-cycle KPIs · loop {loop} · {String(data.summary.periodicity ?? "")}</h5>
        <div className="dyn-table-scroll"><table aria-label={`Per-cycle KPIs of loop ${loop}`}><thead><tr>{KPI_COLUMNS.map(([, title]) => <th scope="col" key={title}>{title}</th>)}</tr></thead>
          <tbody>{data.cycles.map((cycle, i) => <tr key={i}>{KPI_COLUMNS.map(([key]) => <td key={key}>{fmtNum(cycle[key])}</td>)}</tr>)}</tbody></table></div>
        <p>Draws {fmtNum(data.summary.draws)} · harvested {fmtNum(data.summary.harvested_biomass_kg)} kg · mean last-{fmtNum(data.summary.last_k)} productivity {fmtNum(data.summary.mean_last_k_net_volumetric_productivity_kg_m3_day)} kg/m3/d</p></div>)}
    </> : null}
  </section>;
}
