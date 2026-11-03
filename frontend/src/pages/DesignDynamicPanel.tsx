import { useEffect, useState } from "react";
import { designTarget } from "../app/designTargets";
import { getDraft, listDrafts, type DraftProjection, type DraftSummary } from "../api/processDraft";
import DynamicPanel from "../components/dynamic/DynamicPanel";

// Design › Dynamic: the time simulation of a Process draft, as its own stage instead of a fold at the bottom of the
// Process editor. It opens the version's Plant draft; another draft of the version is one choice away and the choice
// lives in the URL (&draft=<id>), so Back, Forward and the Process editor's link reach the same draft.
export default function DesignDynamicPanel({ workspaceId, plantDraftIds, requestedDraftId, onChooseDraft, onOpenPlant }: Readonly<{
  workspaceId: string; plantDraftIds: readonly string[]; requestedDraftId: string | null;
  onChooseDraft(draftId: string | null, replace?: boolean): void; onOpenPlant(): void;
}>) {
  const [drafts, setDrafts] = useState<DraftSummary[] | null>(null);
  const [draft, setDraft] = useState<DraftProjection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [linkMissed, setLinkMissed] = useState(false);

  useEffect(() => {
    let live = true;
    setDrafts(null); setError(null);
    void listDrafts(workspaceId).then((rows) => { if (live) setDrafts(rows); })
      .catch((cause: unknown) => { if (live) setError(cause instanceof Error ? cause.message : "Process drafts are unavailable."); });
    return () => { live = false; };
  }, [workspaceId]);
  const known = (id: string | null | undefined) => (id && drafts?.some((row) => row.draft_id === id) ? id : undefined);
  // The Plant flowsheet shows the bound draft, or the first one while no component binds a draft.
  const plantIds = plantDraftIds.length ? plantDraftIds : drafts?.length ? [drafts[0].draft_id] : [];
  const chosenId = drafts ? known(requestedDraftId) ?? plantIds.map(known).find(Boolean) ?? drafts[0]?.draft_id : undefined;
  // A draft in the URL that is not in this version (a stale link, another version's choice) is corrected in place, and the screen says so.
  useEffect(() => {
    if (drafts && requestedDraftId && !known(requestedDraftId)) { setLinkMissed(true); onChooseDraft(null, true); }
  }, [drafts, requestedDraftId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    let live = true;
    setDraft(null);
    if (chosenId) {
      setError(null);
      void getDraft(workspaceId, chosenId).then((next) => { if (live) setDraft(next); })
        .catch((cause: unknown) => { if (live) setError(cause instanceof Error ? cause.message : "The Process draft is unavailable."); });
    }
    return () => { live = false; };
  }, [workspaceId, chosenId]);
  const nameOf = (id: string | undefined) => drafts?.find((row) => row.draft_id === id)?.name ?? "another draft";

  return <section className="design-project__panel design-project__main design-project__dynamic" aria-label="Dynamic simulation" {...designTarget("design.panel.dynamic")}>
    <h2>Dynamic</h2>
    <p className="design-project__dynamic-intro">Runs this plant's simplified process model forward in time, so you can see how it would behave over days or weeks under conditions you choose.</p>
    {error ? <p role="alert" className="design-project__error">{error}</p> : null}
    {drafts === null && !error ? <p role="status">Loading Process drafts…</p> : null}
    {drafts && !drafts.length ? <p>This version has no Process draft yet, so there is no plant to simulate. Build the flowsheet in Plant first.{" "}
      <button type="button" onClick={onOpenPlant}>Go to Plant</button></p> : null}
    {drafts && drafts.length > 1 ? <div className="design-project__dynamic-draft">
      <label htmlFor="design-dynamic-draft">Process draft to simulate</label>
      <select id="design-dynamic-draft" value={chosenId ?? ""} onChange={(event) => { setLinkMissed(false); onChooseDraft(event.target.value); }} {...designTarget("design.dynamic.draft")}>
        {drafts.map((row) => <option key={row.draft_id} value={row.draft_id}>{row.name}{plantIds.includes(row.draft_id) ? " · Plant" : ""}</option>)}
      </select>
      {plantDraftIds.length > 1 ? <p>This version's components come from {plantDraftIds.length} Process drafts; choose which one to simulate.</p> : null}
    </div> : null}
    {linkMissed ? <p role="status">The requested Process draft is not in this version; showing {nameOf(chosenId)} instead.</p> : null}
    {chosenId && !draft && !error ? <p role="status">Loading the Process draft…</p> : null}
    {draft ? <DynamicPanel key={draft.draft_id} workspaceId={workspaceId} draft={draft} guided
      onUpdated={(next) => setDraft((current) => (current?.draft_id === next.draft_id ? next : current))} /> : null}
  </section>;
}
