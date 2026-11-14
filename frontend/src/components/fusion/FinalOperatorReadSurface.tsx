import { useEffect, useMemo, useState } from "react";

import {
  getSystemInfo as readFinalSystemInfo,
  listAssumptions as listFinalAssumptions,
  listDecisions as listFinalDecisions,
  listModelSpecs as listFinalModelSpecs,
  listParameters as listFinalParameters,
  listRequirements as listFinalRequirements,
  listWorkspaces as listFinalWorkspaces,
  type Assumption as FinalAssumption,
  type Decision as FinalDecision,
  type ModelSpec as FinalModelSpec,
  type Parameter as FinalParameter,
  type Requirement as FinalRequirement,
  type SystemInfoResponse,
  type Workspace as FinalWorkspace
} from "../../api/client";

import ProjectKnowledgePanel from "./ProjectKnowledgePanel";
import ValueHistoryModal, { type HistoryTarget } from "./ValueHistoryModal";

type ReadKind = "project-basis" | "models" | "runtime";

type Props = Readonly<{
  kind: ReadKind;
  readOnly?: boolean;
  workspaceId: string | null;
  onWorkspaceChange: (workspaceId: string) => void;
  projectSearch?: React.ReactNode;
  jarvis?: React.ReactNode;
  onRecordSelect?: (ref: string | null, label?: string) => void;
  requestedRecordRef?: string | null;
}>;

type WorkspaceRecords = Readonly<{
  requirements: FinalRequirement[];
  parameters: FinalParameter[];
  assumptions: FinalAssumption[];
  decisions: FinalDecision[];
  modelSpecs: FinalModelSpec[];
}>;

const emptyRecords: WorkspaceRecords = { requirements: [], parameters: [], assumptions: [], decisions: [], modelSpecs: [] };

function Panel({ title, children, className = "", status }: Readonly<{ title: string; children: React.ReactNode; className?: string; status?: string }>) {
  return <section className={`final-fusion__panel ${className}`.trim()} aria-label={title}><header className="final-fusion__panel-head"><h2>{title}</h2>{status && <span>{status}</span>}</header>{children}</section>;
}

function JarvisReadBoundary({ detail }: Readonly<{ detail: string }>) {
  return <Panel title="Jarvis" className="final-fusion__jarvis" status="Read context only"><div className="final-fusion__jarvis-body"><div className="final-fusion__context-note">Browsing does not add records to Jarvis context. Explicit context insertion remains separately governed.</div><div className="final-fusion__bubble">{detail}</div><div className="final-fusion__composer" aria-disabled="true"><span>Ask Jarvis about an exact selected record…</span><button type="button" disabled>Send</button></div></div></Panel>;
}

function ProjectBasis({ readOnly, records, workspaces, workspaceId, onWorkspaceChange, loading, error, searchPanel, requestedRecordRef, jarvis, onRecordSelect, onChanged }: Readonly<{ readOnly: boolean; records: WorkspaceRecords; workspaces: FinalWorkspace[]; workspaceId: string | null; onWorkspaceChange: (id: string) => void; loading: boolean; error: string | null; searchPanel?: React.ReactNode; requestedRecordRef?: string | null; jarvis?: React.ReactNode; onRecordSelect?: (ref: string | null, label?: string) => void; onChanged: () => void }>) {
  const [editing, setEditing] = useState<FinalRequirement | null>(null);
  const [activeHistoryTarget, setActiveHistoryTarget] = useState<HistoryTarget | null>(null);
  useEffect(() => { setEditing(null); }, [workspaceId]);
  useEffect(() => { setActiveHistoryTarget(null); }, [workspaceId]);
  const [open, setOpen] = useState<Set<string>>(new Set());
  useEffect(() => { setOpen(new Set(requestedRecordRef ? [requestedRecordRef] : [])); }, [workspaceId, requestedRecordRef]);
  const groups = [
    { title: "Requirements & acceptance criteria", rows: records.requirements.map(item => ({ id: `requirement:${item.id}`, title: item.statement, status: item.status, data: item })) },
    { title: "Parameters & boundary conditions", rows: records.parameters.map(item => ({ id: `parameter:${item.id}`, title: item.name, status: `${item.value ?? "Not set"} ${item.unit ?? ""}`, data: item })) },
    { title: "Assumptions", rows: records.assumptions.map(item => ({ id: `assumption:${item.id}`, title: item.statement, status: item.status, data: item })) },
    { title: "Decisions", rows: records.decisions.map(item => ({ id: `decision:${item.id}`, title: item.title, status: item.status, data: item })) }
  ];
  const toggle = (id: string, label: string) => {
    setOpen(current => { const next = new Set(current); next.has(id) ? next.delete(id) : next.add(id); return next; });
    onRecordSelect?.(id, label);
  };
  return <div className="final-fusion__workbench final-fusion__workbench--memory">
    {searchPanel}
    <Panel title="Project Basis" className="final-fusion__basis" status="Current project knowledge">
      <div className="basis-records">
        {loading ? <p role="status">Loading project records…</p> : error ? <p role="alert">{error}</p> : <>
          {!groups.some(group => group.rows.length) && <p>No current basis records. Add a requirement below to start this project.</p>}
          {groups.map(group => <section key={group.title} className="basis-group">
            <h3>{group.title} <span>{group.rows.length}</span></h3>
            {group.rows.map(row => <article key={row.id} data-search-ref={row.id} data-search-selected={row.id === requestedRecordRef ? "true" : undefined}>
              <button className="basis-record secondary-button" aria-expanded={open.has(row.id)} onClick={() => toggle(row.id, row.title)}>
                <span aria-hidden="true">{open.has(row.id) ? "⌄" : "›"}</span><strong>{row.title}</strong><em>{row.status}</em>
              </button>
              {open.has(row.id) && <div className="basis-record-detail">
                <dl>{Object.entries(row.data).filter(([key, value]) => value !== null && value !== "" && !["id", "workspace_id", "created_at", "updated_at", "statement", "name", "title"].includes(key) && !/digest|token|schema|_id$/.test(key)).map(([key,value]) => <div key={key}><dt>{key.replace(/_/g, " ")}</dt><dd>{typeof value === "object" ? JSON.stringify(value) : String(value)}</dd></div>)}</dl>
                {!readOnly && row.id.startsWith("requirement:") && <button type="button" onClick={() => setEditing(row.data as FinalRequirement)}>Edit</button>}
                {row.id.startsWith("parameter:") && <button type="button" onClick={() => setActiveHistoryTarget({ kind: "parameter", id: (row.data as FinalParameter).id, name: (row.data as FinalParameter).name })}>History</button>}
                <p>Select this record in Jarvis to prepare a proposal with its exact context.</p>
                <details><summary>Technical details</summary><pre>{JSON.stringify(row.data, null, 2)}</pre></details>
              </div>}
            </article>)}
          </section>)}
        </>}
      </div>
      <ProjectKnowledgePanel key={workspaceId} workspaceId={workspaceId} readOnly={readOnly} onChanged={onChanged} editing={editing} onCancelEdit={() => setEditing(null)} />
      {activeHistoryTarget && workspaceId && (
        <ValueHistoryModal
          workspaceId={workspaceId}
          target={activeHistoryTarget}
          onClose={() => setActiveHistoryTarget(null)}
        />
      )}
    </Panel>
    <section className="final-fusion__panel final-fusion__jarvis" aria-label="Jarvis">{jarvis}</section>
  </div>;
}

function Models({ records, workspaces, workspaceId, onWorkspaceChange, loading, error }: Readonly<{ records: WorkspaceRecords; workspaces: FinalWorkspace[]; workspaceId: string | null; onWorkspaceChange: (id: string) => void; loading: boolean; error: string | null }>) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = records.modelSpecs.find((item) => item.id === selectedId) ?? records.modelSpecs[0] ?? null;
  useEffect(() => { if (selected && selected.id !== selectedId) setSelectedId(selected.id); }, [selected, selectedId]);
  return <div className="final-fusion__workbench final-fusion__workbench--models"><Panel title="Model versions" className="final-fusion__versions" status="Model specs only"><div className="final-fusion__searchbox">Exact model-version inventory is not exposed by the current read owner.</div>{loading ? <div className="final-fusion__source-empty"><strong>Loading model specifications…</strong></div> : error ? <div className="final-fusion__source-empty"><strong>Backend read failed</strong><span>{error}</span></div> : records.modelSpecs.length ? <div className="final-fusion__source-list">{records.modelSpecs.map((item) => <button type="button" className="final-fusion__disclosure-row" key={item.id} onClick={() => setSelectedId(item.id)} aria-pressed={selected?.id === item.id}><span>›</span><strong>{item.title}</strong><em>{item.status} · schema {item.schema_version}</em></button>)}</div> : <div className="final-fusion__source-empty"><strong>No model specifications</strong></div>}<div className="final-fusion__lineage-slot">Exact version lineage · Unknown</div></Panel><Panel title="Version dossier" className="final-fusion__model-dossier" status={selected ? "Model spec READ" : "Unavailable"}><div className="final-fusion__dossier-top"><strong>{selected?.title ?? "Exact model / version"}</strong><span>{selected ? "Specification record; not an exact model version" : "Unknown"}</span></div><div className="final-fusion__summary-strip"><span>Identity · {selected?.id ?? "Unknown"}</span><span>Status · {selected?.status ?? "Unknown"}</span><span>Maturity · {selected?.maturity_status ?? "Unknown"}</span></div><div className="final-fusion__dossier-grid"><section><header><strong>Definition</strong><span>{selected ? "READ" : "Unavailable"}</span></header><p>{selected?.engineering_question ?? "No exact-version definition is available."}</p>{selected?.scope && <p>{selected.scope}</p>}</section>{["Assumptions", "Methods & Equations", "Parameters & Inputs", "Process", "BlueCad", "Results & Validation", "Criticalities", "Sources", "Artifacts", "Runs", "Changelog / Lineage"].map((label) => <section key={label}><header><strong>{label}</strong><span>Exact-version owner unavailable</span></header><p>Not projected from workspace-level records because exact model/version binding cannot be proven.</p></section>)}</div><div className="final-fusion__context-strip">Workspace records are not promoted into exact-version evidence without an explicit binding.</div></Panel><JarvisReadBoundary detail="The selected model specification can be read. Exact version, runs, artifacts and evidence remain Unknown until an accepted version-bound read owner exists." /></div>;
}

function Runtime({ system, loading, error }: Readonly<{ system: SystemInfoResponse | null; loading: boolean; error: string | null }>) {
  return <div className="final-fusion__workbench final-fusion__workbench--coding"><Panel title="Runtime" className="final-fusion__runtime-main" status="Observed system facts"><div className="final-fusion__repo-status"><div><strong>JarvisOS runtime identity</strong><span>Exact executed SHA vs remote SHA observer unavailable</span></div><span className="final-fusion__unknown">Unknown</span></div><div className="final-fusion__runtime-body"><section className="final-fusion__compare"><div className="final-fusion__version-card"><small>Local current · actually executed</small><strong>LOCAL · Unknown SHA</strong><code>{system?.environment ?? "Environment unknown"}</code><p>{system ? `${system.app_name} ${system.version} is reporting through /system/info, but that does not prove an executed Git SHA.` : "No exact local code identity is proven."}</p></div><div className="final-fusion__delta">→<span>Unknown</span></div><div className="final-fusion__version-card is-remote"><small>GitHub latest · remote exact</small><strong>REMOTE · Unknown</strong><code>Repository observer unavailable</code><p>The frontend does not call GitHub directly or infer alignment.</p></div></section><section className="final-fusion__runtime-services"><Panel title="Observed backend" status={loading ? "Loading" : error ? "Read error" : system?.database.ready ? "Database ready" : "Unknown"}>{error ? <div className="final-fusion__empty">{error}</div> : <div className="final-fusion__facts"><div>Application · {system?.app_name ?? "Unknown"}</div><div>Version · {system?.version ?? "Unknown"}</div><div>Environment · {system?.environment ?? "Unknown"}</div><div>Database initialized · {system ? String(system.database.initialized) : "Unknown"}</div></div>}</Panel><Panel title="Update / terminal" status="Unavailable"><div className="final-fusion__empty">Safe update and terminal EXECUTE authority are not present in 100f.</div></Panel></section></div></Panel><div className="final-fusion__rightstack"><JarvisReadBoundary detail="Observed /system/info facts may be discussed, but local executed SHA, remote exact SHA and alignment remain Unknown." /><Panel title="Runtime facts" className="final-fusion__facts" status="READ"><div className="final-fusion__empty">System observations are backend-provided. No GitHub or process authority exists in the browser.</div></Panel></div></div>;
}

export default function FinalOperatorReadSurface({ kind, readOnly = false, workspaceId, onWorkspaceChange, projectSearch, requestedRecordRef, jarvis, onRecordSelect }: Props) {
  const [reloadNonce, setReloadNonce] = useState(0);
  const [workspaces, setWorkspaces] = useState<FinalWorkspace[]>([]);
  const [records, setRecords] = useState<WorkspaceRecords>(emptyRecords);
  const [system, setSystem] = useState<SystemInfoResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const activeWorkspaceId = useMemo(() => workspaceId ?? workspaces[0]?.id ?? null, [workspaceId, workspaces]);

  useEffect(() => {
    let alive = true;
    if (kind === "runtime") {
      setLoading(true); setError(null);
      void readFinalSystemInfo().then(value => { if (alive) setSystem(value); }).catch((cause: unknown) => { if (alive) setError(cause instanceof Error ? cause.message : "Runtime read failed"); }).finally(() => { if (alive) setLoading(false); });
      return () => { alive = false; };
    }
    setLoading(true); setError(null);
    void listFinalWorkspaces().then((items) => {
      if (!alive) return;
      setWorkspaces(items);
      if (!workspaceId && items[0]) onWorkspaceChange(items[0].id);
    }).catch((cause: unknown) => { if (alive) setError(cause instanceof Error ? cause.message : "Workspace read failed"); });
    return () => { alive = false; };
  }, [kind, onWorkspaceChange, workspaceId]);

  useEffect(() => {
    if (kind === "runtime" || !activeWorkspaceId) return;
    let alive = true;
    setRecords(emptyRecords);
    setLoading(true); setError(null);
    const calls = kind === "models"
      ? Promise.all([Promise.resolve([] as FinalRequirement[]), Promise.resolve([] as FinalParameter[]), Promise.resolve([] as FinalAssumption[]), Promise.resolve([] as FinalDecision[]), listFinalModelSpecs(activeWorkspaceId)])
      : Promise.all([listFinalRequirements(activeWorkspaceId), listFinalParameters(activeWorkspaceId), listFinalAssumptions(activeWorkspaceId), listFinalDecisions(activeWorkspaceId), Promise.resolve([] as FinalModelSpec[])]);
    void calls.then(([requirements, parameters, assumptions, decisions, modelSpecs]) => { if (alive) setRecords({ requirements, parameters, assumptions, decisions, modelSpecs }); }).catch((cause: unknown) => { if (alive) setError(cause instanceof Error ? cause.message : "Workspace record read failed"); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [activeWorkspaceId, kind, reloadNonce]);

  if (kind === "runtime") return <Runtime system={system} loading={loading} error={error} />;
  if (kind === "models") return <Models records={records} workspaces={workspaces} workspaceId={activeWorkspaceId} onWorkspaceChange={onWorkspaceChange} loading={loading} error={error} />;
  return <ProjectBasis readOnly={readOnly} records={records} workspaces={workspaces} workspaceId={activeWorkspaceId} onWorkspaceChange={onWorkspaceChange} loading={loading} error={error} searchPanel={projectSearch} requestedRecordRef={requestedRecordRef} jarvis={jarvis} onRecordSelect={onRecordSelect} onChanged={() => setReloadNonce(n => n + 1)} />;
}
