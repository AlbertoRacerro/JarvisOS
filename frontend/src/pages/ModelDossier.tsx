import { useEffect, useMemo, useRef, useState } from "react";

import "./memory-recovery.css";

import { createModelSpec, listWorkspaces, type Workspace } from "../api/client";
import {
  getModelDossier,
  listModelDossiers,
  type ModelDossierDetail,
  type ModelDossierIndexItem
} from "../api/modelDossier";

type Props = Readonly<{
  jarvis?: React.ReactNode;
  revisionPanel?: React.ReactNode;
  workspaceId: string | null;
  onWorkspaceChange: (workspaceId: string) => void;
  requestedModelVersionId?: string | null;
  onModelVersionSelectionChange?: (modelVersionId: string | null, label?: string) => void;
}>;

const plainDisclosureRowStyle = { gridTemplateColumns: "minmax(0, 1fr) auto" } as const;

function normalizedSearch(value: string): string {
  return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase().trim();
}

function matchesSearch(value: string, query: string): boolean {
  const terms = normalizedSearch(query).split(/\s+/).filter(Boolean);
  const haystack = normalizedSearch(value);
  return terms.every((term) => haystack.includes(term));
}

function Empty({ children }: Readonly<{ children: React.ReactNode }>) {
  return <div className="final-fusion__source-empty">{children}</div>;
}

function Value({ label, value }: Readonly<{ label: string; value?: string | null }>) {
  return <div className="final-fusion__disclosure-row" style={plainDisclosureRowStyle}><strong>{label}</strong><em>{value || "Unknown"}</em></div>;
}

function TechnicalDetails({ children }: Readonly<{ children: React.ReactNode }>) {
  return <details><summary>Technical details</summary>{children}</details>;
}

export default function ModelDossier({ jarvis, revisionPanel, workspaceId, onWorkspaceChange, requestedModelVersionId = null, onModelVersionSelectionChange }: Props) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [query, setQuery] = useState("");
  const [selectedSpecId, setSelectedSpecId] = useState<string | null>(null);
  const [index, setIndex] = useState<ModelDossierIndexItem[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [activeRequestedVersionId, setActiveRequestedVersionId] = useState<string | null>(requestedModelVersionId);
  const [detail, setDetail] = useState<ModelDossierDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createTitle, setCreateTitle] = useState("");
  const [createQuestion, setCreateQuestion] = useState("");
  const [createScope, setCreateScope] = useState("");
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const componentAlive = useRef(true);
  const creationSequence = useRef(0);

  useEffect(() => {
    componentAlive.current = true;
    return () => { componentAlive.current = false; };
  }, []);

  const activeWorkspaceId = useMemo(() => workspaceId ?? workspaces[0]?.id ?? null, [workspaceId, workspaces]);
  const activeWorkspaceToken = useRef(activeWorkspaceId);
  activeWorkspaceToken.current = activeWorkspaceId;
  const versions = useMemo(() => index.flatMap((item) => item.versions.map((version) => ({ item, version }))), [index]);
  const requestedSelectionUnavailable = Boolean(requestedModelVersionId && !loading && !error && !versions.some(({ version }) => version.model_version_id === requestedModelVersionId));

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    void listWorkspaces().then((items) => {
      if (!alive) return;
      setWorkspaces(items);
      if (!workspaceId && items[0]) onWorkspaceChange(items[0].id);
    }).catch((cause: unknown) => {
      if (alive) setError(cause instanceof Error ? cause.message : "Workspace read failed");
    }).finally(() => {
      if (alive) setLoading(false);
    });
    return () => { alive = false; };
  }, [onWorkspaceChange, workspaceId]);

  useEffect(() => {
    creationSequence.current += 1;
    setCreating(false); setCreateError(null); setNotice(null);
    onModelVersionSelectionChange?.(null);
    setIndex([]); setSelectedSpecId(null); setSelectedVersionId(null); setQuery("");
    setActiveRequestedVersionId(requestedModelVersionId);
    if (!activeWorkspaceId) {
      setIndex([]);
      setSelectedVersionId(null);
      setDetail(null);
      return;
    }
    let alive = true;
    setLoading(true);
    setError(null);
    setDetail(null);
    void listModelDossiers(activeWorkspaceId).then((items) => {
      if (!alive) return;
      setIndex(items);
      const allVersions = items.flatMap((item) => item.versions);
      const firstVersion = allVersions[0]?.model_version_id ?? null;
      if (requestedModelVersionId) {
        setSelectedVersionId(allVersions.some((version) => version.model_version_id === requestedModelVersionId) ? requestedModelVersionId : null);
        return;
      }
      setSelectedVersionId(firstVersion);
      if (!firstVersion) setSelectedSpecId(items[0]?.model_spec_id ?? null);
    }).catch((cause: unknown) => {
      if (alive) setError(cause instanceof Error ? cause.message : "Model dossier index read failed");
    }).finally(() => {
      if (alive) setLoading(false);
    });
    return () => { alive = false; };
  }, [activeWorkspaceId, onModelVersionSelectionChange, requestedModelVersionId]);

  useEffect(() => {
    onModelVersionSelectionChange?.(null);
    setDetail(null);
    if (!activeWorkspaceId || !selectedVersionId) return;
    if (activeRequestedVersionId && activeRequestedVersionId !== selectedVersionId) return;

    let alive = true;
    setLoading(true);
    setError(null);
    void getModelDossier(activeWorkspaceId, selectedVersionId).then((value) => {
      if (!alive) return;
      setDetail(value);
      onModelVersionSelectionChange?.(value.identity.model_version_id, `${value.title} · ${value.identity.version_label || "Unlabelled version"}`);
    }).catch((cause: unknown) => {
      if (alive) setError(cause instanceof Error ? cause.message : "Exact model-version dossier read failed");
    }).finally(() => {
      if (alive) setLoading(false);
    });
    return () => { alive = false; };
  }, [activeRequestedVersionId, activeWorkspaceId, onModelVersionSelectionChange, selectedVersionId]);

  const selectVersion = (modelVersionId: string) => {
    setActiveRequestedVersionId(null);
    setSelectedSpecId(null);
    setSelectedVersionId(modelVersionId);
  };

  const createDefinition = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!activeWorkspaceId || creating || !createTitle.trim() || !createQuestion.trim()) return;
    const ws = activeWorkspaceId;
    const sequence = ++creationSequence.current;
    setCreating(true); setCreateError(null); setNotice(null);
    try {
      const created = await createModelSpec(ws, { title: createTitle.trim(), engineering_question: createQuestion.trim(), scope: createScope.trim() || null, status: "draft", maturity_status: "draft" });
      if (!componentAlive.current || sequence !== creationSequence.current || activeWorkspaceToken.current !== ws) return;
      setNotice(`Saved draft definition “${created.title}”. Add a version from the modeling workflow when its implementation is ready.`);
      setCreateTitle(""); setCreateQuestion(""); setCreateScope(""); setCreateOpen(false);
      try {
        const refreshed = await listModelDossiers(ws);
        if (!componentAlive.current || sequence !== creationSequence.current || activeWorkspaceToken.current !== ws) return;
        setIndex(refreshed); setSelectedSpecId(created.id); setSelectedVersionId(null); setActiveRequestedVersionId(null);
      } catch (cause) {
        if (componentAlive.current && sequence === creationSequence.current && activeWorkspaceToken.current === ws) setCreateError(`Saved draft definition “${created.title}”, but the model list could not be refreshed. Reload this workspace to see it${cause instanceof Error ? ` (${cause.message})` : ""}.`);
      }
    } catch (cause) {
      if (componentAlive.current && sequence === creationSequence.current) setCreateError(`Model definition could not be saved${cause instanceof Error ? `: ${cause.message}` : "."}`);
    } finally { if (componentAlive.current && sequence === creationSequence.current) setCreating(false); }
  };

  const selectedSpec = index.find((item) => item.model_spec_id === selectedSpecId);
  const filteredVersions = versions.filter(({ item, version }) => matchesSearch(`${item.title} ${version.version_label ?? ""} ${version.status ?? ""}`, query));
  const versionless = index.filter((item) => !item.versions.length && matchesSearch(`${item.title} ${item.engineering_question}`, query));

  return <div className="final-fusion__workbench final-fusion__workbench--models models-recovery">
    <section className="final-fusion__panel final-fusion__versions" aria-label="Model versions">
      <header className="final-fusion__panel-head"><div><h2>Models & versions</h2><span>{index.length} models</span></div><button type="button" disabled={!activeWorkspaceId} onClick={() => setCreateOpen((value) => !value)} aria-expanded={createOpen}>+ New model</button></header>      <div className="final-fusion__searchbox">Opening a version does not add records to Jarvis context. Use Selected context in Jarvis to include it explicitly.</div>
      {requestedSelectionUnavailable ? <Empty><strong>Requested model version is unavailable.</strong><span>The exact search identity no longer exists in this workspace.</span></Empty> : null}
      <label className="model-filter">Find a model or version<input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Title, version or status…" /></label>
      {notice ? <p className="model-notice" role="status">{notice}</p> : null}
      {createOpen ? <form className="model-create" onSubmit={(event) => void createDefinition(event)}>
        <h3>Start a model definition</h3><p>Capture the engineering question first. This saves a draft definition; it does not create a runnable version.</p>
        <label>Model title<input required maxLength={500} autoFocus value={createTitle} disabled={creating} onChange={(event) => setCreateTitle(event.target.value)} placeholder="e.g. Pipe pressure drop" /></label>
        <label>Engineering question<textarea required maxLength={8000} value={createQuestion} disabled={creating} onChange={(event) => setCreateQuestion(event.target.value)} placeholder="What should this model answer?" /></label>
        <label>Scope (optional)<textarea maxLength={8000} value={createScope} disabled={creating} onChange={(event) => setCreateScope(event.target.value)} placeholder="System boundary, phase or operating case" /></label>
        <button type="submit" disabled={creating || !createTitle.trim() || !createQuestion.trim()}>{creating ? "Saving definition…" : "Save draft definition"}</button><button type="button" disabled={creating} onClick={() => setCreateOpen(false)}>Cancel</button>
      </form> : null}
      {createError ? <p className="model-create-error" role="alert">{createError}</p> : null}
      {loading && !index.length ? <Empty><strong>Loading model dossiers…</strong></Empty> : error && !index.length ? <Empty><strong>Backend read failed</strong><span>{error}</span></Empty> : index.length ? <div className="final-fusion__source-list">
        {filteredVersions.map(({ item, version }) => <button type="button" className="model-version-row" data-model-version-id={version.model_version_id} data-search-selected={selectedVersionId === version.model_version_id ? "true" : undefined} key={version.model_version_id} onClick={() => selectVersion(version.model_version_id)} aria-pressed={selectedVersionId === version.model_version_id}><strong>{item.title}</strong><span>{version.version_label || "Unlabelled version"} · {version.status || "Unknown status"}</span></button>)}
        {versionless.map((item) => <button type="button" className="model-version-row" key={item.model_spec_id} aria-pressed={selectedSpecId === item.model_spec_id} onClick={() => { setActiveRequestedVersionId(null); setSelectedVersionId(null); setSelectedSpecId(item.model_spec_id); }}><strong>{item.title}</strong><span>Definition · no version yet</span></button>)}
        {!filteredVersions.length && !versionless.length ? <p>No matching models or versions.</p> : null}
      </div> : <Empty><strong>No model definitions yet</strong><span>Use <strong>+ New model</strong> above to capture the engineering question and save a draft.</span></Empty>}
      <div className="final-fusion__lineage-slot">{detail ? `Selected · ${detail.title} · ${detail.identity.version_label || "Unlabelled version"}` : selectedSpec ? `${selectedSpec.title} · definition only` : "No model version selected"}</div>
    </section>

    <section className="final-fusion__panel final-fusion__model-dossier" aria-label="Version dossier">
      <header className="final-fusion__panel-head"><h2>Version dossier</h2><span>{detail ? "Canonical READ" : loading ? "Loading" : selectedSpec ? "Definition only" : "No selection"}</span></header>
      {error && <Empty><strong>Version read failed</strong><span>{error}</span></Empty>}
      {!detail ? <div className="model-definition-empty"><h2>{selectedSpec?.title ?? (loading ? "Loading dossier…" : "Select a model")}</h2><p>{selectedSpec?.engineering_question ?? "Choose a model or version on the left to inspect its definition, assumptions, runs and evidence."}</p>{selectedSpec ? <><p>{selectedSpec.scope}</p><p>This model has a saved definition but no version yet. Runs, artifacts and exact version context are unavailable until a version exists.</p></> : null}</div> : null}
      {detail ? <>
      <div className="final-fusion__dossier-top"><strong>{detail.title}</strong><span>{detail.identity.version_label ?? "Unlabelled version"}</span></div>
      <div className="final-fusion__summary-strip"><span>Status · {detail?.identity.status ?? "Unknown"}</span><span>Maturity · {detail?.maturity_status ?? "Unknown"}</span><span>Runs · {detail?.runs.length ?? 0}</span><span>Evidence · {detail?.evidence.length ?? 0}</span></div>
      {detail ? <TechnicalDetails><Value label="Model version ID" value={detail.identity.model_version_id} /><Value label="Model spec ID" value={detail.identity.model_spec_id} /></TechnicalDetails> : null}

      <div className="final-fusion__dossier-grid">
        <section><header><strong>Definition</strong><span>READ</span></header><p>{detail?.engineering_question ?? "Unknown"}</p><p>{detail?.scope ?? "Scope unavailable"}</p></section>
        <section><header><strong>Assumptions</strong><span>Summary</span></header><p>{detail?.assumptions_summary ?? "Unknown"}</p></section>
        <section><header><strong>Parameters & Inputs</strong><span>Summary</span></header><p>{detail?.inputs_summary ?? "Unknown"}</p></section>
        <section><header><strong>Outputs</strong><span>Summary</span></header><p>{detail?.outputs_summary ?? "Unknown"}</p></section>
        <section><header><strong>Runs</strong><span>{detail?.runs.length ?? 0}</span></header>{detail?.runs.length ? detail.runs.map((run) => <div key={run.run_id}><Value label={run.run_label || "Model run"} value={run.status || "Unknown status"} /><TechnicalDetails><Value label="Run ID" value={run.run_id} /><Value label="Project Knowledge revision" value={run.project_knowledge_revision_id} /></TechnicalDetails></div>) : <p>No version-bound runs.</p>}</section>
        <section><header><strong>Artifacts</strong><span>{detail?.artifacts.length ?? 0}</span></header>{detail?.artifacts.length ? detail.artifacts.map((artifact) => <div key={artifact.artifact_id}><Value label={artifact.role || "Artifact"} value={artifact.availability || "Unknown availability"} /><TechnicalDetails><Value label="Artifact ID" value={artifact.artifact_id} /><Value label="Digest" value={artifact.digest} /></TechnicalDetails></div>) : <p>No version-bound artifacts.</p>}</section>
        <section><header><strong>Freshness & Evidence</strong><span>{detail?.evidence.length ?? 0}</span></header>{detail?.evidence.length ? detail.evidence.map((evidence) => <div key={evidence.evidence_id}><Value label={evidence.kind || "Evidence"} value={`${evidence.freshness || "Unknown freshness"} · ${evidence.availability}`} /><TechnicalDetails><Value label="Evidence ID" value={evidence.evidence_id} /></TechnicalDetails></div>) : <p>No explicit evidence records.</p>}</section>
        <section><header><strong>Version lineage</strong><span>Identity</span></header><Value label="Version label" value={detail?.identity.version_label} /><Value label="Implementation" value={detail?.identity.implementation_kind} />{detail ? <TechnicalDetails><Value label="Input contract digest" value={detail.identity.input_contract_digest} /><Value label="Created" value={detail.identity.created_at} /></TechnicalDetails> : null}</section>
      </div>
      </> : null}
      {revisionPanel ? <details className="model-revision-history"><summary>Project knowledge revision history</summary>{revisionPanel}</details> : null}
      <div className="final-fusion__context-strip">Browsing is context-neutral. Add a selected version to Jarvis explicitly when you need to discuss it; draft creation is available from the model list.</div>
    </section>

    <section className="final-fusion__panel final-fusion__jarvis" aria-label="Jarvis">{jarvis}</section>
  </div>;
}
