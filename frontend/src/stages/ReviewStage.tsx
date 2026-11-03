import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  listMemoryProposals,
  promoteMemoryRecord,
  promoteParameterReplacement,
  rejectMemoryRecord,
  type MemoryRecord,
  type MemoryStatusFilter,
  type ReplacementResult
} from "../api/memory";
import {
  acceptsReviewMutation,
  acceptsReviewRequest,
  isActionable,
  nextAfterRemoval,
  orderedRecords,
  promotionRoute,
  recordLabel,
  retainedSelection,
  type ReviewMutationContext,
  type ReviewRequestContext
} from "../components/review/reviewState";
import Button from "../components/ui/Button";
import InlineNotice from "../components/ui/InlineNotice";
import Surface from "../components/ui/Surface";
import type { PrimaryStageProps } from "./registry";

type LoadState = "idle" | "loading" | "ready" | "error";
type TransitionKind = "accept" | "reject";
const FILTERS: readonly MemoryStatusFilter[] = ["proposed", "accepted", "rejected", "superseded", "all"];

function value(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not recorded";
  return String(value);
}
function Fact({ label, children }: { label: string; children: unknown }) { return <div className="review-fact"><dt>{label}</dt><dd>{value(children)}</dd></div>; }
function replacementNotice(result: ReplacementResult): string { return `Accepted replacement ${result.accepted_parameter.id}; superseded ${result.superseded_parameter.id}; freshness invalidation affected ${result.invalidation.affected_count}; ${result.invalidation.source_ref} → ${result.invalidation.replacement_ref}.`; }

function ReviewStage({ workspaceId, onShellRegionsChange }: PrimaryStageProps) {
  const [statusFilter, setStatusFilter] = useState<MemoryStatusFilter>("proposed");
  const [records, setRecords] = useState<MemoryRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loadState, setLoadState] = useState<LoadState>(workspaceId ? "loading" : "idle");
  const [message, setMessage] = useState<string | null>(null);
  const [transitionNotice, setTransitionNotice] = useState<string | null>(null);
  const [busyRecordId, setBusyRecordId] = useState<string | null>(null);
  const requestGeneration = useRef(0);
  const mutationGeneration = useRef(0);
  const currentRequest = useRef<ReviewRequestContext | null>(null);
  const currentMutation = useRef<ReviewMutationContext | null>(null);
  const workspaceRef = useRef(workspaceId);
  const filterRef = useRef(statusFilter);
  const selectedRef = useRef(selectedId);
  const recordsRef = useRef(records);
  const itemRefs = useRef(new Map<string, HTMLButtonElement>());
  const filterControlRef = useRef<HTMLSelectElement>(null);
  workspaceRef.current = workspaceId; filterRef.current = statusFilter; selectedRef.current = selectedId; recordsRef.current = records;
  const selected = useMemo(() => records.find((record) => record.id === selectedId) ?? null, [records, selectedId]);

  const loadRecords = useCallback(async (targetWorkspaceId: string, targetFilter: MemoryStatusFilter, preferredSelection: string | null = selectedRef.current, removedId: string | null = null, focusAfterLoad = false) => {
    const previousRecords = recordsRef.current;
    const request: ReviewRequestContext = { generation: ++requestGeneration.current, workspaceId: targetWorkspaceId, statusFilter: targetFilter };
    currentRequest.current = request; setLoadState("loading"); setMessage(null);
    try {
      const response = orderedRecords(await listMemoryProposals(targetWorkspaceId, targetFilter));
      if (!acceptsReviewRequest(currentRequest.current, request)) return;
      if (workspaceRef.current !== targetWorkspaceId || filterRef.current !== targetFilter) return;
      const preferredStillVisible = preferredSelection ? response.some((record) => record.id === preferredSelection) : false;
      const removalTarget = removedId ? nextAfterRemoval(previousRecords, removedId) : null;
      const nextSelected = preferredStillVisible ? preferredSelection : removalTarget && response.some((record) => record.id === removalTarget) ? removalTarget : retainedSelection(response, preferredSelection);
      recordsRef.current = response; setRecords(response); selectedRef.current = nextSelected; setSelectedId(nextSelected); setLoadState("ready");
      if (focusAfterLoad) requestAnimationFrame(() => { if (nextSelected) itemRefs.current.get(nextSelected)?.focus(); else filterControlRef.current?.focus(); });
    } catch (error) {
      if (!acceptsReviewRequest(currentRequest.current, request)) return;
      if (workspaceRef.current !== targetWorkspaceId || filterRef.current !== targetFilter) return;
      recordsRef.current = []; setRecords([]); selectedRef.current = null; setSelectedId(null); setLoadState("error"); setMessage(error instanceof Error ? error.message : "Proposal list unavailable.");
    }
  }, []);

  useLayoutEffect(() => {
    currentRequest.current = null; currentMutation.current = null; requestGeneration.current += 1; mutationGeneration.current += 1;
    recordsRef.current = []; setRecords([]); setSelectedId(null); selectedRef.current = null; setMessage(null); setTransitionNotice(null); setLoadState(workspaceId ? "loading" : "idle");
    if (workspaceId) void loadRecords(workspaceId, statusFilter, null);
  }, [loadRecords, statusFilter, workspaceId]);

  const selectRecord = useCallback((recordId: string) => { mutationGeneration.current += 1; currentMutation.current = null; selectedRef.current = recordId; setSelectedId(recordId); setMessage(null); setTransitionNotice(null); }, []);
  const changeFilter = useCallback((next: MemoryStatusFilter) => {
    if (next === filterRef.current) return;
    currentRequest.current = null; currentMutation.current = null; requestGeneration.current += 1; mutationGeneration.current += 1;
    recordsRef.current = []; setRecords([]); selectedRef.current = null; setSelectedId(null); setMessage(null); setTransitionNotice(null); setLoadState(workspaceRef.current ? "loading" : "idle"); filterRef.current = next; setStatusFilter(next);
  }, []);

  const transition = useCallback(async (record: MemoryRecord, kind: TransitionKind) => {
    if (!workspaceRef.current || busyRecordId !== null || !isActionable(record)) return;
    const request: ReviewMutationContext = { generation: ++mutationGeneration.current, workspaceId: workspaceRef.current, recordKind: record.record_kind, recordId: record.id };
    currentMutation.current = request; setBusyRecordId(record.id); setMessage(null); setTransitionNotice(null);
    try {
      let notice: string;
      if (kind === "reject") { const result = await rejectMemoryRecord(record.record_kind, record.id); notice = `Rejected ${result.record_kind} ${result.id}.`; }
      else if (promotionRoute(record) === "replacement") notice = replacementNotice(await promoteParameterReplacement(record.id));
      else { const result = await promoteMemoryRecord(record.record_kind, record.id); notice = `Accepted ${result.record_kind} ${result.id}.`; }
      if (!acceptsReviewMutation(currentMutation.current, request)) return;
      if (workspaceRef.current !== request.workspaceId || selectedRef.current !== request.recordId) return;
      setTransitionNotice(notice); await loadRecords(request.workspaceId, filterRef.current, request.recordId, request.recordId, true);
    } catch (error) {
      if (!acceptsReviewMutation(currentMutation.current, request)) return;
      if (workspaceRef.current !== request.workspaceId || selectedRef.current !== request.recordId) return;
      setMessage(error instanceof Error ? error.message : "Proposal transition failed.");
    } finally { setBusyRecordId((current) => current === record.id ? null : current); }
  }, [busyRecordId, loadRecords]);

  const navigator = useMemo(() => <div className="review-nav" aria-label="Proposal review navigator">
    <label className="review-filter"><span>Status</span><select ref={filterControlRef} value={statusFilter} onChange={(event) => changeFilter(event.target.value as MemoryStatusFilter)}>{FILTERS.map((filter) => <option key={filter} value={filter}>{filter}</option>)}</select></label>
    <div className="review-nav__summary" aria-live="polite">{loadState === "loading" ? "Loading proposals…" : `${records.length} record${records.length === 1 ? "" : "s"}`}</div>
    <div className="review-nav__list">{records.map((record) => <button key={record.id} ref={(node) => { if (node) itemRefs.current.set(record.id, node); else itemRefs.current.delete(record.id); }} type="button" className="review-nav__item" aria-pressed={record.id === selectedId} onClick={() => selectRecord(record.id)}><span className="review-nav__kind">{record.record_kind}</span><strong>{recordLabel(record)}</strong><span>{record.status} · {record.origin}</span></button>)}</div>
  </div>, [changeFilter, loadState, records, selectRecord, selectedId, statusFilter]);

  useEffect(() => { onShellRegionsChange({ navigator }); return () => onShellRegionsChange({}); }, [navigator, onShellRegionsChange]);

  if (!workspaceId) return <section className="review-workbench" aria-labelledby="review-stage-title"><div className="page-header"><p className="eyebrow">Proposal authority</p><h1 id="review-stage-title">Review</h1></div><InlineNotice tone="neutral">Choose a workspace elsewhere in the shell to inspect proposal records.</InlineNotice></section>;

  return <section className="review-workbench" aria-labelledby="review-stage-title" aria-busy={busyRecordId !== null}>
    <div className="page-header review-workbench__header"><div><p className="eyebrow">Proposal authority</p><h1 id="review-stage-title">Review</h1></div><Button variant="secondary" disabled={loadState === "loading" || busyRecordId !== null} onClick={() => void loadRecords(workspaceId, statusFilter)}>Refresh</Button></div>
    {message && <InlineNotice tone="danger">{message}</InlineNotice>}{transitionNotice && <InlineNotice tone="success">{transitionNotice}</InlineNotice>}
    {loadState === "loading" && records.length === 0 && <InlineNotice tone="neutral">Loading canonical MemoryStore proposals…</InlineNotice>}
    {loadState === "ready" && records.length === 0 && <InlineNotice tone="neutral">No {statusFilter === "all" ? "proposal records" : statusFilter + " proposals"} in this workspace.</InlineNotice>}
    {selected && <Surface as="article" className="review-card">
      <header className="review-card__header"><div><p className="eyebrow">{selected.record_kind} · {selected.status}</p><h2>{recordLabel(selected)}</h2></div><span className="review-card__origin">{selected.origin}</span></header>
      <dl className="review-facts">
        {selected.record_kind === "assumption" && <><Fact label="Statement">{selected.statement}</Fact><Fact label="Scope">{selected.scope}</Fact><Fact label="Confidence">{selected.confidence}</Fact><Fact label="Source reference">{selected.source_ref}</Fact><Fact label="Notes">{selected.notes}</Fact></>}
        {selected.record_kind === "parameter" && <><Fact label="Name">{selected.name}</Fact><Fact label="Symbol">{selected.symbol}</Fact><Fact label="Value">{selected.value}</Fact><Fact label="Unit">{selected.unit}</Fact><Fact label="Value status">{selected.value_status}</Fact><Fact label="Minimum">{selected.value_min}</Fact><Fact label="Maximum">{selected.value_max}</Fact><Fact label="Confidence">{selected.confidence}</Fact><Fact label="Source reference">{selected.source_ref}</Fact><Fact label="Notes">{selected.notes}</Fact><Fact label="Replacement target">{selected.supersedes_parameter_id}</Fact></>}
        {selected.record_kind === "decision" && <><Fact label="Title">{selected.title}</Fact><Fact label="Decision">{selected.decision_text}</Fact><Fact label="Rationale">{selected.rationale}</Fact><Fact label="Linked run">{selected.linked_run_id}</Fact><Fact label="Notes">{selected.notes}</Fact></>}
      </dl>
      {isActionable(selected) ? <div className="review-actions"><Button disabled={busyRecordId !== null} onClick={() => void transition(selected, "accept")}>Accept</Button><Button variant="danger" disabled={busyRecordId !== null} onClick={() => void transition(selected, "reject")}>Reject</Button>{busyRecordId === selected.id && <span role="status">Applying canonical transition…</span>}</div> : <p className="review-card__historical">Historical record. Only proposed records can be transitioned.</p>}
      <details className="review-technical"><summary>Technical details</summary><dl className="review-facts review-facts--meta"><Fact label="Canonical id">{selected.id}</Fact><Fact label="Status">{selected.status}</Fact><Fact label="Source AI job">{selected.source_ai_job_id}</Fact><Fact label="Created">{selected.created_at}</Fact><Fact label="Updated">{selected.updated_at}</Fact><Fact label="Promoted">{selected.promoted_at}</Fact></dl></details>
    </Surface>}
  </section>;
}
export default ReviewStage;
