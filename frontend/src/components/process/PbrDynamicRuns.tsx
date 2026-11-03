import { useCallback, useEffect, useState } from "react";
import { request, StudioApiError } from "../../api/studio";
import { listEnvironmentProfiles, type EnvironmentProfileHead, type SeriesResponse } from "../../api/studioProcedure";
import type { DraftOp } from "../../api/processDraft";
import { formatSig } from "./pbrLogic";

import NumericInput from "../ui/NumericInput";
type Job = { job_id: string; scenario_id: string; status: string; progress?: number; error?: { code?: string; message?: string } | null;
  identity?: { draft_revision?: string }; created_at?: string; current?: boolean | null };
type Manifest = { fidelity?: string; channels?: Record<string, string> };
const path = (workspaceId: string, draftId: string) =>
  `/workspaces/${encodeURIComponent(workspaceId)}/process/drafts/${encodeURIComponent(draftId)}/dynamic/runs`;
const channel = (tag: string, kind: "intercepted" | "absorbed") => `${tag}_par_${kind}_umol_s_m`;

function ParPlot({ series, tag }: { series: SeriesResponse; tag: string }) {
  const times = series.channels.t_s ?? [];
  const intercepted = series.channels[channel(tag, "intercepted")] ?? [];
  const absorbed = series.channels[channel(tag, "absorbed")] ?? [];
  if (!times.length || intercepted.length !== times.length || absorbed.length !== times.length) return <p>No PAR points in this run.</p>;
  const width = 640; const height = 180; const inset = 16;
  const t0 = times[0]; const span = Math.max(1, times[times.length - 1] - t0);
  const ymax = Math.max(1, ...intercepted, ...absorbed);
  const points = (values: number[]) => values.map((value, index) =>
    `${index ? "L" : "M"}${(inset + (times[index] - t0) / span * (width - 2 * inset)).toFixed(2)},${(height - inset - value / ymax * (height - 2 * inset)).toFixed(2)}`).join(" ");
  return <figure className="pbr-par-plot"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="One-day intercepted and absorbed PAR curves" preserveAspectRatio="none">
    <path d={points(intercepted)} fill="none" stroke="var(--color-chart-series-2)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
    <path d={points(absorbed)} fill="none" stroke="var(--color-chart-series-3)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
  </svg><figcaption><span className="pbr-par-plot__intercepted">Intercepted PAR</span> · <span className="pbr-par-plot__absorbed">Absorbed PAR</span> · µmol s⁻¹ m⁻¹ · first 24 h · {series.returned_points} points</figcaption>
    <details><summary>PAR values</summary><table><thead><tr><th>Hour</th><th>Intercepted</th><th>Absorbed</th></tr></thead>
      <tbody>{times.map((time, index) => index % Math.max(1, Math.ceil(times.length / 48)) === 0 ? <tr key={index}><td>{formatSig(time / 3600)}</td><td>{formatSig(intercepted[index])}</td><td>{formatSig(absorbed[index])}</td></tr> : null)}</tbody></table></details>
  </figure>;
}

export default function PbrDynamicRuns({ workspaceId, draftId, revision, tag, apply }: {
  workspaceId: string; draftId: string; revision: string; tag: string; apply(ops: DraftOp[]): Promise<unknown>;
}) {
  const [profiles, setProfiles] = useState<EnvironmentProfileHead[]>([]);
  const [profileId, setProfileId] = useState("");
  const [initial, setInitial] = useState({ X: "", N: "", O2: "" });
  const [parFactor, setParFactor] = useState("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [runFidelity, setRunFidelity] = useState<Record<string, string>>({});
  const [current, setCurrent] = useState<boolean | null>(null);
  const [series, setSeries] = useState<SeriesResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const runsPath = path(workspaceId, draftId);
  const scenarioPrefix = `optics_${tag.toLowerCase().replace(/[^a-z0-9]/g, "_").slice(0, 16)}_`;
  const refresh = useCallback(() => request<Job[]>(runsPath).then((rows) => { setJobs(rows); setSelectedId((id) =>
    rows.some((row) => row.job_id === id && row.scenario_id.startsWith(scenarioPrefix)) ? id : rows.find((row) => row.scenario_id.startsWith(scenarioPrefix))?.job_id || ""); }), [runsPath, scenarioPrefix]);

  useEffect(() => {
    void listEnvironmentProfiles(workspaceId).then((rows) => { setProfiles(rows); setProfileId((id) => id || rows[0]?.profile_id || ""); }).catch((cause: unknown) => setError(String(cause)));
    void refresh().catch((cause: unknown) => setError(String(cause)));
  }, [workspaceId, refresh]);
  useEffect(() => {
    if (!jobs.some((job) => job.status === "queued" || job.status === "running")) return;
    const timer = window.setInterval(() => { void refresh().catch(() => undefined); }, 1500);
    return () => window.clearInterval(timer);
  }, [jobs, refresh]);
  const selected = jobs.find((job) => job.job_id === selectedId);
  useEffect(() => {
    let active = true;
    const relevant = jobs.filter((job) => job.status === "succeeded" && job.scenario_id.startsWith(scenarioPrefix)).slice(0, 10);
    void Promise.all(relevant.map(async (job) => {
      const value = await request<Manifest>(`${runsPath}/${encodeURIComponent(job.job_id)}/manifest`);
      return [job.job_id, value.fidelity ?? "T1"] as const;
    })).then((rows) => { if (active) setRunFidelity(Object.fromEntries(rows)); }).catch(() => undefined);
    return () => { active = false; };
  }, [jobs, runsPath, scenarioPrefix]);
  useEffect(() => {
    setManifest(null); setSeries(null); setCurrent(null);
    if (!selected) return;
    let active = true;
    const runPath = `${runsPath}/${encodeURIComponent(selected.job_id)}`;
    void request<Job>(runPath).then((job) => { if (active) setCurrent(job.current ?? null); }).catch(() => undefined);
    if (selected.status !== "succeeded") return () => { active = false; };
    void request<Manifest>(`${runPath}/manifest`).then(async (next) => {
      if (!active) return;
      setManifest(next);
      if (next.channels?.[channel(tag, "intercepted")] && next.channels?.[channel(tag, "absorbed")]) {
        const query = new URLSearchParams({ channels: [channel(tag, "intercepted"), channel(tag, "absorbed")].join(","), start_s: "0", end_s: "86400", max_points: "400" });
        const values = await request<SeriesResponse>(`${runPath}/series?${query}`);
        if (active) setSeries(values);
      }
    }).catch((cause: unknown) => { if (active) setError(String(cause)); });
    return () => { active = false; };
  }, [runsPath, selected?.job_id, selected?.status, tag]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = async () => {
    const values = Object.fromEntries(Object.entries(initial).map(([key, text]) => [key, Number(text)])) as { X: number; N: number; O2: number };
    const profile = profiles.find((item) => item.profile_id === profileId);
    const factor = Number(parFactor);
    if (!profile || Object.values(initial).some((value) => !value.trim()) || !parFactor.trim()
        || Object.values(values).some((value) => !Number.isFinite(value) || value < 0) || !Number.isFinite(factor) || factor <= 0)
      return setError("Choose a profile and enter declared nonnegative initial X, N and O₂ and a positive PAR conversion factor.");
    setBusy(true); setError("");
    try {
      const scenarioId = `${scenarioPrefix}${Date.now().toString(36)}`;
      const start = new Date(profile.start);
      const end = new Date(start.getTime() + 86400_000);
      if (end > new Date(profile.end)) throw new Error("This profile does not cover a full day from its first timestamp.");
      const saved = await apply([{ op: "set_scenario", id: scenarioId, value: {
        units: [tag], profiles: [{ profile_id: profileId, digest: profileId }],
        start_utc: start.toISOString(), end_utc: end.toISOString(), output_cadence_s: 900,
        temperature_source: "unit_mean", par_from_ghi_factor: factor, initial: { [tag]: values },
      } }]);
      if (saved !== true) return;
      const run = await request<Job>(runsPath, "POST", { scenario_id: scenarioId });
      await refresh(); setSelectedId(run.job_id);
    } catch (cause) {
      setError(cause instanceof StudioApiError && cause.code ? `${cause.code}: ${cause.message}`
        : cause instanceof Error ? cause.message : "Dynamic run could not start.");
    }
    finally { setBusy(false); }
  };
  const unitRuns = jobs.filter((job) => job.scenario_id.startsWith(scenarioPrefix));
  const t1Reference = unitRuns.find((job) => job.job_id !== selectedId && runFidelity[job.job_id] === "T1");
  return <section className="pbr-section pbr-dynamic" aria-label="Dynamic optics runs">
    <h5>Dynamic optics run</h5>
    <p className="draft-hint">One day from the selected profile. The run records this draft revision and its profile digest.</p>
    <label>Environment profile<select value={profileId} onChange={(event) => setProfileId(event.target.value)}>
      <option value="">Choose profile…</option>{profiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.name} · {profile.start.slice(0, 10)}</option>)}
    </select></label>
    <label>GHI to PAR factor (µmol/J; declared input)<NumericInput min={0} value={parFactor} onValueChange={(text) => setParFactor(text)} /></label>
    <div className="pbr-dynamic__initial" role="group" aria-label="Initial culture concentrations">
      {(["X", "N", "O2"] as const).map((name) => <label key={name}>{name === "O2" ? "O₂" : name} initial (kg/m³)<NumericInput min={0} value={initial[name]}
        onValueChange={(text) => setInitial((old) => ({ ...old, [name]: text }))} /></label>)}
    </div>
    <button type="button" disabled={busy || !profileId} onClick={() => void start()}>{busy ? "Starting…" : "Run one day"}</button>
    {error && <p className="pbr-failure" role="alert">{error}</p>}
    {unitRuns.length > 0 && <label>Stored optics runs<select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>
      {unitRuns.map((job) => <option key={job.job_id} value={job.job_id}>{job.job_id.slice(0, 8)} · {job.status} · {job.created_at?.slice(0, 16) ?? ""}</option>)}
    </select></label>}
    {selected && <div className="pbr-dynamic__run"><p>Run {selected.job_id.slice(0, 8)} · {selected.status}{selected.progress !== undefined ? ` · ${Math.round(selected.progress * 100)}%` : ""}</p>
      <p>Revision {selected.identity?.draft_revision ?? "unknown"}{current === null ? "" : current ? " · current content" : " · earlier content"}{selected.identity?.draft_revision !== revision ? " · earlier revision" : ""}</p>
      {selected.error && <p className="pbr-failure" role="alert">{selected.error.code}: {selected.error.message}</p>}
      {manifest && <p>Manifest fidelity <strong>{manifest.fidelity ?? "T1"}</strong></p>}
      {series && <div className="pbr-dynamic__comparison"><ParPlot series={series} tag={tag} />
        {t1Reference && <aside><strong>T1 reference run</strong><p>{t1Reference.job_id.slice(0, 8)} · {t1Reference.status}</p>
          <p>Revision {t1Reference.identity?.draft_revision ?? "unknown"}</p>
          <button type="button" onClick={() => setSelectedId(t1Reference.job_id)}>Inspect T1 run</button></aside>}
      </div>}
      {manifest?.fidelity === "T1" && <p className="draft-hint">T1 reference run: this model does not report array interception or absorbed PAR channels.</p>}
    </div>}
  </section>;
}
