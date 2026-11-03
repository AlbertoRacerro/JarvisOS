import { useEffect, useState } from "react";
import { designTarget } from "../app/designTargets";
import { getDraft, getDraftRegistry, listDrafts, type DraftProjection, type DraftRegistry } from "../api/processDraft";
import { listDynamicRuns } from "../components/dynamic/dynamicApi";
import type { DynRun } from "../components/dynamic/dynamicLogic";
import DynamicPanel from "../components/dynamic/DynamicPanel";
import { DesignStatus, Empty } from "../components/design/DesignStatus";

// Spec 229: the Optics stage shows, inside Design, what the operator previously had to find in the full Process
// editor: each photobioreactor unit's optics inputs (read-only, unset values say so) and the stored Dynamic Studio
// prediction of a chosen draft. Edits and runs stay with their owners (the Process inspector, Dynamic Studio).
const DRAFT_LIMIT = 12;

type Loaded = Readonly<{ draft: DraftProjection; runs: readonly DynRun[] }>;

function hasOptics(registry: DraftRegistry, draft: DraftProjection) {
  const keys = new Set((registry.pbr_t2_params ?? []).map((param) => param.key));
  return draft.objects.filter((item) => item.kind === "unit" && (item.options?.optics_model !== undefined || Object.keys(item.params ?? {}).some((key) => keys.has(key))));
}

export default function DesignOpticsPanel({ workspaceId, plantDraftId }: Readonly<{ workspaceId: string; plantDraftId?: string }>) {
  const [registry, setRegistry] = useState<DraftRegistry | null>(null);
  const [loaded, setLoaded] = useState<Loaded[] | null>(null);
  const [omitted, setOmitted] = useState(0);
  const [predictionId, setPredictionId] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setLoaded(null); setRegistry(null); setError(null); setPredictionId("");
    void Promise.all([getDraftRegistry(workspaceId), listDrafts(workspaceId)]).then(async ([nextRegistry, summaries]) => {
      const shown = summaries.slice(0, DRAFT_LIMIT);
      const rows = await Promise.all(shown.map(async (summary) => ({
        draft: await getDraft(workspaceId, summary.draft_id),
        runs: await listDynamicRuns(workspaceId, summary.draft_id, 30).catch(() => [] as DynRun[])
      })));
      if (!live) return;
      setRegistry(nextRegistry); setLoaded(rows); setOmitted(summaries.length - shown.length);
      // Default to the draft with the latest succeeded prediction; the Plant draft only when nothing else has one.
      const withPrediction = rows.filter((row) => row.runs.some((run) => run.status === "succeeded"));
      setPredictionId((withPrediction.find((row) => row.draft.draft_id !== plantDraftId) ?? withPrediction[0])?.draft.draft_id ?? "");
    }).catch((cause: unknown) => { if (live) setError(cause instanceof Error ? cause.message : "Process drafts are unavailable."); });
    return () => { live = false; };
  }, [workspaceId, plantDraftId]);

  const params = registry?.pbr_t2_params ?? [];
  const prediction = loaded?.find((row) => row.draft.draft_id === predictionId);
  const accept = (next: DraftProjection) => setLoaded((rows) => rows?.map((row) => row.draft.draft_id === next.draft_id ? { ...row, draft: next } : row) ?? rows);

  return <section className="design-project__panel design-project__main design-project__optics" aria-label="Optics and prediction" {...designTarget("design.panel.optics")}>
    <h2>Optics</h2>
    <details className="design-project__about"><summary>Details</summary><p>Optics inputs belong to photobioreactor units in this version's Process drafts and are edited in the unit inspector (Light &amp; environment). The prediction is a stored Dynamic Studio run of one draft. Optics is not coupled to CFD.</p></details>
    {error ? <p role="alert" className="design-project__error">{error}</p> : null}
    {loaded === null && !error ? <p role="status">Loading Process drafts…</p> : null}
    {loaded ? <>
      <section aria-label="Optics inputs" {...designTarget("design.panel.optics-inputs")}>
        <div className="design-project__node-head"><h3>Optics inputs</h3><span className="design-empty">unqualified</span>
          {omitted ? <span className="design-empty" title={`${omitted} more in the full Process editor`}>first {DRAFT_LIMIT} drafts</span> : null}</div>
        {loaded.every((row) => !hasOptics(registry!, row.draft).length) ? <>
          <DesignStatus state="empty" title="No Process unit in this version has optics inputs" />
          <dl className="design-project__optics-values">{params.map((param) => <div key={param.key}><dt>{param.label}</dt><dd><Empty /></dd></div>)}</dl>
        </> : loaded.map(({ draft }) => {
            const units = hasOptics(registry!, draft);
            return units.length ? <div key={draft.draft_id} className="design-project__optics-draft">
              <h4>{draft.name}{draft.draft_id === plantDraftId ? " · Plant" : ""}</h4>
              {units.map((unit) => <details key={unit.id} open>
                <summary>{unit.tag} · <span title={unit.options?.optics_model === "t2" ? "Profile-driven tube/array" : "Screening"}>{unit.options?.optics_model === "t2" ? "Tier 2" : "Tier 1"}</span></summary>
                <dl className="design-project__optics-values">{params.map((param) => {
                  const value = unit.params?.[param.key];
                  return <div key={param.key}><dt>{param.label}</dt><dd>{value ? `${value.value} ${value.unit}`.trim() : <Empty />}</dd></div>;
                })}</dl>
              </details>)}
            </div> : null;
          })}
      </section>
      <section aria-label="Dynamic Studio prediction" {...designTarget("design.panel.prediction")}>
        <h3>Prediction · Dynamic Studio</h3>
        <label htmlFor="design-prediction-draft">Draft with the prediction</label>
        <select id="design-prediction-draft" value={predictionId} onChange={(event) => setPredictionId(event.target.value)}>
          <option value="">Choose a Process draft</option>
          {loaded.map(({ draft, runs }) => {
            const succeeded = runs.filter((run) => run.status === "succeeded").length;
            return <option key={draft.draft_id} value={draft.draft_id}>{draft.name} · {succeeded ? `${succeeded} run${succeeded === 1 ? "" : "s"}` : "empty"}</option>;
          })}
        </select>
        {prediction ? <DynamicPanel key={prediction.draft.draft_id} workspaceId={workspaceId} draft={prediction.draft} onUpdated={accept} />
          : <DesignStatus state="empty" title={loaded.some((row) => row.runs.some((run) => run.status === "succeeded"))
            ? "Choose a draft" : "No succeeded Dynamic Studio run in this version"} />}
      </section>
    </> : null}
  </section>;
}
