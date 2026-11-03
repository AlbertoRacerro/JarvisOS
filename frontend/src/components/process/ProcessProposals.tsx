import { useCallback, useEffect, useState } from "react";
import {
  announceDraftChanged,
  approveProposal,
  DraftApiError,
  formatQuantity,
  listDrafts,
  listProposals,
  rejectProposal,
  DRAFT_CHANGED_EVENT,
  type Proposal,
} from "../../api/processDraft";

const propertyLabel = (property: string) => property.replace(/_/g, " ");

type Props = Readonly<{
  workspaceId: string;
  draftId?: string | null;
  // The Process page passes its own proposals; the Sidecar polls the workspace's latest draft.
  proposals?: Proposal[];
  onDecided?(): void;
}>;

/** Structured Hermes/operator change sets: nothing changes until the operator approves. */
export default function ProcessProposals({ workspaceId, draftId, proposals, onDecided }: Props) {
  const [polled, setPolled] = useState<{ draftId: string; proposals: Proposal[] } | null>(null);
  const [selection, setSelection] = useState<Record<string, number[]>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const owned = proposals !== undefined;

  const refresh = useCallback(async () => {
    if (owned) return;
    try {
      const target = draftId ?? (await listDrafts(workspaceId))[0]?.draft_id;
      if (!target) return setPolled(null);
      const rows = await listProposals(workspaceId, target);
      setPolled({ draftId: target, proposals: rows.filter((row) => row.state === "pending" || row.state === "stale") });
    } catch {
      /* The Sidecar keeps working when the process draft owner is unavailable. */
    }
  }, [draftId, owned, workspaceId]);

  useEffect(() => {
    if (owned) return;
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    const onChanged = () => void refresh();
    window.addEventListener(DRAFT_CHANGED_EVENT, onChanged);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener(DRAFT_CHANGED_EVENT, onChanged);
    };
  }, [owned, refresh]);

  const rows = owned ? proposals : polled?.proposals ?? [];
  const activeDraft = owned ? draftId : polled?.draftId;
  if (!rows.length || !activeDraft) return null;

  const decide = async (proposal: Proposal, approve: boolean) => {
    setBusy(proposal.proposal_id);
    setMessage("");
    try {
      const chosen = selection[proposal.proposal_id];
      const partial = chosen && chosen.length !== proposal.changes.length ? chosen : undefined;
      if (approve) await approveProposal(workspaceId, activeDraft, proposal.proposal_id, partial);
      else await rejectProposal(workspaceId, activeDraft, proposal.proposal_id);
      announceDraftChanged(activeDraft);
      onDecided?.();
      await refresh();
    } catch (cause) {
      setMessage(cause instanceof DraftApiError ? `${cause.message} (${cause.code})` : "The decision could not be recorded.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="process-proposals" aria-label="Proposed process changes">
      <h3>Proposed flowsheet changes</h3>
      {rows.map((proposal) => {
        const chosen = selection[proposal.proposal_id] ?? proposal.changes.map((_change, index) => index);
        const stale = proposal.state === "stale";
        return (
          <article key={proposal.proposal_id} className={`process-proposal${stale ? " process-proposal--stale" : ""}`}>
            <header>
              <strong>{proposal.source.startsWith("hermes") ? "Hermes proposes" : "Proposal"}</strong>
              <span>on revision {proposal.base_revision.split(":")[0]}</span>
            </header>
            {proposal.rationale && <p className="process-proposal__why">{proposal.rationale}</p>}
            <table>
              <thead>
                <tr><th aria-label="Include" /><th>Target</th><th>Property</th><th>Current</th><th>Proposed</th></tr>
              </thead>
              <tbody>
                {proposal.changes.map((change, index) => (
                  <tr key={`${change.target}-${change.property}`}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Include ${change.target} ${propertyLabel(change.property)}`}
                        checked={chosen.includes(index)}
                        disabled={stale || proposal.changes.length === 1}
                        onChange={(event) =>
                          setSelection((current) => ({
                            ...current,
                            [proposal.proposal_id]: event.target.checked
                              ? [...chosen, index].sort()
                              : chosen.filter((item) => item !== index),
                          }))
                        }
                      />
                    </td>
                    <td>{change.target}</td>
                    <td>{propertyLabel(change.property)}</td>
                    <td>{formatQuantity(change.current)}</td>
                    <td className="process-proposal__new">{formatQuantity(change.proposed)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {stale ? (
              <p className="process-proposal__stale">The draft changed after this proposal. It can only be rejected.</p>
            ) : (
              <p className="process-proposal__note">The draft is unchanged until you approve. DWSIM is not contacted.</p>
            )}
            <div className="process-proposal__actions">
              <button
                type="button"
                className="process-proposal__approve"
                disabled={stale || busy !== null || chosen.length === 0}
                onClick={() => void decide(proposal, true)}
              >
                Approve{chosen.length !== proposal.changes.length ? ` ${chosen.length} of ${proposal.changes.length}` : ""}
              </button>
              <button type="button" disabled={busy !== null} onClick={() => void decide(proposal, false)}>
                Reject
              </button>
            </div>
          </article>
        );
      })}
      {message && <p role="alert" className="process-proposal__error">{message}</p>}
    </section>
  );
}
