import { useCallback, useEffect, useState } from "react";
import { designTarget } from "../app/designTargets";
import { getComponentReducedModels, pinReducedModelToProcess, type DesignComponent, type ReducedModelOutOfRange,
  type ReducedModelStatus } from "../api/designProject";
import { Empty } from "../components/design/DesignStatus";
import { canPinToProcess, reducedModelStatusLabel, segmentValidityText, showsReasons } from "./reducedModelStatus";

type Props = {
  projectId: string; versionId: string; component: DesignComponent;
  /** Revision of the component's linked Process draft, or null when it has no (loaded) Process source. */
  draftRevision: string | null;
  /** Called after a successful pin so the page can reload the component and the Process draft revision. */
  onChanged: () => Promise<void> | void;
};

export default function ReducedModelsPanel({ projectId, versionId, component, draftRevision, onChanged }: Props) {
  const [items, setItems] = useState<ReducedModelStatus[] | null>(null);
  const [policy, setPolicy] = useState<ReducedModelOutOfRange>("refuse");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setItems(await getComponentReducedModels(projectId, versionId, component.id)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "CFD reduced models are unavailable."); }
  }, [projectId, versionId, component.id]);
  useEffect(() => { setItems(null); setError(null); void load(); }, [load, component.revision]);

  const pin = async (item: ReducedModelStatus) => {
    if (!draftRevision) return;
    setBusyId(item.model_version_id); setError(null);
    try {
      await pinReducedModelToProcess(projectId, versionId, component, item.model_version_id, draftRevision, policy);
      await onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Pinning the reduced model failed.");
      void onChanged();
    } finally { setBusyId(null); await load(); }
  };

  return <section aria-label="CFD reduced models" className="design-project__reduced" {...designTarget("design.panel.reduced-model")}>
    <h3>CFD reduced models</h3>
    {error ? <p role="alert" className="design-project__error">{error}</p> : null}
    {items === null ? (error ? null : <p>Loading CFD reduced models…</p>)
      : <>
        <label htmlFor="design-reduced-policy">Out-of-range policy for the Process pin</label>
        <select id="design-reduced-policy" value={policy} disabled={!items.length} onChange={(event) => setPolicy(event.target.value as ReducedModelOutOfRange)}>
          <option value="refuse">Refuse out-of-range operating points</option>
          <option value="warn">Warn on out-of-range operating points</option>
        </select>
        {!items.length ? <dl className="design-project__fields design-project__reduced-item" aria-label="Reduced model">
          <div><dt>Fidelity</dt><dd><Empty /></dd></div>
          <div><dt>Status</dt><dd><Empty /></dd></div>
          <div><dt>Validity range</dt><dd><Empty /></dd></div>
          <div><dt>Process pin</dt><dd><Empty /></dd></div>
        </dl>
        : <ul className="design-project__reduced-list">{items.map((item) => <li key={item.model_version_id} className="design-project__reduced-item">
          <p><strong>{item.fidelity}</strong> · <span className={`design-project__reduced-status design-project__reduced-status--${item.status}`}>{reducedModelStatusLabel(item.status)}</span>
            {item.process_unit_pinned ? <> · <span className="design-project__reduced-pinned">Pinned on Process unit</span></> : null}</p>
          {showsReasons(item) ? <ul aria-label="Reasons">{item.obsolete_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : null}
          {item.segments.length ? <ul aria-label="Validity range">{item.segments.map((segment, index) =>
            <li key={index}>{segmentValidityText(segment)}</li>)}</ul> : null}
          {canPinToProcess(item) ? <button type="button" disabled={busyId !== null || !draftRevision} onClick={() => void pin(item)}
            title={draftRevision ? undefined : "Needs a component linked to a Process unit"}>Pin on Process unit</button> : null}
        </li>)}</ul>}
      </>}
  </section>;
}
