import { useEffect, useMemo, useState } from "react";
import type { DraftObject, DraftProjection } from "../../api/processDraft";
import { exportRunRows, failureText, getDynamicManifest } from "./dynamicApi";
import {
  comparablePlantMetrics, DEFAULT_PLANT_OVERVIEW, EMPTY_PLANT_CHOICES, fmtKpi, fmtNum, isTerminal, movePlantMetric, plantChoiceKey, plantMetrics, restorePlantChoices, runTitle, staleness,
  type DynRun, type Manifest, type PlantChoices, type PlantMetric, type SeriesData,
} from "./dynamicLogic";
import { SimulationNotice } from "./SimulationNotice";

type Loaded = { manifest: Manifest | null; series: SeriesData | null; error: string | null };

function readChoices(key: string): PlantChoices {
  try {
    const raw = JSON.parse(window.localStorage.getItem(key) ?? "null") as Partial<PlantChoices> | null;
    return raw && typeof raw === "object" ? {
      runIds: Array.isArray(raw.runIds) ? raw.runIds.filter((id): id is string => typeof id === "string") : [],
      metricIds: Array.isArray(raw.metricIds) ? raw.metricIds.filter((id): id is string => typeof id === "string") : [],
      overviewIds: Array.isArray(raw.overviewIds) ? raw.overviewIds.filter((id): id is string => typeof id === "string") : DEFAULT_PLANT_OVERVIEW,
      equipmentId: typeof raw.equipmentId === "string" ? raw.equipmentId : null,
      view: raw.view === "charts" ? "charts" : "table",
    } : EMPTY_PLANT_CHOICES;
  } catch { return EMPTY_PLANT_CHOICES; }
}

function Pfd({ objects, selected, onSelect }: { objects: DraftObject[]; selected: string | null; onSelect(id: string): void }) {
  const units = objects.filter((item) => item.kind === "unit");
  const streams = objects.filter((item) => item.kind === "stream" && item.type !== "EnergyStream" && item.source?.unit && item.target?.unit);
  if (!units.length) return <p>No equipment is placed on this draft's PFD.</p>;
  const xs = units.map((item) => item.x), ys = units.map((item) => item.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const position = (item: DraftObject) => ({ x: 85 + (maxX === minX ? 0.5 : (item.x - minX) / (maxX - minX)) * 830,
    y: 65 + (maxY === minY ? 0.5 : (item.y - minY) / (maxY - minY)) * 290 });
  const byId = new Map(units.map((item) => [item.id, item]));
  return <div className="dyn-pfd-scroll"><svg className="dyn-pfd" viewBox="0 0 1000 420" role="group" aria-label="Process flow diagram (PFD): equipment and material streams">
    <defs><marker id="dyn-pfd-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8" fill="none" stroke="currentColor" /></marker></defs>
    {streams.map((stream) => {
      const source = byId.get(stream.source!.unit), target = byId.get(stream.target!.unit);
      if (!source || !target) return null;
      const a = position(source), b = position(target);
      return <g key={stream.id}><line x1={a.x} y1={a.y} x2={b.x} y2={b.y} markerEnd="url(#dyn-pfd-arrow)" className="dyn-pfd__stream" />
        <text x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 7} textAnchor="middle" className="dyn-pfd__stream-label">{stream.tag}</text></g>;
    })}
    {units.map((unit) => { const p = position(unit); return <g key={unit.id} role="button" tabIndex={0} aria-label={`Select equipment ${unit.tag}`}
      aria-pressed={selected === unit.id} className={`dyn-pfd__unit${selected === unit.id ? " is-selected" : ""}`} onClick={() => onSelect(unit.id)}
      onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(unit.id); } }}>
      <rect x={p.x - 57} y={p.y - 25} width="114" height="50" rx="8" /><text x={p.x} y={p.y + 4} textAnchor="middle">{unit.tag}</text></g>; })}
  </svg></div>;
}

const metricText = (metric: PlantMetric | undefined) => metric ? fmtKpi(metric) : "not available: this run does not report the metric";
const qualification = (run: DynRun, data: Loaded | undefined, metric: PlantMetric | undefined, headSeq: number) =>
  `${runTitle(run, staleness(run, headSeq))} · ${run.status === "succeeded" ? "complete" : `${run.status}, partial or unavailable`} · revision ${run.identity?.draft_revision ?? "unknown"} · ${data?.manifest?.fidelity ?? "fidelity unrecorded"} · ${metric?.qualification ?? "metric unavailable"}${data?.series && data.series.stride > 1 ? ` · sampled series, stride ${data.series.stride}` : ""}`;

export default function PlantView({ workspaceId, draft, runs }: { workspaceId: string; draft: DraftProjection; runs: DynRun[] }) {
  const key = plantChoiceKey(workspaceId, draft.draft_id);
  const [choices, setChoices] = useState<PlantChoices>(() => readChoices(key));
  const [loaded, setLoaded] = useState<Record<string, Loaded>>({});
  const [removed, setRemoved] = useState<string[]>([]);
  const [ready, setReady] = useState(false);
  const completed = useMemo(() => runs.filter((run) => isTerminal(run.status)), [runs]);
  const validIds = useMemo(() => new Set(completed.map((run) => run.job_id)), [completed]);
  const activeIds = useMemo(() => {
    const selected = choices.runIds.filter((id) => validIds.has(id)).slice(0, 4);
    return selected.length ? selected : completed.length ? [completed.find((run) => run.status === "succeeded")?.job_id ?? completed[0].job_id] : [];
  }, [choices.runIds, validIds, completed]);
  const activeKey = activeIds.join("|");

  useEffect(() => {
    let live = true;
    for (const id of activeIds) if (!loaded[id]) {
      void getDynamicManifest(workspaceId, draft.draft_id, id).then(async (manifest) => {
        const wanted = ["harvest_X_kg", ...(manifest.units ?? []).map((unit) => `${unit.tag}_X`)].filter((name) => name in (manifest.channels ?? {}));
        const complete = wanted.length ? await exportRunRows(workspaceId, draft.draft_id, id, wanted) : null;
        const series = complete ? { channels: complete.columns, returned_points: complete.rows, total_points: complete.rows, stride: 1 } : null;
        if (live) setLoaded((old) => ({ ...old, [id]: { manifest, series, error: null } }));
      }).catch((cause) => { if (live) setLoaded((old) => ({ ...old, [id]: { manifest: null, series: null, error: failureText(cause).message } })); });
    }
    return () => { live = false; };
  }, [activeKey, workspaceId, draft.draft_id, loaded]); // selected run data is read only once; new draft remounts this view

  const byRun = useMemo(() => Object.fromEntries(activeIds.map((id) => [id, plantMetrics(loaded[id]?.manifest ?? null, loaded[id]?.series ?? null)])), [activeKey, loaded]);
  const catalog = useMemo(() => {
    const out = new Map<string, PlantMetric>();
    for (const id of activeIds) for (const metric of byRun[id] ?? []) if (!out.has(metric.id)) out.set(metric.id, metric);
    return [...out.values()];
  }, [activeKey, byRun]);
  const metricsById = new Map(catalog.map((metric) => [metric.id, metric]));
  const equipment = useMemo(() => draft.objects.filter((item) => item.kind === "unit"), [draft.objects]);
  const primaryId = activeIds[0] ?? null;
  const primary = primaryId ? completed.find((run) => run.job_id === primaryId) : null;
  const primaryMetrics = primaryId ? byRun[primaryId] ?? [] : [];

  useEffect(() => {
    if (!activeIds.length || activeIds.some((id) => !loaded[id])) return;
    const normalized = restorePlantChoices(choices, completed.map((run) => run.job_id), catalog.map((metric) => metric.id), equipment.map((item) => item.id),
      catalog.filter((metric) => metric.scope === "plant").map((metric) => metric.id));
    const next = { ...normalized.choices, runIds: activeIds };
    if (JSON.stringify(next) !== JSON.stringify(choices)) setChoices(next);
    if (normalized.removed.length) setRemoved(normalized.removed);
    setReady(true);
  }, [activeKey, loaded, completed, catalog, equipment, choices, activeIds]);
  useEffect(() => { if (ready) window.localStorage.setItem(key, JSON.stringify(choices)); }, [key, choices, ready]);

  const metricFor = (id: string, runId: string) => byRun[runId]?.find((metric) => metric.id === id);
  const toggleMetric = (id: string) => setChoices((old) => ({ ...old, metricIds: old.metricIds.includes(id) ? old.metricIds.filter((item) => item !== id) : [...old.metricIds, id] }));
  const toggleOverview = (id: string) => setChoices((old) => ({ ...old, overviewIds: old.overviewIds.includes(id) ? old.overviewIds.filter((item) => item !== id) : old.overviewIds.length < 5 ? [...old.overviewIds, id] : old.overviewIds }));
  const toggleRun = (id: string) => setChoices((old) => ({ ...old, runIds: old.runIds.includes(id) ? old.runIds.filter((item) => item !== id) : old.runIds.length < 4 ? [...old.runIds, id] : old.runIds }));
  const choosePrimary = (id: string) => setChoices((old) => ({ ...old, runIds: [id, ...old.runIds.filter((item) => item !== id)] }));
  const plant = primaryMetrics.filter((metric) => metric.scope === "plant");
  const plantCatalog = catalog.filter((metric) => metric.scope === "plant");
  const local = primaryMetrics.filter((metric) => metric.scope === "equipment" && equipment.find((item) => item.id === choices.equipmentId)?.tag === metric.owner);
  const loop = primaryMetrics.filter((metric) => metric.scope === "loop");
  const available = ["plant", "equipment", "loop", "unclassified"].map((scope) => ({ scope, rows: catalog.filter((metric) => metric.scope === scope &&
    (scope !== "equipment" || metric.owner === equipment.find((item) => item.id === choices.equipmentId)?.tag)) })).filter((item) => item.rows.length);

  return <section className="dyn-plant" aria-label="Dynamic plant overview">
    <header><h4>Plant overview</h4><SimulationNotice /><p>Run-level quantities stay separate from equipment and culture-loop values. This is a simulation result, not live plant telemetry.</p></header>
    {!completed.length ? <p>No completed dynamic run yet. Start one in Runs and results.</p> : <>
      <fieldset className="dyn-fieldset"><legend>Runs in the shared comparison (up to four)</legend>{completed.map((run) => <label key={run.job_id}>
        <input type="checkbox" checked={activeIds.includes(run.job_id)} disabled={!activeIds.includes(run.job_id) && activeIds.length >= 4} onChange={() => toggleRun(run.job_id)} />
        {runTitle(run, staleness(run, draft.seq))}{run.status === "succeeded" ? "" : ` · ${run.status}, partial or unavailable`}
        {activeIds.includes(run.job_id) && primaryId !== run.job_id ? <button type="button" onClick={() => choosePrimary(run.job_id)}>Show in overview</button> : null}
      </label>)}</fieldset>
      {removed.length ? <p className="dyn-warning" role="status">Saved choices no longer available: {removed.join(", ")}.</p> : null}
      {primary && <details className="dyn-hint"><summary>Run details</summary>Overview: {qualification(primary, loaded[primary.job_id], undefined, draft.seq)}. Snapshot {loaded[primary.job_id]?.manifest?.snapshot_digest ?? primary.snapshot_digest ?? "unavailable"}.</details>}
      {activeIds.map((id) => loaded[id]?.error ? <p key={id} className="dyn-error" role="alert">Run {id.slice(0, 8)}: {loaded[id].error}</p> : null)}
      <details className="dyn-plant__customize"><summary>Customize plant overview · {choices.overviewIds.length} of 5 cards</summary>
        <div role="group" aria-label="Plant overview KPIs">{plantCatalog.map((metric) => <label key={metric.id}><input type="checkbox" checked={choices.overviewIds.includes(metric.id)}
          disabled={!choices.overviewIds.includes(metric.id) && choices.overviewIds.length >= 5} onChange={() => toggleOverview(metric.id)} />{metric.label} [{metric.unit}]</label>)}</div></details>
      <div className="dyn-plant__cards" aria-label="Important plant-level KPIs">{choices.overviewIds.map((id) => {
        const metric = plant.find((item) => item.id === id), known = plantCatalog.find((item) => item.id === id);
        if (!known) return null;
        // Spec 229: while the overview run's results are still loading, say so instead of "not available".
        return <article key={id}><span>{known.label}</span><strong>{primaryId && !loaded[primaryId] ? "loading the run's results…" : metricText(metric)}</strong><small>{metric?.qualification ?? "Not reported by the overview run."}</small>
          <button type="button" onClick={() => toggleMetric(id)}>{choices.metricIds.includes(id) ? "Remove from comparison" : "Add to comparison"}</button></article>;
      })}{!choices.overviewIds.length ? <p>Choose plant-level KPIs above for this overview.</p> : null}</div>
      <div className="dyn-plant__layout"><div><h5>Process flow diagram · PFD</h5><p className="dyn-hint">Current draft equipment and material streams; a stale run may describe an older revision. Scroll sideways on narrow screens. This is not a piping and instrumentation diagram (P&ID).</p>
        <Pfd objects={draft.objects} selected={choices.equipmentId} onSelect={(id) => setChoices((old) => ({ ...old, equipmentId: id }))} />
        <div className="dyn-plant__equipment" role="group" aria-label="Equipment on PFD">{equipment.map((item) => <button type="button" key={item.id} aria-pressed={choices.equipmentId === item.id}
          onClick={() => setChoices((old) => ({ ...old, equipmentId: item.id }))}>{item.tag}</button>)}</div></div>
        <aside className="dyn-plant__local" aria-label="Equipment KPIs"><h5>{choices.equipmentId ? `Equipment · ${equipment.find((item) => item.id === choices.equipmentId)?.tag ?? "removed"}` : "Select equipment"}</h5>
          {local.length ? <ul>{local.map((metric) => <li key={metric.id}><strong>{metric.label}</strong><span>{fmtKpi(metric)}</span><small>{metric.qualification}</small>
            <button type="button" onClick={() => toggleMetric(metric.id)}>{choices.metricIds.includes(metric.id) ? "Remove from comparison" : "Add to comparison"}</button></li>)}</ul>
            : <p>{choices.equipmentId ? "This run reports no local KPIs for this equipment." : "Choose a unit on the PFD to see its local KPIs."}</p>}</aside></div>
      {loop.length ? <details><summary>Culture-loop KPIs ({loop.length})</summary><ul>{loop.map((metric) => <li key={metric.id}>{metric.label} · {fmtKpi(metric)} <button type="button" onClick={() => toggleMetric(metric.id)}>{choices.metricIds.includes(metric.id) ? "Remove" : "Add"}</button></li>)}</ul></details> : null}
      <section aria-label="Shared metric comparison"><h5>Shared metric comparison</h5><p>Select any reported KPI. Values are compared only with the same definition and unit. Missing or partial data remain explicit.</p>
        <div className="dyn-plant__catalog">{available.map(({ scope, rows }) => <details key={scope}><summary>{scope === "equipment" ? "Selected equipment" : scope} metrics ({rows.length})</summary>
          {rows.map((metric) => <label key={metric.id}><input type="checkbox" checked={choices.metricIds.includes(metric.id)} onChange={() => toggleMetric(metric.id)} />{metric.label} [{metric.unit}]</label>)}</details>)}</div>
        {choices.metricIds.length ? <><ol className="dyn-plant__selected" aria-label="Ordered comparison metrics">{choices.metricIds.map((id, index) => <li key={id}>
          {metricsById.get(id)?.label ?? id} <button type="button" disabled={index === 0} aria-label={`Move ${id} earlier`} onClick={() => setChoices((old) => ({ ...old, metricIds: movePlantMetric(old.metricIds, id, -1) }))}>↑</button>
          <button type="button" disabled={index === choices.metricIds.length - 1} aria-label={`Move ${id} later`} onClick={() => setChoices((old) => ({ ...old, metricIds: movePlantMetric(old.metricIds, id, 1) }))}>↓</button>
          <button type="button" aria-label={`Remove ${id}`} onClick={() => toggleMetric(id)}>Remove</button></li>)}</ol>
          <div role="group" aria-label="Comparison view"><button type="button" aria-pressed={choices.view === "table"} onClick={() => setChoices((old) => ({ ...old, view: "table" }))}>Table</button>
            <button type="button" aria-pressed={choices.view === "charts"} onClick={() => setChoices((old) => ({ ...old, view: "charts" }))}>Charts</button></div>
          {choices.view === "table" ? <div className="dyn-table-scroll"><table aria-label="Shared KPI comparison"><thead><tr><th scope="col">KPI and scope</th>{activeIds.map((id) => <th scope="col" key={id}>{id.slice(0, 8)}</th>)}</tr></thead>
            <tbody>{choices.metricIds.map((id) => { const source = metricsById.get(id); return <tr key={id}><th scope="row">{source?.label ?? id}<small>{source?.scope}{source?.owner ? ` · ${source.owner}` : ""} · {source?.unit ?? "unit unavailable"}</small></th>
              {activeIds.map((runId) => { const run = completed.find((item) => item.job_id === runId)!; const metric = metricFor(id, runId); const mismatch = metric && source && !comparablePlantMetrics(source, metric);
                return <td key={runId}>{mismatch ? `not comparable: ${metric.label} [${metric.unit}] versus ${source.label} [${source.unit}]` : metricText(metric)}
                  <small>{qualification(run, loaded[runId], metric, draft.seq)}</small></td>; })}</tr>; })}</tbody></table></div>
            : <div className="dyn-plant__charts">{choices.metricIds.map((id) => { const source = metricsById.get(id); if (!source) return null;
              const values = activeIds.map((runId) => { const metric = metricFor(id, runId); return metric && comparablePlantMetrics(source, metric) && typeof metric.value === "number" && Number.isFinite(metric.value) ? metric.value : null; });
              const max = Math.max(0, ...values.map((value) => Math.abs(value ?? 0)));
              return <figure key={id} aria-label={`${source.label} comparison chart`}><figcaption>{source.label} · {source.scope}{source.owner ? ` ${source.owner}` : ""} [{source.unit}]</figcaption>
                {activeIds.map((runId, index) => { const run = completed.find((item) => item.job_id === runId)!; const metric = metricFor(id, runId); return <div className="dyn-plant__bar-row" key={runId}>
                  <span>{runId.slice(0, 8)}</span><div className="dyn-plant__bar-track">{values[index] !== null ? <div className="dyn-plant__bar" style={{ width: `${max ? Math.abs(values[index]!) / max * 100 : 0}%` }} /> : null}</div>
                  <strong>{metric && !comparablePlantMetrics(source, metric) ? `not comparable: ${metric.label} [${metric.unit}]` : metricText(metric)}</strong><small>{qualification(run, loaded[runId], metric, draft.seq)}</small></div>; })}</figure>; })}</div>}
        </> : <p>No KPI is selected for the shared comparison.</p>}
      </section>
    </>}
  </section>;
}
