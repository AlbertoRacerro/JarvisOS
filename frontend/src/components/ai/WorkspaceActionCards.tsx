import { useState } from "react";
import type { ActionOutcome } from "../../api/workspaceActions";
import { actionOriginLabel, actionStatePresentation, actionSummaryText } from "./workspaceActionPresentation";
import { studioProposalView } from "./studioProposalPresentation";

type Props = { actions: ActionOutcome[]; onApply(actionId: string): Promise<void>; onDismiss(actionId: string): Promise<void>; onUndo(actionId: string): Promise<void>; technicalDetails?: string | null; technicalInfo?: unknown };

function studioProposal(action: ActionOutcome) {
  const view = studioProposalView(action);
  if (!view) return null;
  return <div className="jarvis-studio-proposal">
    <p><strong>{view.title}</strong> · {view.category} · {view.mode === "update" ? "update selected draft" : "new draft"}</p>
    {view.question ? <p>{view.question}</p> : null}
    <div><strong>Engineering contract</strong>{view.groups.length ? view.groups.map((group) => <section key={group.title}><h4>{group.title}</h4><ul>{group.rows.map((row, index) => <li key={index}>{row}</li>)}</ul></section>) : <p>Review the proposed contract in technical details.</p>}</div>
    <div><strong>Assumptions and limits</strong><p>{view.assumptions}</p><p>Apply creates a draft only. Test and Publish remain separate actions; scientific status is unqualified.</p></div>
    <details><summary>Python source diff</summary><pre>{view.sourceDiff}</pre></details>
  </div>;
}

export default function WorkspaceActionCards({ actions, onApply, onDismiss, onUndo, technicalDetails, technicalInfo }: Props) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const run = async (id: string, fn: (actionId: string) => Promise<void>) => {
    setBusyId(id);
    try { await fn(id); } finally { setBusyId(null); }
  };
  if (!actions.length && !technicalDetails && !technicalInfo) return null;
  const details = [
    technicalDetails,
    actions.length ? JSON.stringify(actions.map(({ request, request_digest, origin, result_revision }) => ({ request, request_digest, origin, result_revision })), null, 2) : null,
    technicalInfo ? JSON.stringify(technicalInfo, null, 2) : null
  ].filter(Boolean).join("\n\n");
  return <div className="jarvis-action-list">
    {actions.map((action) => {
      const presentation = actionStatePresentation(action.state);
      return <section className={`jarvis-action-card jarvis-action-card--${presentation.tone}`} key={action.action_id} aria-label={`${presentation.label} workspace action`}>
        <header><strong>{presentation.label}</strong><span>{actionOriginLabel(action.origin)}</span></header>
        <p>{actionSummaryText(action)}</p>
        {studioProposal(action)}
        {action.surface !== "studio" && action.changes.length > 0 && <ul>{action.changes.map((change, index) => <li key={`${change.label}-${index}`}><strong>{change.label}</strong>: {change.before ? `${change.before} → ` : ""}{change.after ?? "—"}</li>)}</ul>}
        {(action.state === "refused" || action.state === "stale") && action.reason && <p className="jarvis-action-card__reason">{action.reason}</p>}
        {action.state === "proposed" && <div className="jarvis-action-card__buttons"><button type="button" className="jarvis-primary-button" disabled={busyId === action.action_id} aria-label={`Apply ${actionSummaryText(action)}`} onClick={() => void run(action.action_id, onApply)}>Apply</button><button type="button" className="jarvis-link-button" disabled={busyId === action.action_id} aria-label={`Dismiss ${actionSummaryText(action)}`} onClick={() => void run(action.action_id, onDismiss)}>Dismiss</button></div>}
        {action.state === "applied" && action.undo_available && <button type="button" className="jarvis-link-button" disabled={busyId === action.action_id} aria-label={`Undo ${actionSummaryText(action)}`} onClick={() => void run(action.action_id, onUndo)}>Undo</button>}
        {action.state === "applied" && action.result_revision && <small>Revision {action.result_revision.split(":", 1)[0]}</small>}
      </section>;
    })}
    {Boolean(details) && <details className="jarvis-action-technical"><summary>Technical details</summary><pre>{details}</pre></details>}
  </div>;
}
