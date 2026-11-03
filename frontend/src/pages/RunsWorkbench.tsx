import { useEffect, useMemo, useRef, useState } from "react";

import { listWorkspaces, type Workspace } from "../api/client";
import { RunsRequestError, getRun, listRunArtifacts, listRunLogs, listRuns, type RunArtifact, type RunLog, type SimulationRunDetail, type SimulationRunSummary } from "../api/runs";
import { parseSourceRunTarget, type SourceRunTarget } from "../components/analytics/variantComparisonNavigation";
import type { EngineeringPropertiesController } from "../components/engineering/EngineeringProperties";
import { acceptsResponse, chooseSelection, projectPayload, visibleRuns } from "../components/runs/state";

type Props = {
  workspaceId: string | null;
  onWorkspaceChange(next: string | null): void;
  engineeringProperties: EngineeringPropertiesController;
};

type LoadState = "idle" | "loading" | "ready" | "error";
type PreviousRunConfirmation = Readonly<{ runId: string; revision: number }>;

function fmt(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function initialSourceRunTarget(): SourceRunTarget | null {
  return typeof window === "undefined" ? null : parseSourceRunTarget(window.location.search);
}

function RunsWorkbench({ workspaceId, onWorkspaceChange, engineeringProperties }: Props) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceState, setWorkspaceState] = useState<LoadState>("loading");
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [runs, setRuns] = useState<SimulationRunSummary[]>([]);
  const [runsState, setRunsState] = useState<LoadState>("idle");
  const [runsError, setRunsError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SimulationRunDetail | null>(null);
  const [detailState, setDetailState] = useState<LoadState>("idle");
  const [detailError, setDetailError] = useState<string | null>(null);
  const [logs, setLogs] = useState<RunLog[]>([]);
  const [logsState, setLogsState] = useState<LoadState>("idle");
  const [logsError, setLogsError] = useState<string | null>(null);
  const [artifacts, setArtifacts] = useState<RunArtifact[]>([]);
  const [artifactsState, setArtifactsState] = useState<LoadState>("idle");
  const [artifactsError, setArtifactsError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [previousRunConfirmation, setPreviousRunConfirmation] = useState<PreviousRunConfirmation | null>(null);
  const [previousRunMessage, setPreviousRunMessage] = useState<string | null>(null);

  const workspaceGeneration = useRef(0);
  const listGeneration = useRef(0);
  const detailGeneration = useRef(0);
  const logsGeneration = useRef(0);
  const artifactsGeneration = useRef(0);
  const currentWorkspace = useRef(workspaceId);
  const currentRun = useRef(selectedId);
  const searchRef = useRef<HTMLInputElement>(null);
  const runRefs = useRef(new Map<string, HTMLButtonElement>());
  const pendingRefreshFocus = useRef<{ runId: string | null } | null>(null);
  const pendingSourceRun = useRef<SourceRunTarget | null>(initialSourceRunTarget());
  currentWorkspace.current = workspaceId;
  currentRun.current = selectedId;

  const clearRunState = () => {
    listGeneration.current += 1;
    detailGeneration.current += 1;
    logsGeneration.current += 1;
    artifactsGeneration.current += 1;
    setRuns([]);
    setSelectedId(null);
    setDetail(null);
    setLogs([]);
    setArtifacts([]);
    setRunsState("idle");
    setDetailState("idle");
    setLogsState("idle");
    setArtifactsState("idle");
    setRunsError(null);
    setDetailError(null);
    setLogsError(null);
    setArtifactsError(null);
    setPreviousRunConfirmation(null);
    setPreviousRunMessage(null);
    pendingRefreshFocus.current = null;
  };

  const changeWorkspace = (next: string | null, preserveSourceTarget = false) => {
    if (!preserveSourceTarget) pendingSourceRun.current = null;
    clearRunState();
    onWorkspaceChange(next);
  };

  const loadWorkspaces = () => {
    const generation = ++workspaceGeneration.current;
    setWorkspaceState("loading");
    setWorkspaceError(null);
    void listWorkspaces().then((rows) => {
      if (!acceptsResponse({ generation, identity: "workspaces" }, workspaceGeneration.current, "workspaces")) return;
      setWorkspaces(rows);
      setWorkspaceState("ready");

      const sourceTarget = pendingSourceRun.current;
      if (sourceTarget) {
        if (rows.some((row) => row.id === sourceTarget.workspaceId)) {
          if (currentWorkspace.current !== sourceTarget.workspaceId) changeWorkspace(sourceTarget.workspaceId, true);
          return;
        }
        pendingSourceRun.current = null;
      }

      const activeWorkspace = currentWorkspace.current;
      const valid = activeWorkspace !== null && rows.some((row) => row.id === activeWorkspace);
      if (!valid) changeWorkspace(rows[0]?.id ?? null);
    }).catch((error: Error) => {
      if (!acceptsResponse({ generation, identity: "workspaces" }, workspaceGeneration.current, "workspaces")) return;
      setWorkspaceState("error");
      setWorkspaceError(error.message);
    });
  };

  useEffect(loadWorkspaces, []);

  const loadRuns = (targetWorkspace: string) => {
    const generation = ++listGeneration.current;
    const focusedRunId = document.activeElement instanceof HTMLElement ? document.activeElement.dataset.runId ?? null : null;
    setRunsState("loading");
    setRunsError(null);
    void listRuns(targetWorkspace).then((rows) => {
      if (!acceptsResponse({ generation, identity: targetWorkspace }, listGeneration.current, currentWorkspace.current ?? "")) return;
      const sourceTarget = pendingSourceRun.current;
      const sourceRunId = sourceTarget?.workspaceId === targetWorkspace && rows.some((row) => row.id === sourceTarget.runId)
        ? sourceTarget.runId
        : null;
      setRuns(rows);
      setRunsState("ready");
      setSelectedId((current) => {
        const next = sourceRunId ?? chooseSelection(current, rows);
        if (focusedRunId && !rows.some((row) => row.id === focusedRunId)) pendingRefreshFocus.current = { runId: next };
        return next;
      });
      if (sourceTarget?.workspaceId === targetWorkspace) pendingSourceRun.current = null;
    }).catch((error: Error) => {
      if (!acceptsResponse({ generation, identity: targetWorkspace }, listGeneration.current, currentWorkspace.current ?? "")) return;
      setRuns([]);
      setSelectedId(null);
      setRunsState("error");
      setRunsError(error.message);
      if (focusedRunId) pendingRefreshFocus.current = { runId: null };
    });
  };

  useEffect(() => {
    const pending = pendingRefreshFocus.current;
    if (!pending) return;
    pendingRefreshFocus.current = null;
    if (pending.runId) runRefs.current.get(pending.runId)?.focus();
    else searchRef.current?.focus();
  }, [runs, selectedId, runsState]);

  useEffect(() => {
    clearRunState();
    if (workspaceId) loadRuns(workspaceId);
  }, [workspaceId]);

  const loadDetail = (targetWorkspace: string, runId: string) => {
    const identity = `${targetWorkspace}:${runId}`;
    const generation = ++detailGeneration.current;
    setDetailState("loading");
    setDetailError(null);
    void getRun(targetWorkspace, runId).then((value) => {
      const currentIdentity = `${currentWorkspace.current ?? ""}:${currentRun.current ?? ""}`;
      if (!acceptsResponse({ generation, identity }, detailGeneration.current, currentIdentity)) return;
      setDetail(value);
      setDetailState("ready");
    }).catch((error: Error) => {
      const currentIdentity = `${currentWorkspace.current ?? ""}:${currentRun.current ?? ""}`;
      if (!acceptsResponse({ generation, identity }, detailGeneration.current, currentIdentity)) return;
      setDetail(null);
      setDetailState("error");
      setDetailError(error instanceof RunsRequestError && error.status === 404 ? "Selected run is no longer available. Refresh the run list." : error.message);
    });
  };


  const loadLogs = (targetWorkspace: string, runId: string) => {
    const identity = `${targetWorkspace}:${runId}`;
    const generation = ++logsGeneration.current;
    setLogsState("loading");
    setLogsError(null);
    void listRunLogs(targetWorkspace, runId).then((rows) => {
      const currentIdentity = `${currentWorkspace.current ?? ""}:${currentRun.current ?? ""}`;
      if (!acceptsResponse({ generation, identity }, logsGeneration.current, currentIdentity)) return;
      setLogs(rows);
      setLogsState("ready");
    }).catch((error: Error) => {
      const currentIdentity = `${currentWorkspace.current ?? ""}:${currentRun.current ?? ""}`;
      if (!acceptsResponse({ generation, identity }, logsGeneration.current, currentIdentity)) return;
      setLogs([]);
      setLogsState("error");
      setLogsError(error.message);
    });
  };

  const loadArtifacts = (targetWorkspace: string, runId: string) => {
    const identity = `${targetWorkspace}:${runId}`;
    const generation = ++artifactsGeneration.current;
    setArtifactsState("loading");
    setArtifactsError(null);
    void listRunArtifacts(targetWorkspace, runId).then((rows) => {
      const currentIdentity = `${currentWorkspace.current ?? ""}:${currentRun.current ?? ""}`;
      if (!acceptsResponse({ generation, identity }, artifactsGeneration.current, currentIdentity)) return;
      setArtifacts(rows);
      setArtifactsState("ready");
    }).catch((error: Error) => {
      const currentIdentity = `${currentWorkspace.current ?? ""}:${currentRun.current ?? ""}`;
      if (!acceptsResponse({ generation, identity }, artifactsGeneration.current, currentIdentity)) return;
      setArtifacts([]);
      setArtifactsState("error");
      setArtifactsError(error.message);
    });
  };

  useEffect(() => {
    detailGeneration.current += 1;
    logsGeneration.current += 1;
    artifactsGeneration.current += 1;
    setDetail(null);
    setLogs([]);
    setArtifacts([]);
    setPreviousRunConfirmation(null);
    setPreviousRunMessage(null);
    if (workspaceId && selectedId) {
      loadDetail(workspaceId, selectedId);
      loadLogs(workspaceId, selectedId);
      loadArtifacts(workspaceId, selectedId);
    }
  }, [workspaceId, selectedId]);

  const statuses = useMemo(() => Array.from(new Set(runs.map((run) => run.status))).sort(), [runs]);
  const filtered = useMemo(() => visibleRuns(runs, query, statusFilter), [runs, query, statusFilter]);
  const hiddenSelection = Boolean(selectedId && !filtered.some((run) => run.id === selectedId));
  const previousRunLoadability = detail ? engineeringProperties.previousRunLoadability(detail) : null;

  const handleLoadResult = (result: ReturnType<EngineeringPropertiesController["loadPreviousSuccessfulRun"]>) => {
    if (result.status === "confirm-required") return;
    setPreviousRunConfirmation(null);
    if (result.status === "loaded") {
      setPreviousRunMessage("Loaded as working configuration. Deterministic preflight is checking the restored snapshot.");
    } else if (result.status === "pending-model-switch") {
      setPreviousRunMessage("Switching to the run's exact model contract before applying the working configuration.");
    } else if (result.status === "stale") {
      setPreviousRunMessage("Working configuration changed before load completed. Nothing was replaced.");
    } else {
      setPreviousRunMessage(result.reason);
    }
  };

  const requestPreviousRunLoad = () => {
    if (!detail || !previousRunLoadability?.loadable) return;
    const expectedRevision = engineeringProperties.revision;
    const result = engineeringProperties.loadPreviousSuccessfulRun(detail, expectedRevision);
    if (result.status === "confirm-required") {
      setPreviousRunConfirmation({ runId: detail.id, revision: expectedRevision });
      setPreviousRunMessage(null);
      return;
    }
    handleLoadResult(result);
  };

  const confirmPreviousRunLoad = () => {
    if (!detail || !previousRunConfirmation || previousRunConfirmation.runId !== detail.id) {
      setPreviousRunConfirmation(null);
      setPreviousRunMessage("Selected run changed before confirmation. Nothing was replaced.");
      return;
    }
    handleLoadResult(engineeringProperties.loadPreviousSuccessfulRun(detail, previousRunConfirmation.revision, true));
  };

  if (workspaceState === "error") return <section className="runs-empty"><h1>Runs</h1><p>Workspace discovery failed: {workspaceError}</p><button onClick={loadWorkspaces}>Retry</button></section>;
  if (workspaceState === "loading") return <section className="runs-empty"><h1>Runs</h1><p>Loading workspaces…</p></section>;
  if (workspaces.length === 0) return <section className="runs-empty"><h1>Runs</h1><p>No workspaces</p></section>;

  return (
    <section className="runs-workbench" aria-labelledby="runs-title">
      <header className="runs-toolbar">
        <div><p className="eyebrow">Execution evidence</p><h1 id="runs-title">Runs</h1></div>        <button type="button" disabled={!workspaceId || runsState === "loading"} onClick={() => { if (workspaceId) { loadRuns(workspaceId); if (selectedId) loadDetail(workspaceId, selectedId); } }}>Refresh</button>
      </header>

      <div className="runs-grid">
        <aside className="runs-list" aria-labelledby="run-list-title">
          <h2 id="run-list-title">Run history</h2>
          <label>Search<input ref={searchRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Label or id" /></label>
          <label>Status<select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="">All statuses</option>{statuses.map((status) => <option key={status} value={status}>{status}</option>)}</select></label>
          {runsState === "loading" && <p>Loading runs…</p>}
          {runsState === "error" && <div className="runs-inline-error"><p>Run list failed: {runsError}</p><button onClick={() => workspaceId && loadRuns(workspaceId)}>Retry</button></div>}
          {runsState === "ready" && runs.length === 0 && <p>No persisted runs</p>}
          {hiddenSelection && <p className="runs-note">Selected run hidden by current filter</p>}
          <div className="runs-list__rows">{filtered.map((run) => <button type="button" key={run.id} data-run-id={run.id} ref={(node) => { if (node) runRefs.current.set(run.id, node); else runRefs.current.delete(run.id); }} className={run.id === selectedId ? "run-row is-selected" : "run-row"} aria-pressed={run.id === selectedId} onClick={() => setSelectedId(run.id)}><span><strong>{run.run_label || "Unnamed run"}</strong></span><span className="run-row__meta"><span>{run.status}</span><time>{fmt(run.created_at)}</time></span></button>)}</div>
        </aside>

        <main className="run-detail">
          {!selectedId && runsState === "ready" && runs.length > 0 && <p>Select a run</p>}
          {detailState === "loading" && <p>Loading run detail…</p>}
          {detailState === "error" && <div className="runs-inline-error"><p>Run detail failed: {detailError}</p><button onClick={() => workspaceId && selectedId && loadDetail(workspaceId, selectedId)}>Retry</button></div>}
          {detail && <>
            <header className="run-detail__header"><div><p className="eyebrow">Persisted run</p><h2>{detail.run_label || "Unnamed run"}</h2></div><strong className="run-status">{detail.status}</strong></header>
            {detail.stale ? <p className="runs-note" role="status">Stale: a linked Parameter changed. Rebind the current Parameter and run this version again.</p> : null}
            {detail.superseded ? <p className="runs-note" role="status">Superseded: a newer published Studio version exists. This run remains bound to its original version.</p> : null}
            <dl className="run-facts"><div><dt>Created</dt><dd>{fmt(detail.created_at)}</dd></div><div><dt>Started</dt><dd>{fmt(detail.started_at)}</dd></div><div><dt>Completed</dt><dd>{fmt(detail.completed_at)}</dd></div></dl>
            <section className="run-evidence" aria-label="Working configuration action">
              <header><h3>Working configuration</h3></header>
              {previousRunLoadability?.loadable ? (
                <button type="button" onClick={requestPreviousRunLoad}>Load as working configuration</button>
              ) : (
                <p>{previousRunLoadability?.reason ?? "This run cannot be loaded as a working configuration."}</p>
              )}
              {previousRunConfirmation ? (
                <div className="runs-inline-error" role="alert">
                  <p>Current unsaved working edits will be replaced. This does not change canonical project records.</p>
                  <button type="button" onClick={confirmPreviousRunLoad}>Replace unsaved changes</button>
                  <button type="button" onClick={() => setPreviousRunConfirmation(null)}>Cancel</button>
                </div>
              ) : null}
              {previousRunMessage ? <p aria-live="polite">{previousRunMessage}</p> : null}
            </section>
            {detail.notes && <p className="run-notes">{detail.notes}</p>}
            <details className="run-technical"><summary>Technical details</summary><dl className="run-facts"><div><dt>Run id</dt><dd className="technical-token">{detail.id}</dd></div><div><dt>Model version id</dt><dd className="technical-token">{detail.model_version_id ?? "—"}</dd></div></dl></details>
            <Payload title="Inputs" projection={projectPayload(detail.input_payload, "input")} />
            <Payload title="Bindings" projection={projectPayload(detail.parameter_payload, "binding")} />
            <Payload title="Results" projection={projectPayload(detail.output_payload, "result")} />

            <section className="run-evidence"><header><h3>Logs</h3>{logsState === "error" && <button onClick={() => workspaceId && selectedId && loadLogs(workspaceId, selectedId)}>Retry</button>}</header>{logsState === "loading" && <p>Loading logs…</p>}{logsState === "error" && <p>Logs unavailable: {logsError}</p>}{logsState === "ready" && logs.length === 0 && <p>No persisted logs</p>}{logs.map((log) => <article className="run-log" key={log.id}><header><strong>{log.stream}</strong><time>{fmt(log.created_at)}</time>{log.truncated && <span>truncated</span>}</header><details><summary>View raw log</summary><pre>{log.content}</pre></details></article>)}</section>

            <section className="run-evidence"><header><h3>Artifacts</h3>{artifactsState === "error" && <button onClick={() => workspaceId && selectedId && loadArtifacts(workspaceId, selectedId)}>Retry</button>}</header>{artifactsState === "loading" && <p>Loading artifacts…</p>}{artifactsState === "error" && <p>Artifacts unavailable: {artifactsError}</p>}{artifactsState === "ready" && artifacts.length === 0 && <p>No persisted artifacts</p>}<div className="run-artifacts">{artifacts.map((artifact) => <article key={artifact.artifact_id}><strong>{artifact.filename}</strong><span>{artifact.status} · {artifact.role} / {artifact.artifact_type}</span><details><summary>Technical details</summary><dl><div><dt>Size</dt><dd>{artifact.size_bytes ?? "—"}</dd></div><div><dt>MIME</dt><dd>{artifact.mime_type ?? "—"}</dd></div><div><dt>SHA-256</dt><dd className="technical-token">{artifact.sha256 ?? "—"}</dd></div><div><dt>Source</dt><dd>{[artifact.source_module, artifact.source_ref].filter(Boolean).join(" · ") || "—"}</dd></div><div><dt>Created</dt><dd>{fmt(artifact.created_at)}</dd></div>{!artifact.under_data_root && <div><dt>Storage boundary</dt><dd>Outside configured data root</dd></div>}</dl></details></article>)}</div></section>
          </>}
        </main>
      </div>
    </section>
  );
}

function Payload({ title, projection }: { title: string; projection: ReturnType<typeof projectPayload> }) {
  if (projection.state !== "ok") return <section className="run-payload"><header><h3>{title}</h3></header><p>{projection.text}</p></section>;
  return <section className="run-payload"><header><h3>{title}</h3>{projection.truncated && <span>Presentation truncated</span>}</header><details><summary>View raw {title.toLocaleLowerCase()}</summary><pre>{projection.text}</pre></details></section>;
}

export default RunsWorkbench;
