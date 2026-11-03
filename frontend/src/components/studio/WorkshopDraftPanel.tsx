import { useEffect, useRef, useState } from "react";
import { requestWorkshopDraft, type WorkshopDraft, type WorkshopDraftRequest } from "../../api/studio";

/** Proposal-only draft next to the Workshop questionnaire: refreshed after each answered step, never applied without Accept. */
export default function WorkshopDraftPanel({ workspaceId, step, answers, accepted, onAccept, onClear }: { workspaceId: string; step: number; answers: WorkshopDraftRequest; accepted: boolean; onAccept(source: string): void; onClear(): void }) {
  const [draft, setDraft] = useState<WorkshopDraft | null>(null);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const latest = useRef(answers); latest.current = answers;
  const sequence = useRef(0);
  // Only the step (a completed answer) triggers a draft; a response for an earlier step never overwrites a newer one.
  useEffect(() => {
    const mine = ++sequence.current; setPending(true); setFailure(null);
    const timer = window.setTimeout(() => {
      requestWorkshopDraft(workspaceId, { ...latest.current, step }).then((next) => { if (sequence.current === mine) { setDraft(next); setPending(false); } })
        .catch((reason: unknown) => { if (sequence.current === mine) { setFailure(reason instanceof Error ? reason.message : "Draft unavailable."); setPending(false); } });
    }, 250);
    return () => { window.clearTimeout(timer); if (sequence.current === mine) sequence.current += 1; };
  }, [workspaceId, step, answers.category]);
  const label = draft ? (draft.origin === "ai" ? "AI draft" : `Deterministic scaffold — AI unavailable: ${draft.reason ?? "no reason reported"}`) : null;
  return <section className="workshop-draft" role="region" aria-label="Workshop draft">
    <h3>Draft so far</h3>
    <p role="status" aria-live="polite">{pending ? "Drafting…" : failure ? `Draft unavailable: ${failure}` : label}</p>
    <p className="workshop-draft__note">Proposal only. Nothing is saved or run until you accept it and create the draft; Test and Publish still apply.</p>
    {draft && !pending ? <>
      <p>{draft.explanation}</p>
      {draft.contract ? <details><summary>Contract</summary><pre>{JSON.stringify(draft.contract, null, 2)}</pre></details> : null}
      {draft.source ? <details open><summary>Python source</summary><pre tabIndex={0}>{draft.source}</pre></details> : null}
      <div className="studio-actions">
        <button type="button" disabled={!draft.acceptable || !draft.source || accepted} onClick={() => onAccept(draft.source)}>{accepted ? "Draft accepted" : "Accept draft source"}</button>
        {accepted ? <button type="button" onClick={onClear}>Discard accepted draft</button> : null}
      </div>
    </> : null}
  </section>;
}
