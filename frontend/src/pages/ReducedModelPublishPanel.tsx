import { useCallback, useEffect, useState } from "react";
import { listComponentCfdRuns, publishComponentReducedModel, type CfdRun, type DesignComponent } from "../api/designProject";
import { Empty } from "../components/design/DesignStatus";
import { readableKey, regimeLabel, runLabel } from "../components/design/designText";
import { classifyRuns, emptySelection, MESH_STUDY_RUNS, planPublication, REGIMES, type PublishSelection } from "./reducedModelPublish";

type Props = {
  projectId: string; versionId: string; component: DesignComponent;
  /** Called after a successful publication so the page reloads the component and reduced-model status. */
  onChanged: () => Promise<void> | void;
};

const message = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback;

/** Spec 216 contract 7: name server-measured runs; the server rebuilds every number and may still refuse. */
export default function ReducedModelPublishPanel({ projectId, versionId, component, onChanged }: Props) {
  const [runs, setRuns] = useState<CfdRun[] | null>(null);
  const [selection, setSelection] = useState<PublishSelection>(emptySelection);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setRuns(await listComponentCfdRuns(projectId, versionId, component.id)); }
    catch (cause) { setRuns((current) => current ?? []); setError(message(cause, "CFD runs are unavailable.")); }
  }, [projectId, versionId, component.id]);
  useEffect(() => { setRuns(null); setSelection(emptySelection()); setError(null); setDone(null); void load(); }, [load]);

  const toggle = (kind: "points" | "meshStudy", regime: (typeof REGIMES)[number], runId: string) => setSelection((current) => {
    const ids = current[kind][regime];
    return { ...current, [kind]: { ...current[kind], [regime]: ids.includes(runId) ? ids.filter((id) => id !== runId) : [...ids, runId] } };
  });
  const plan = runs ? planPublication(runs, selection, component, versionId) : null;
  const groups = runs ? classifyRuns(runs) : null;

  const publish = async () => {
    if (!plan?.body) return;
    setBusy(true); setError(null); setDone(null);
    try {
      const status = await publishComponentReducedModel(projectId, versionId, component.id, plan.body);
      setDone(`The server published the reduced model and reports it as ${status.status}.`);
      setSelection(emptySelection());
      await onChanged();
    } catch (cause) { setError(message(cause, "Publishing the reduced model failed.")); }
    finally { setBusy(false); void load(); }
  };

  return <section aria-label="Publish reduced model" className="design-project__reduced">
    <h4>Publish reduced model</h4>
    <p className="draft-hint" title="Name finished, converged, server-measured runs. The server rebuilds the points from its own records and decides whether to publish.">The server decides; nothing is qualified here.</p>
    <button type="button" disabled={busy} onClick={() => void load()}>Refresh runs</button>
    {error ? <p role="alert" className="design-project__error">{error}</p> : null}
    {done ? <p role="status">{done}</p> : null}
    {groups === null ? <p>Loading CFD runs…</p> : REGIMES.map((regime) => <fieldset key={regime} aria-label={`${regimeLabel(regime)} runs`}>
      <legend>{regimeLabel(regime)}</legend>
      {!groups[regime].length ? <p><Empty /></p> : <ul className="design-project__reduced-list">{groups[regime].map(({ run, eligible, reasons }) => <li key={run.run_id} className="design-project__reduced-item" title={run.run_id}>
        {runLabel(run)} · {run.state}
        {run.measurement?.status === "measured" ? ` · mesh level ${run.measurement.mesh_level ?? "n/a"} · ${run.measurement.cells ?? "n/a"} cells` : ""}
        {eligible ? <>
          <label><input type="checkbox" checked={selection.points[regime].includes(run.run_id)} onChange={() => toggle("points", regime, run.run_id)} /> Study run</label>
          <label><input type="checkbox" checked={selection.meshStudy[regime].includes(run.run_id)} onChange={() => toggle("meshStudy", regime, run.run_id)} /> Mesh-study run</label>
        </> : <span className="draft-hint" title={reasons.join("; ")}> · Not usable</span>}
      </li>)}</ul>}
      <p className="draft-hint" title={`Exactly ${MESH_STUDY_RUNS} runs at distinct mesh levels, one Reynolds number`}>Mesh study: {selection.meshStudy[regime].length}/{MESH_STUDY_RUNS} selected</p>
    </fieldset>)}
    <form onSubmit={(event) => { event.preventDefault(); void publish(); }} aria-label="Publish a reduced model">
      <label htmlFor="publish-model-id">Reduced model id</label>
      <input id="publish-model-id" value={selection.modelId} onChange={(event) => setSelection({ ...selection, modelId: event.target.value })} />
      {REGIMES.map((regime) => <span key={regime}>
        <label htmlFor={`publish-tol-${regime}`} title="A fraction from 0 to 1">{readableKey(regimeLabel(regime))} tolerance</label>
        <input id={`publish-tol-${regime}`} inputMode="decimal" value={selection.tolerances[regime]}
          onChange={(event) => setSelection({ ...selection, tolerances: { ...selection.tolerances, [regime]: event.target.value } })} />
      </span>)}
      {plan && plan.reasons.length ? <ul aria-label="Why publishing is not possible">{plan.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : null}
      <button type="submit" disabled={busy || !plan?.body}>Publish reduced model</button>
    </form>
  </section>;
}
