import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { listAssumptions, listDecisions, listModelSpecs, listWorkspaces, type Assumption, type Decision, type ModelSpec, type Workspace } from "../api/client";
import { listCanonicalParameters, ParameterLifecycleApiError, transitionCanonicalParameter, updateCanonicalParameter, type CanonicalParameter, type ParameterEditInput, type ParameterLifecycleAction } from "../api/parameterLifecycle";
import AppLink, { type Navigate } from "../app/AppLink";
import { acceptsWorkspaceResponse, chooseEngineeringSelection, ENGINEERING_KINDS, projectEngineeringData, recordKey, visibleEngineeringRecords, type EngineeringKind, type EngineeringRecordProjection } from "../components/engineering-data/engineeringDataState";

type Props = {
  workspaceId: string | null;
  onWorkspaceChange(next: string | null): void;
  navigate: Navigate;
};
type LoadState = "idle" | "loading" | "ready" | "error";

type MutationNotice = Readonly<{ kind: "success" | "error"; text: string }>;

const KIND_LABEL: Readonly<Record<EngineeringKind, string>> = {
  "model-spec": "Model specs",
  assumption: "Assumptions",
  parameter: "Parameters",
  decision: "Decisions",
};

const LIFECYCLE_LABEL: Readonly<Record<string, string>> = {
  active: "Active",
  inactive: "Inactive",
  superseded: "Superseded",
  archived: "Archived",
  deleted: "Deleted",
};

function shown(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "Unavailable";
  return String(value);
}

function linkedParameterTarget(search: string): string | null {
  const params = new URLSearchParams(search);
  if (params.get("kind") !== "parameter") return null;
  const id = params.get("id")?.trim() ?? "";
  return id || null;
}

function mutationMessage(error: unknown): string {
  if (!(error instanceof ParameterLifecycleApiError)) return error instanceof Error ? error.message : "Canonical Parameter mutation failed.";
  if (error.code === "parameter_stale") return "This Parameter changed after you reviewed it. Current server truth has been refreshed; review it before trying again.";
  if (error.code === "parameter_lifecycle_dependents_require_reconciliation") return "Current dependent records prevent a truthful canonical change. Use replacement/reconciliation authority before changing this Parameter.";
  if (error.code === "parameter_not_active") return "Only an Active Parameter can be edited. Refresh current server truth before continuing.";
  if (error.code === "parameter_lifecycle_transition_invalid") return "That lifecycle transition is no longer valid for the current Parameter state.";
  return error.message;
}

function lifecycleConsequence(action: ParameterLifecycleAction): string {
  if (action === "delete") return "remove it from normal product use while retaining its server-side tombstone for audit";
  if (action === "archive") return "move it out of normal current project use while retaining it as project history";
  if (action === "deactivate") return "remove it from current canonical use while keeping it as a valid alternative";
  return "return it to current canonical use";
}

function EngineeringData({ workspaceId, onWorkspaceChange, navigate }: Props) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceState, setWorkspaceState] = useState<LoadState>("loading");
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [modelSpecs, setModelSpecs] = useState<ModelSpec[]>([]);
  const [assumptions, setAssumptions] = useState<Assumption[]>([]);
  const [parameters, setParameters] = useState<CanonicalParameter[]>([]);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [modelSpecsState, setModelSpecsState] = useState<LoadState>("idle");
  const [assumptionsState, setAssumptionsState] = useState<LoadState>("idle");
  const [parametersState, setParametersState] = useState<LoadState>("idle");
  const [decisionsState, setDecisionsState] = useState<LoadState>("idle");
  const [modelSpecsError, setModelSpecsError] = useState<string | null>(null);
  const [assumptionsError, setAssumptionsError] = useState<string | null>(null);
  const [parametersError, setParametersError] = useState<string | null>(null);
  const [decisionsError, setDecisionsError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [enabledKinds, setEnabledKinds] = useState<EngineeringKind[]>([...ENGINEERING_KINDS]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [sourceTargetMessage, setSourceTargetMessage] = useState<string | null>(null);
  const [showParameterHistory, setShowParameterHistory] = useState(false);
  const [mutationPending, setMutationPending] = useState(false);
  const [mutationNotice, setMutationNotice] = useState<MutationNotice | null>(null);
  const workspaceDiscoveryGeneration = useRef(0);
  const recordsGeneration = useRef(0);
  const currentWorkspace = useRef(workspaceId);
  currentWorkspace.current = workspaceId;

  const sourceParameterId = linkedParameterTarget(window.location.search);

  const clearRecords = () => {
    recordsGeneration.current += 1;
    setModelSpecs([]);
    setAssumptions([]);
    setParameters([]);
    setDecisions([]);
    setModelSpecsState("idle");
    setAssumptionsState("idle");
    setParametersState("idle");
    setDecisionsState("idle");
    setModelSpecsError(null);
    setAssumptionsError(null);
    setParametersError(null);
    setDecisionsError(null);
    setSelectedKey(null);
    setSourceTargetMessage(null);
    setMutationNotice(null);
    setMutationPending(false);
  };

  const requestWorkspaceChange = (next: string | null) => {
    clearRecords();
    onWorkspaceChange(next);
  };

  const loadWorkspaces = () => {
    const generation = ++workspaceDiscoveryGeneration.current;
    setWorkspaceState("loading");
    setWorkspaceError(null);
    void listWorkspaces().then((rows) => {
      if (generation !== workspaceDiscoveryGeneration.current) return;
      setWorkspaces(rows);
      setWorkspaceState("ready");
      const active = currentWorkspace.current;
      if (active === null || !rows.some((row) => row.id === active)) requestWorkspaceChange(rows[0]?.id ?? null);
    }).catch((error: Error) => {
      if (generation !== workspaceDiscoveryGeneration.current) return;
      setWorkspaceState("error");
      setWorkspaceError(error.message);
    });
  };

  useEffect(loadWorkspaces, []);

  const loadRecords = (targetWorkspace: string, includeParameterHistory = showParameterHistory) => {
    const generation = ++recordsGeneration.current;
    const accepted = () => acceptsWorkspaceResponse(generation, targetWorkspace, recordsGeneration.current, currentWorkspace.current);

    setModelSpecsState("loading");
    setAssumptionsState("loading");
    setParametersState("loading");
    setDecisionsState("loading");
    setModelSpecsError(null);
    setAssumptionsError(null);
    setParametersError(null);
    setDecisionsError(null);

    void listModelSpecs(targetWorkspace).then((rows) => {
      if (!accepted()) return;
      setModelSpecs(rows);
      setModelSpecsState("ready");
    }).catch((error: Error) => {
      if (!accepted()) return;
      setModelSpecs([]);
      setModelSpecsState("error");
      setModelSpecsError(error.message);
    });
    void listAssumptions(targetWorkspace).then((rows) => {
      if (!accepted()) return;
      setAssumptions(rows);
      setAssumptionsState("ready");
    }).catch((error: Error) => {
      if (!accepted()) return;
      setAssumptions([]);
      setAssumptionsState("error");
      setAssumptionsError(error.message);
    });
    void listCanonicalParameters(targetWorkspace, includeParameterHistory).then((rows) => {
      if (!accepted()) return;
      setParameters(rows);
      setParametersState("ready");
    }).catch((error: Error) => {
      if (!accepted()) return;
      setParameters([]);
      setParametersState("error");
      setParametersError(error.message);
    });
    void listDecisions(targetWorkspace).then((rows) => {
      if (!accepted()) return;
      setDecisions(rows);
      setDecisionsState("ready");
    }).catch((error: Error) => {
      if (!accepted()) return;
      setDecisions([]);
      setDecisionsState("error");
      setDecisionsError(error.message);
    });
  };

  useEffect(() => {
    clearRecords();
    if (workspaceId) loadRecords(workspaceId, showParameterHistory);
  }, [workspaceId, showParameterHistory]);

  useEffect(() => {
    setSourceTargetMessage(null);
    if (!sourceParameterId) return;
    setEnabledKinds((current) => current.includes("parameter")
      ? current
      : ENGINEERING_KINDS.filter((kind) => current.includes(kind) || kind === "parameter"));
    if (parametersState !== "ready" || !workspaceId) return;
    const exact = parameters.find((parameter) => parameter.id === sourceParameterId && parameter.workspace_id === workspaceId);
    if (!exact) {
      setSourceTargetMessage("The linked Parameter is unavailable in the current workspace/current lifecycle view.");
      return;
    }
    setQuery("");
    setSelectedKey(recordKey({ kind: "parameter", id: exact.id }));
  }, [sourceParameterId, parametersState, parameters, workspaceId]);

  const projected = useMemo(() => projectEngineeringData({ modelSpecs, assumptions, parameters, decisions }), [modelSpecs, assumptions, parameters, decisions]);
  const visible = useMemo(() => visibleEngineeringRecords(projected, query, enabledKinds), [projected, query, enabledKinds]);
  useEffect(() => setSelectedKey((current) => chooseEngineeringSelection(current, visible)), [visible]);
  const selected = useMemo(() => visible.find((row) => recordKey(row) === selectedKey) ?? null, [visible, selectedKey]);

  const toggleKind = (kind: EngineeringKind) => setEnabledKinds((current) => current.includes(kind) ? current.filter((value) => value !== kind) : ENGINEERING_KINDS.filter((value) => current.includes(value) || value === kind));
  const anyLoading = [modelSpecsState, assumptionsState, parametersState, decisionsState].some((state) => state === "loading");

  const applyParameterMutation = async (parameter: CanonicalParameter, request: () => Promise<CanonicalParameter>, successText: string) => {
    if (!workspaceId || parameter.workspace_id !== workspaceId || mutationPending) return;
    const requestWorkspace = workspaceId;
    const requestGeneration = recordsGeneration.current;
    setMutationPending(true);
    setMutationNotice(null);
    try {
      await request();
      if (!acceptsWorkspaceResponse(requestGeneration, requestWorkspace, recordsGeneration.current, currentWorkspace.current)) return;
      setMutationNotice({ kind: "success", text: successText });
      loadRecords(requestWorkspace, showParameterHistory);
    } catch (error) {
      if (!acceptsWorkspaceResponse(requestGeneration, requestWorkspace, recordsGeneration.current, currentWorkspace.current)) return;
      setMutationNotice({ kind: "error", text: mutationMessage(error) });
      loadRecords(requestWorkspace, showParameterHistory);
    } finally {
      if (currentWorkspace.current === requestWorkspace) setMutationPending(false);
    }
  };

  const editParameter = (parameter: CanonicalParameter, edits: ParameterEditInput) => applyParameterMutation(
    parameter,
    () => updateCanonicalParameter(parameter, edits),
    `${parameter.name} was updated in canonical Engineering Data.`,
  );

  const transitionParameter = (parameter: CanonicalParameter, action: ParameterLifecycleAction) => {
    const consequence = lifecycleConsequence(action);
    if (!window.confirm(`${action[0].toUpperCase()}${action.slice(1)} ${parameter.name}? This will ${consequence}.`)) return;
    void applyParameterMutation(
      parameter,
      () => transitionCanonicalParameter(parameter, action),
      `${parameter.name} is now ${action === "activate" ? "Active" : action === "deactivate" ? "Inactive" : action === "archive" ? "Archived" : "Deleted"}.`,
    );
  };

  if (workspaceState === "loading") return <section className="engineering-data-empty"><h1>Engineering Data</h1><p>Loading workspaces…</p></section>;
  if (workspaceState === "error") return <section className="engineering-data-empty"><h1>Engineering Data</h1><p>Workspace discovery failed: {workspaceError}</p><button type="button" onClick={loadWorkspaces}>Retry</button></section>;
  if (workspaces.length === 0) return <section className="engineering-data-empty"><h1>Engineering Data</h1><p>No workspaces are available.</p></section>;

  return (
    <section className="engineering-data" aria-labelledby="engineering-data-title">
      <header className="engineering-data__toolbar">
        <div><p className="eyebrow">Canonical records</p><h1 id="engineering-data-title">Engineering Data</h1></div>        <button type="button" disabled={!workspaceId || anyLoading || mutationPending} onClick={() => workspaceId && loadRecords(workspaceId, showParameterHistory)}>Refresh</button>
      </header>

      <div className="engineering-data__controls">
        <label className="engineering-data__search">Search<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Canonical record fields" /></label>
        <fieldset><legend>Record kinds</legend>{ENGINEERING_KINDS.map((kind) => <label key={kind}><input type="checkbox" checked={enabledKinds.includes(kind)} onChange={() => toggleKind(kind)} />{KIND_LABEL[kind]}</label>)}</fieldset>
        <label className="engineering-data__history"><input type="checkbox" checked={showParameterHistory} onChange={(event) => setShowParameterHistory(event.target.checked)} />Advanced/Audit: show noncurrent Parameters</label>
      </div>

      <KindFailures rows={[
        ["Model specs", modelSpecsState, modelSpecsError],
        ["Assumptions", assumptionsState, assumptionsError],
        ["Parameters", parametersState, parametersError],
        ["Decisions", decisionsState, decisionsError],
      ]} />
      {sourceTargetMessage ? <div className="engineering-data__failures" role="status"><p><strong>Linked source unavailable.</strong> {sourceTargetMessage}</p></div> : null}
      {mutationNotice ? <div className={mutationNotice.kind === "error" ? "engineering-data__mutation is-error" : "engineering-data__mutation"} role={mutationNotice.kind === "error" ? "alert" : "status"}><p>{mutationNotice.text}</p></div> : null}

      <div className="engineering-data__grid">
        <div className="engineering-data__list" aria-label="Engineering records">
          {anyLoading && projected.length === 0 && <p>Loading engineering records…</p>}
          {!anyLoading && projected.length === 0 && ![modelSpecsState, assumptionsState, parametersState, decisionsState].includes("error") && <p>No supported engineering records</p>}
          {projected.length > 0 && visible.length === 0 && <p>No records match the current search and kind filters.</p>}
          {visible.map((row) => <button type="button" key={recordKey(row)} className={recordKey(row) === selectedKey ? "engineering-record is-selected" : "engineering-record"} aria-pressed={recordKey(row) === selectedKey} onClick={() => setSelectedKey(recordKey(row))}><span className="engineering-record__kind">{KIND_LABEL[row.kind]}</span><span className="engineering-record__body"><strong>{shown(row.primary)}</strong><small>{shown(row.secondary)}</small></span><span className="engineering-record__status">{row.kind === "parameter" ? (LIFECYCLE_LABEL[row.status] ?? row.status) : row.status}</span></button>)}
        </div>
        <aside className="engineering-data__inspector" aria-label="Selected engineering record">
          {selected ? <Inspector record={selected} mutationPending={mutationPending} onEdit={editParameter} onTransition={transitionParameter} navigate={navigate} /> : <p>Select a visible engineering record.</p>}
        </aside>
      </div>

      <nav className="engineering-data__links" aria-label="Related engineering surfaces">
        <AppLink href="/runs" navigate={navigate}>Open runs</AppLink>
      </nav>
    </section>
  );
}

function KindFailures({ rows }: { rows: Array<[string, LoadState, string | null]> }) {
  const failures = rows.filter(([, state]) => state === "error");
  if (failures.length === 0) return null;
  return <div className="engineering-data__failures" aria-label="Partial data failures">{failures.map(([label, , error]) => <p key={label}><strong>{label} unavailable.</strong> {error}</p>)}</div>;
}

function Inspector({ record, mutationPending, onEdit, onTransition, navigate }: { record: EngineeringRecordProjection; mutationPending: boolean; onEdit(parameter: CanonicalParameter, edits: ParameterEditInput): void; onTransition(parameter: CanonicalParameter, action: ParameterLifecycleAction): void; navigate: Navigate }) {
  return <div className="engineering-inspector"><header><p className="eyebrow">{KIND_LABEL[record.kind]}</p><h2>{shown(record.primary)}</h2><p className="technical-token">{record.id}</p></header><dl><Fact label={record.kind === "parameter" ? "Lifecycle" : "Status"} value={record.kind === "parameter" ? (LIFECYCLE_LABEL[record.record.lifecycle_state] ?? record.record.lifecycle_state) : record.status} /><Fact label="Workspace id" value={record.workspaceId} /><Fact label="Freshness" value="Unavailable" />{record.kind === "model-spec" && <><Fact label="Engineering question" value={record.record.engineering_question} /><Fact label="Scope" value={record.record.scope} /><Fact label="Maturity status" value={record.record.maturity_status} /><Fact label="Schema version" value={record.record.schema_version} /><Fact label="Created" value={record.record.created_at} /><Fact label="Updated" value={record.record.updated_at} /></>}{record.kind === "assumption" && <><Fact label="Statement" value={record.record.statement} /><Fact label="Confidence (persisted)" value={record.record.confidence} /></>}{record.kind === "parameter" && <><Fact label="Proposal status" value={record.record.status} /><Fact label="Value status" value={record.record.value_status} /><Fact label="Name" value={record.record.name} /><Fact label="Symbol" value={record.record.symbol} /><Fact label="Value (canonical)" value={record.record.value} /><Fact label="Unit" value={record.record.unit} /><Fact label="Source" value={record.record.source_ref} /><Fact label="Updated" value={record.record.updated_at} /></>}{record.kind === "decision" && <><Fact label="Title" value={record.record.title} /><Fact label="Decision" value={record.record.decision_text} /></>}</dl>{record.kind === "parameter" ? <ParameterActions parameter={record.record as CanonicalParameter} mutationPending={mutationPending} onEdit={onEdit} onTransition={onTransition} navigate={navigate} /> : <p className="engineering-data__readonly">Lifecycle/edit authority for this record kind is not part of 098 V0.</p>}</div>;
}

function ParameterActions({ parameter, mutationPending, onEdit, onTransition, navigate }: { parameter: CanonicalParameter; mutationPending: boolean; onEdit(parameter: CanonicalParameter, edits: ParameterEditInput): void; onTransition(parameter: CanonicalParameter, action: ParameterLifecycleAction): void; navigate: Navigate }) {
  const [editing, setEditing] = useState(false);
  const lifecycle = parameter.lifecycle_state;
  const canEdit = lifecycle === "active";
  const actions: ParameterLifecycleAction[] = lifecycle === "active"
    ? ["deactivate", "archive", "delete"]
    : lifecycle === "inactive"
      ? ["activate", "archive", "delete"]
      : lifecycle === "archived"
        ? ["delete"]
        : [];

  return <section className="engineering-data__parameter-actions" aria-label="Canonical Parameter actions">
    <header><h3>Canonical Parameter actions</h3><p>These actions change project Engineering Data on the server. They do not edit only the current working configuration.</p></header>
    <div className="engineering-data__action-row">
      {canEdit ? <button type="button" disabled={mutationPending} onClick={() => setEditing((current) => !current)}>{editing ? "Cancel edit" : "Edit canonical Parameter"}</button> : null}
      {actions.map((action) => <button type="button" key={action} disabled={mutationPending} onClick={() => onTransition(parameter, action)}>{action === "activate" ? "Activate" : action === "deactivate" ? "Deactivate" : action === "archive" ? "Archive" : "Delete"}</button>)}
    </div>
    {editing ? <ParameterEditForm key={parameter.updated_at} parameter={parameter} disabled={mutationPending} onCancel={() => setEditing(false)} onSubmit={(edits) => { setEditing(false); onEdit(parameter, edits); }} /> : null}
    {lifecycle === "active" || lifecycle === "inactive" ? <p className="engineering-data__readonly"><strong>Supersede:</strong> replacement promotion stays in the existing Parameter proposal/review authority. <AppLink href="/review" navigate={navigate}>Open replacement review</AppLink></p> : null}
  </section>;
}

function optionalNumber(value: string, label: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${label} must be a finite number or left blank.`);
  return parsed;
}

function ParameterEditForm({ parameter, disabled, onCancel, onSubmit }: { parameter: CanonicalParameter; disabled: boolean; onCancel(): void; onSubmit(edits: ParameterEditInput): void }) {
  const [name, setName] = useState(parameter.name);
  const [symbol, setSymbol] = useState(parameter.symbol ?? "");
  const [value, setValue] = useState(parameter.value ?? "");
  const [unit, setUnit] = useState(parameter.unit);
  const [valueStatus, setValueStatus] = useState<CanonicalParameter["value_status"]>(parameter.value_status);
  const [valueMin, setValueMin] = useState(parameter.value_min?.toString() ?? "");
  const [valueMax, setValueMax] = useState(parameter.value_max?.toString() ?? "");
  const [sourceRef, setSourceRef] = useState(parameter.source_ref ?? "");
  const [confidence, setConfidence] = useState(parameter.confidence?.toString() ?? "");
  const [notes, setNotes] = useState(parameter.notes ?? "");
  const [validationError, setValidationError] = useState<string | null>(null);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!name.trim() || !unit.trim()) return;
    try {
      const parsedMin = optionalNumber(valueMin, "Minimum");
      const parsedMax = optionalNumber(valueMax, "Maximum");
      const parsedConfidence = optionalNumber(confidence, "Confidence");
      if (parsedMin !== null && parsedMax !== null && parsedMin > parsedMax) {
        throw new Error("Minimum must be less than or equal to Maximum.");
      }
      setValidationError(null);
      onSubmit({
        name: name.trim(),
        symbol: symbol.trim() || null,
        value: value.trim() || null,
        unit: unit.trim(),
        value_status: valueStatus,
        value_min: parsedMin,
        value_max: parsedMax,
        source_ref: sourceRef.trim() || null,
        confidence: parsedConfidence,
        notes: notes.trim() || null,
      });
    } catch (error) {
      setValidationError(error instanceof Error ? error.message : "Numeric fields are invalid.");
    }
  };

  return <form className="engineering-data__edit-form" onSubmit={submit}>
    <p><strong>Canonical edit.</strong> Saving changes the project Parameter after a server-side compare-and-swap check. A stale edit is rejected and refreshed; it never silently overwrites newer server truth.</p>
    {validationError ? <p role="alert"><strong>Check numeric fields.</strong> {validationError}</p> : null}
    <div className="engineering-data__edit-grid">
      <label>Name<input required value={name} onChange={(event) => setName(event.target.value)} /></label>
      <label>Symbol<input value={symbol} onChange={(event) => setSymbol(event.target.value)} /></label>
      <label>Value<input value={value} onChange={(event) => setValue(event.target.value)} /></label>
      <label>Unit<input required value={unit} onChange={(event) => setUnit(event.target.value)} /></label>
      <label>Value status<select value={valueStatus} onChange={(event) => setValueStatus(event.target.value as CanonicalParameter["value_status"])}><option value="candidate">Candidate</option><option value="literature">Literature</option><option value="measured">Measured</option><option value="validated">Validated</option><option value="accepted">Accepted</option></select></label>
      <label>Minimum<input inputMode="decimal" value={valueMin} onChange={(event) => { setValueMin(event.target.value); setValidationError(null); }} /></label>
      <label>Maximum<input inputMode="decimal" value={valueMax} onChange={(event) => { setValueMax(event.target.value); setValidationError(null); }} /></label>
      <label>Confidence<input inputMode="decimal" value={confidence} onChange={(event) => { setConfidence(event.target.value); setValidationError(null); }} /></label>
      <label className="is-wide">Source reference<input value={sourceRef} onChange={(event) => setSourceRef(event.target.value)} /></label>
      <label className="is-wide">Notes<textarea rows={3} value={notes} onChange={(event) => setNotes(event.target.value)} /></label>
    </div>
    <div className="engineering-data__action-row"><button type="submit" disabled={disabled || !name.trim() || !unit.trim()}>Save canonical Parameter</button><button type="button" disabled={disabled} onClick={onCancel}>Cancel</button></div>
  </form>;
}

function Fact({ label, value }: { label: string; value: string | number | null | undefined }) {
  return <div><dt>{label}</dt><dd>{shown(value)}</dd></div>;
}

export default EngineeringData;
