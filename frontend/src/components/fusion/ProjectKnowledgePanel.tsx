import { useEffect, useMemo, useState } from "react";

import {
  approveProjectKnowledgeDraft,
  changeProjectKnowledgeRevisionState,
  createProjectKnowledgeDraft,
  getProjectKnowledgeRevalidation,
  listProjectKnowledgeRevisions,
  previewProjectKnowledgeImpact,
  reconcileProjectKnowledge,
  type ProjectKnowledgeOperation,
  type ProjectKnowledgeDraft,
  type ProjectKnowledgeImpact,
  type ProjectKnowledgeRevalidation,
  type ProjectKnowledgeRevision
} from "../../api/projectKnowledge";

type Props = Readonly<{ workspaceId: string | null; readOnly?: boolean; onChanged?: () => void }>;

function shortId(value: string | null | undefined): string {
  return value ? `${value.slice(0, 8)}…` : "None";
}

function requestKey(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

function recomputationHref(revision: ProjectKnowledgeRevision, revalidation: ProjectKnowledgeRevalidation): string {
  const params = new URLSearchParams({
    project_knowledge_revision_id: revision.id,
    project_knowledge_basis_digest: revision.projected_state_digest,
    project_knowledge_validation_set_digest: revalidation.selected_validation_set_digest
  });
  for (const requirementId of revalidation.recomputation_required) params.append("project_knowledge_requirement_id", requirementId);
  return `/design/process?${params.toString()}`;
}

function ChangeList({ operations }: { operations: ProjectKnowledgeOperation[] }) {
  return <ul className="basis-change-list">{operations.map((operation, index) => <li key={operation.operation_id ?? index}>
    <strong>{operation.operation_kind.replace(/_/g, " ")} {operation.owner_kind.replace(/_/g, " ")}</strong>
    <p>{String(operation.fields.statement ?? operation.fields.title ?? operation.fields.name ?? operation.owner_id ?? "Project record")}</p>
    <details><summary>All proposed fields</summary><pre>{JSON.stringify(operation.fields, null, 2)}</pre></details>
  </li>)}</ul>;
}

export default function ProjectKnowledgePanel({ workspaceId, readOnly = false, onChanged }: Props) {
  const [revisions, setRevisions] = useState<ProjectKnowledgeRevision[]>([]);
  const [selectedRevisionId, setSelectedRevisionId] = useState<string | null>(null);
  const [statement, setStatement] = useState("");
  const [draft, setDraft] = useState<ProjectKnowledgeDraft | null>(null);
  const [impact, setImpact] = useState<ProjectKnowledgeImpact | null>(null);
  const [revalidation, setRevalidation] = useState<ProjectKnowledgeRevalidation | null>(null);
  const [knownFailAck, setKnownFailAck] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const currentReconciled = useMemo(
    () => revisions.find((revision) => revision.state === "reconciled" && revision.reconciled_snapshot_id) ?? null,
    [revisions]
  );
  const working = useMemo(() => revisions.filter((revision) => revision.state === "working"), [revisions]);
  const selectedRevision = useMemo(
    () => revisions.find((revision) => revision.id === selectedRevisionId) ?? working[0] ?? currentReconciled ?? revisions[0] ?? null,
    [currentReconciled, revisions, selectedRevisionId, working]
  );
  const selectedWorking = selectedRevision?.state === "working" ? selectedRevision : null;
  const selectedParent = selectedRevision && (selectedRevision.state === "working" || selectedRevision.state === "reconciled") ? selectedRevision : null;

  const reload = async () => {
    if (!workspaceId) {
      setRevisions([]);
      setSelectedRevisionId(null);
      return;
    }
    const next = await listProjectKnowledgeRevisions(workspaceId);
    setRevisions(next);
    const nextWorking = next.find((revision) => revision.state === "working") ?? null;
    const nextReconciled = next.find((revision) => revision.state === "reconciled" && revision.reconciled_snapshot_id) ?? null;
    setSelectedRevisionId((current) => next.some((revision) => revision.id === current) ? current : nextWorking?.id ?? nextReconciled?.id ?? next[0]?.id ?? null);
  };

  useEffect(() => {
    setDraft(null);
    setImpact(null);
    setRevalidation(null);
    setKnownFailAck("");
    setError(null);
    void reload().catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "Project Knowledge read failed"));
  }, [workspaceId]);

  useEffect(() => {
    if (!workspaceId || !selectedWorking) {
      setRevalidation(null);
      return;
    }
    void getProjectKnowledgeRevalidation(workspaceId, selectedWorking.id)
      .then(setRevalidation)
      .catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "Revalidation read failed"));
  }, [selectedWorking?.id, workspaceId]);

  const act = async (operation: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await operation();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : "Project Knowledge transition failed");
    } finally {
      setBusy(false);
    }
  };

  const stageRequirement = () => act(async () => {
    if (!workspaceId || readOnly || !statement.trim()) return;
    const nextDraft = await createProjectKnowledgeDraft({
      workspace_id: workspaceId,
      parent_kind: selectedParent?.state === "working" ? "working" : "reconciled",
      parent_revision_id: selectedParent?.id ?? currentReconciled?.id ?? null,
      operations: [{
        owner_kind: "requirement",
        operation_kind: "create",
        fields: {
          statement: statement.trim(),
          status: "active",
          basis_kind: "requirement",
          reconciliation_gate: "advisory"
        }
      }]
    });
    const nextImpact = await previewProjectKnowledgeImpact(workspaceId, nextDraft.id);
    setDraft(nextDraft);
    setImpact(nextImpact);
  });

  const approve = () => act(async () => {
    if (!workspaceId || readOnly || !draft || !impact || !impact.complete) return;
    const result = await approveProjectKnowledgeDraft({
      workspace_id: workspaceId,
      approval_request_key: requestKey("project-basis-approve"),
      draft_id: draft.id,
      expected_draft_revision_token: draft.revision_token,
      expected_preview_digest: impact.digest,
      origin: "operator"
    });
    if (result.state !== "success" || !result.working_revision_id) throw new Error(result.failure_code ?? "Approval did not produce a working revision");
    setStatement("");
    setDraft(null);
    setImpact(null);
    await reload();
    setSelectedRevisionId(result.working_revision_id);
  });

  const discard = () => act(async () => {
    if (!workspaceId || readOnly || !selectedWorking) return;
    await changeProjectKnowledgeRevisionState(selectedWorking.id, { workspace_id: workspaceId, action: "discard" });
    setRevalidation(null);
    await reload();
  });

  const reconcile = () => act(async () => {
    if (!workspaceId || readOnly || !selectedWorking || !revalidation?.complete) return;
    if (revalidation.known_fail_requirement_ids.length && !knownFailAck.trim()) return;
    const result = await reconcileProjectKnowledge({
      workspace_id: workspaceId,
      idempotency_key: requestKey("project-basis-reconcile"),
      working_revision_id: selectedWorking.id,
      expected_target_snapshot_id: currentReconciled?.reconciled_snapshot_id ?? null,
      expected_target_digest: selectedWorking.projected_state_digest,
      expected_selected_validation_set_digest: revalidation.selected_validation_set_digest,
      known_fail_acknowledgement: knownFailAck.trim() || null,
      policy_identity: knownFailAck.trim() ? "operator-explicit-known-fail-v0" : null
    });
    if (result.state !== "success") throw new Error(result.failure_code ?? "Reconciliation did not commit");
    setKnownFailAck("");
    setRevalidation(null);
    await reload();
    onChanged?.();
  });

  if (!workspaceId) {
    return <section className="final-fusion__panel final-fusion__basis" aria-label="Project Knowledge"><header className="final-fusion__panel-head"><h2>Project Knowledge</h2><span>Unavailable</span></header><div className="final-fusion__empty">Select a workspace to inspect server-owned working revisions.</div></section>;
  }

  return <section className="final-fusion__panel final-fusion__basis" aria-label="Project Knowledge">
    <header className="final-fusion__panel-head"><h2>Project Knowledge</h2><span>{readOnly ? "Revision history" : "Changes & revisions"}</span></header>
    <div className="final-fusion__dossier-top"><strong>{currentReconciled ? "Reconciled project basis" : "No reconciled revision yet"}</strong><span>Working revisions · {working.length}</span></div>
    {error && <div className="final-fusion__source-empty"><strong>Transition rejected</strong><span>{error}</span></div>}
    <div className="final-fusion__toolbar-line">
      <span>Revision history</span>
      <select aria-label="Project Knowledge revision" value={selectedRevision?.id ?? ""} onChange={(event) => setSelectedRevisionId(event.target.value || null)} disabled={busy || !revisions.length}>
        <option value="">Current reconciled basis</option>
        {revisions.map((revision) => <option key={revision.id} value={revision.id}>{revision.state} · {new Date(revision.created_at).toLocaleString()}</option>)}
      </select>
    </div>
    {selectedRevision && <div className="basis-revision-summary"><p>{selectedRevision.state === "working" ? "Approved changes waiting to be reconciled into the current project basis." : selectedRevision.state === "reconciled" ? "These changes have been reconciled into the project basis." : "Historical revision, retained for inspection."}</p><ChangeList operations={selectedRevision.operations} /></div>}
    {selectedRevision && <details><summary>Technical revision details</summary><div className="final-fusion__facts"><div>Revision · {selectedRevision.id}</div><div>State · {selectedRevision.state}</div><div>Parent · {selectedRevision.parent_kind}:{shortId(selectedRevision.parent_revision_id)}</div><div>Change-set digest · {selectedRevision.change_set_digest}</div><div>Projected state digest · {selectedRevision.projected_state_digest}</div><div>Snapshot · {shortId(selectedRevision.reconciled_snapshot_id)}</div>{selectedRevision.superseded_by_revision_id && <div>Superseded by · {selectedRevision.superseded_by_revision_id}</div>}</div></details>}
    {!readOnly && <div className="final-fusion__toolbar-line"><span>{selectedParent ? `Add a requirement based on the selected ${selectedParent.state} revision.` : "Add a requirement to the project. Preview the change before accepting it."}</span><input aria-label="Requirement statement" value={statement} onChange={(event) => setStatement(event.target.value)} disabled={busy || (revisions.length > 0 && !selectedParent)} placeholder="Requirement statement" /><button type="button" disabled={busy || (revisions.length > 0 && !selectedParent) || !statement.trim()} onClick={stageRequirement}>Preview impact</button></div>}
    {draft && impact && <div className="final-fusion__source-list"><div className="final-fusion__disclosure-row"><strong>Review proposed changes</strong><em>{impact.complete ? "Impact complete" : "Impact incomplete"}</em></div><ChangeList operations={draft.operations} /><details><summary>Impact details</summary><div className="final-fusion__facts"><div>Affected refs · {impact.affected_refs.length ? impact.affected_refs.join(", ") : "None"}</div><div>Owner tokens · {Object.entries(impact.owner_tokens).map(([ref, token]) => `${ref}=${token}`).join(" · ") || "None"}</div><div>Diagnostics · {impact.diagnostics.join(", ") || "None"}</div><div>Impact digest · {impact.digest}</div></div></details><div className="final-fusion__toolbar-line"><span>Approve all creates an immutable working revision; it does not reconcile canonical truth.</span><button type="button" disabled={busy || !impact.complete} onClick={approve}>Approve all</button></div></div>}
    {selectedWorking && revalidation && <div className="final-fusion__source-list"><div className="final-fusion__disclosure-row"><strong>Deterministic revalidation</strong><em>{revalidation.complete ? "Terminal" : "Blocked"}</em></div><div className="final-fusion__facts"><div>PASS/current evidence · {revalidation.current_validation_ids.length}</div><div>Blocking criteria · {revalidation.blocking_requirement_ids.join(", ") || "None"}</div><div>Known FAIL · {revalidation.known_fail_requirement_ids.join(", ") || "None"}</div><div>Recomputation required · {revalidation.recomputation_required.join(", ") || "None"}</div><div>Diagnostics · {revalidation.diagnostics.join(" · ") || "None"}</div><div>Validation-set digest · {revalidation.selected_validation_set_digest}</div></div>{revalidation.recomputation_required.length > 0 && <div className="final-fusion__toolbar-line"><span>Recomputation remains owned by Process; the handoff carries this exact revision, basis digest, validation-set digest and requirement refs.</span><a href={recomputationHref(selectedWorking, revalidation)}>Open Process with context</a></div>}{!readOnly && revalidation.known_fail_requirement_ids.length > 0 && <div className="final-fusion__toolbar-line"><span>Known mandatory FAIL requires explicit acknowledgement.</span><input aria-label="Known fail acknowledgement" value={knownFailAck} onChange={(event) => setKnownFailAck(event.target.value)} disabled={busy} placeholder="Acknowledge known FAIL evidence" /></div>}{!readOnly && <div className="final-fusion__toolbar-line"><button type="button" disabled={busy} onClick={discard}>Discard working revision</button><button type="button" disabled={busy || !revalidation.complete || (revalidation.known_fail_requirement_ids.length > 0 && !knownFailAck.trim())} onClick={reconcile}>Final reconcile</button></div>}</div>}
    <div className="final-fusion__context-strip">Changes become part of the project only when you accept them. Older accepted versions can be reopened as a starting point; discarded and superseded revisions remain inspectable but cannot be edited.</div>
  </section>;
}
