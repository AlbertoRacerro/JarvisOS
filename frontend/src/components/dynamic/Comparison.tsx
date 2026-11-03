import { useEffect, useMemo, useState } from "react";
import { getDynamicManifest, getDynamicSeries, failureText } from "./dynamicApi";
import { alignOnTime, compareChannels, compareSelection, fmtNum, kpiDeltas, kpis, runTitle, staleness, type DynRun, type Manifest, type SeriesData } from "./dynamicLogic";
import { ChartPanel, RangeControls, RangeKeys } from "./SyncCharts";
import { Refusal, SimulationNotice } from "./SimulationNotice";

type Loaded = { manifest: Manifest; series: SeriesData };
type Range = [number, number];
export default function Comparison({ workspaceId, draftId, headSeq, runs }: { workspaceId: string; draftId: string; headSeq: number; runs: DynRun[] }) {
  const completed = runs.filter((run) => ["succeeded", "failed"].includes(run.status));
  const [ids, setIds] = useState<string[]>([]); const [baseline, setBaseline] = useState("");
  const [data, setData] = useState<Record<string, Loaded>>({}); const [range, setRange] = useState<Range | null>(null);
  const [problem, setProblem] = useState<{ code: string | null; message: string } | null>(null);
  const draftIds = Object.fromEntries(completed.map((run) => [run.job_id, draftId]));
  useEffect(() => { setIds((old) => old.filter((id) => completed.some((run) => run.job_id === id))); }, [runs]);
  useEffect(() => { if (!ids.includes(baseline)) setBaseline(ids[0] ?? ""); }, [ids, baseline]);
  useEffect(() => {
    let live = true; setProblem(null);
    for (const id of ids) if (!data[id]) void getDynamicManifest(workspaceId, draftId, id).then(async (manifest) => {
      const names = Object.keys(manifest.channels ?? {}).filter((name) => name !== "t_s");
      const series = await getDynamicSeries(workspaceId, draftId, id, { channels: names, max_points: 5000 });
      if (live) setData((old) => ({ ...old, [id]: { manifest, series } }));
    }).catch((cause) => { if (live) setProblem(failureText(cause)); });
    return () => { live = false; };
  }, [ids, data, workspaceId, draftId]);
  const loaded = ids.filter((id) => data[id]);
  const channels = compareChannels(loaded.map((id) => ({ jobId: id, channels: data[id].manifest.channels ?? {} })));
  const [channel, setChannel] = useState("");
  useEffect(() => { if (!channels.some((item) => item.name === channel)) setChannel(channels[0]?.name ?? ""); }, [channels, channel]);
  const chosen = channels.find((item) => item.name === channel);
  const traces = chosen?.included.map((id) => ({ id, t: data[id].series.channels.t_s ?? [], y: data[id].series.channels[channel] ?? [] })) ?? [];
  const aligned = useMemo(() => alignOnTime(traces.map((trace) => ({ t: trace.t, y: trace.y }))), [channel, loaded.join("|"), data]); // eslint-disable-line react-hooks/exhaustive-deps
  const bounds: Range = [aligned.x[0] ?? 0, aligned.x[aligned.x.length - 1] ?? 1];
  const unit = chosen ? data[chosen.included[0]]?.manifest.channels?.[chosen.name] ?? "unit not recorded" : "";
  const cards = loaded.map((id) => ({ jobId: id, kpis: kpis(data[id].manifest, data[id].series) }));
  const differences = kpiDeltas(baseline, cards);
  const toggle = (id: string) => { const next = compareSelection(ids, id, draftIds); if (next.ok) { setIds(next.selection); setRange(null); setProblem(null); } else setProblem({ code: null, message: next.reason ?? "Cannot compare these runs." }); };
  return <section className="dyn-section" aria-label="Run comparison"><h4>Compare runs</h4><SimulationNotice />
    <p>Choose up to four runs of this draft. Time is elapsed seconds. A channel is overlaid only when its units match; values are never interpolated.</p>
    <fieldset className="dyn-fieldset"><legend>Runs</legend>{completed.map((run) => <label key={run.job_id}><input type="checkbox" aria-label={`Compare run ${run.job_id.slice(0, 8)}`} checked={ids.includes(run.job_id)} onChange={() => toggle(run.job_id)} />{runTitle(run, staleness(run, headSeq))}</label>)}</fieldset>
    {problem && <Refusal {...problem} />}
    {loaded.length > 0 && <><label>Baseline run<select aria-label="Comparison baseline" value={baseline} onChange={(event) => setBaseline(event.target.value)}>{loaded.map((id) => <option key={id} value={id}>{id.slice(0, 8)}</option>)}</select></label>
      <label>Channel<select aria-label="Comparison channel" value={channel} onChange={(event) => { setChannel(event.target.value); setRange(null); }}>{channels.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}</select></label>
      {chosen && <p role="status">Unit {unit}. {chosen.mismatched.map((item) => `${item.jobId.slice(0, 8)} has ${item.unit} and is excluded`).join("; ") || "All included traces use the same unit."} {chosen.absent.length ? `Absent: ${chosen.absent.map((id) => id.slice(0, 8)).join(", ")}.` : ""}</p>}
      {aligned.x.length > 0 && <RangeKeys label="Comparison chart" range={range} bounds={bounds} onRange={setRange}><RangeControls range={range} bounds={bounds} onRange={setRange} />
        <ChartPanel title={channel} unit={unit} x={aligned.x} bounds={bounds} range={range} onRange={setRange} lines={traces.map((trace, index) => ({ key: trace.id, label: runTitle(completed.find((run) => run.job_id === trace.id)!, staleness(completed.find((run) => run.job_id === trace.id)!, headSeq)), values: aligned.ys[index] }))} />
      </RangeKeys>}
      <h5>KPI differences from {baseline.slice(0, 8)}</h5><div className="dyn-table-scroll"><table aria-label="Comparison KPI differences"><thead><tr><th scope="col">KPI</th><th scope="col">Baseline</th>{loaded.filter((id) => id !== baseline).map((id) => <th scope="col" key={id}>{id.slice(0, 8)}</th>)}</tr></thead>
        <tbody>{differences.map((item) => <tr key={item.id}><th scope="row">{item.label}</th><td>{item.baseline !== null ? `${fmtNum(item.baseline)} ${item.unit}` : "not available"}</td>{item.values.map((value) => <td key={value.jobId}>{value.delta === null ? value.reason ?? "not available" : `${fmtNum(value.delta)} ${item.unit} (${value.relative === null ? "relative n/a" : `${fmtNum(value.relative * 100)} %`})`}</td>)}</tr>)}</tbody></table></div>
      <p className="dyn-hint">The plotted view may be strided to 5000 points per run. Narrow a run's range in Results to inspect each point.</p></>}
  </section>;
}
