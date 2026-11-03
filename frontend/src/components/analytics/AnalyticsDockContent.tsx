import { useEffect, useMemo, useRef, useState } from "react";

import { listModelImplementations, type ModelImplementation } from "../../api/client";
import { listRuns, type SimulationRunSummary } from "../../api/runs";
import {
  MAX_SELECTED_RUNS,
  acceptsWorkspaceResponse,
  compareAnalyticsRuns,
  compareEngineeringConfigurations,
  normalizeBaselineRunId,
  retainExistingSelection,
  toggleRunSelection,
} from "./analyticsState";
import { sourceRunHref } from "./variantComparisonNavigation";

type Props = Readonly<{ workspaceId: string | null }>;
type LoadState = "idle" | "loading" | "ready" | "error";

function formatNumber(value: number): string {
  return String(value);
}

function formatDelta(value: number | null): string {
  if (value === null) return "—";
  if (value === 0) return "No change";
  return `${value > 0 ? "+" : ""}${formatNumber(value)}`;
}

function boundedRunLabel(run: SimulationRunSummary): string {
  return Array.from(run.run_label?.trim() || run.id).slice(0, 160).join("");
}

function AnalyticsDockContent({ workspaceId }: Props) {
  const [runs, setRuns] = useState<SimulationRunSummary[]>([]);
  const [implementations, setImplementations] = useState<ModelImplementation[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [baselineRunId, setBaselineRunId] = useState<string | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [modelError, setModelError] = useState<string | null>(null);
  const generationRef = useRef(0);
  const workspaceRef = useRef(workspaceId);
  workspaceRef.current = workspaceId;

  const load = (targetWorkspace: string) => {
    const generation = ++generationRef.current;
    setLoadState("loading");
    setError(null);
    setModelError(null);
    setImplementations([]);
    void Promise.allSettled([listRuns(targetWorkspace), listModelImplementations(targetWorkspace)]).then(([runResult, modelResult]) => {
      if (!acceptsWorkspaceResponse({ generation, identity: targetWorkspace }, generationRef.current, workspaceRef.current ?? "")) return;
      if (runResult.status === "rejected") {
        setRuns([]);
        setSelectedIds([]);
        setBaselineRunId(null);
        setImplementations([]);
        setLoadState("error");
        setError(runResult.reason instanceof Error ? runResult.reason.message : "Persisted run list failed.");
        return;
      }

      setRuns(runResult.value);
      setSelectedIds((current) => retainExistingSelection(current, runResult.value));
      if (modelResult.status === "fulfilled") {
        setImplementations(modelResult.value);
      } else {
        setImplementations([]);
        setModelError(modelResult.reason instanceof Error ? modelResult.reason.message : "Model contracts unavailable.");
      }
      setLoadState("ready");
    });
  };

  useEffect(() => {
    generationRef.current += 1;
    setRuns([]);
    setImplementations([]);
    setSelectedIds([]);
    setBaselineRunId(null);
    setError(null);
    setModelError(null);
    setLoadState(workspaceId ? "loading" : "idle");
    if (workspaceId) load(workspaceId);
  }, [workspaceId]);

  useEffect(() => {
    setBaselineRunId((current) => normalizeBaselineRunId(selectedIds, current));
  }, [selectedIds]);

  const selectedRuns = useMemo(() => selectedIds.map((id) => runs.find((run) => run.id === id)).filter((run): run is SimulationRunSummary => Boolean(run)), [runs, selectedIds]);
  const comparison = useMemo(() => compareAnalyticsRuns(selectedRuns), [selectedRuns]);
  const visibleBaselineRunId = normalizeBaselineRunId(selectedIds, baselineRunId);
  const configuration = useMemo(
    () => workspaceId ? compareEngineeringConfigurations(workspaceId, selectedRuns, implementations, visibleBaselineRunId) : null,
    [workspaceId, selectedRuns, implementations, visibleBaselineRunId]
  );
  const capReached = selectedIds.length >= MAX_SELECTED_RUNS;

  if (!workspaceId) return <div className="analytics-dock-content"><p className="analytics-empty">Analytics unavailable until a workspace is selected.</p></div>;

  return (
    <div className="analytics-dock-content">
      <header className="analytics-context">
        <div><p className="eyebrow">Persisted run evidence</p><h3>Run comparison</h3></div>
        <button type="button" onClick={() => load(workspaceId)} disabled={loadState === "loading"}>Refresh</button>
      </header>
      <p className="analytics-workspace">Workspace <span className="technical-token">{workspaceId}</span></p>

      {loadState === "loading" && <p>Loading persisted runs…</p>}
      {loadState === "error" && <div className="analytics-error"><p>Run list failed: {error}</p><button type="button" onClick={() => load(workspaceId)}>Retry</button></div>}
      {loadState === "ready" && runs.length === 0 && <p className="analytics-empty">No persisted runs are available for analytics.</p>}

      {runs.length > 0 && <section className="analytics-selection" aria-labelledby="analytics-runs-title">
        <div className="analytics-section-heading"><h4 id="analytics-runs-title">Select runs</h4><span>{selectedIds.length}/{MAX_SELECTED_RUNS}</span></div>
        <p className="analytics-help">Direct comparison requires the same exact model version and exact persisted unit strings.</p>
        <div className="analytics-run-list">{runs.map((run) => {
          const checked = selectedIds.includes(run.id);
          return <label key={run.id} className="analytics-run-row"><input type="checkbox" checked={checked} disabled={!checked && capReached} onChange={() => setSelectedIds((current) => toggleRunSelection(current, run.id))} /><span><strong>{boundedRunLabel(run)}</strong><small className="technical-token">{run.id}</small></span><span className="analytics-run-meta"><span>{run.status}</span><small>{run.model_version_id ?? "model version unavailable"}</small></span></label>;
        })}</div>
        {capReached && <p className="analytics-help">Six-run comparison limit reached. Remove a selected run to choose another.</p>}
      </section>}

      {selectedRuns.length >= 2 && visibleBaselineRunId && <fieldset className="analytics-selection">
        <legend>Comparison baseline</legend>
        <p className="analytics-help">Baseline applies to engineering configuration and remains available when configuration evidence is unavailable but recorded results are still comparable.</p>
        <div className="analytics-run-list">{selectedRuns.map((run) => <label key={run.id} className="analytics-run-row"><input type="radio" name="analytics-baseline" checked={visibleBaselineRunId === run.id} onChange={() => setBaselineRunId(run.id)} /><span><strong>{boundedRunLabel(run)}</strong>{visibleBaselineRunId === run.id && <small>Baseline</small>}</span></label>)}</div>
      </fieldset>}

      {selectedRuns.length > 0 && <section className="analytics-results" aria-labelledby="analytics-source-runs-title">
        <h4 id="analytics-source-runs-title">Source runs</h4>
        <div className="analytics-run-list">{selectedRuns.map((run) => <a key={run.id} href={sourceRunHref(workspaceId, run.id)}>Open {boundedRunLabel(run)}</a>)}</div>
      </section>}

      <section className="analytics-results" aria-live="polite" aria-labelledby="analytics-configuration-title">
        <h4 id="analytics-configuration-title">Engineering configuration</h4>
        {modelError && selectedIds.length >= 2 && <p className="analytics-rejection">Model contracts unavailable: {modelError}</p>}
        {!modelError && configuration?.message && <p className={configuration.state === "rejected" ? "analytics-rejection" : "analytics-help"}>{configuration.message}</p>}
        {!modelError && configuration?.state === "ready" && configuration.baselineRunId && <>
          <div className="analytics-table-wrap"><table><thead><tr><th scope="col">Engineering input</th>{selectedRuns.map((run) => <th scope="col" key={run.id}>{boundedRunLabel(run)}{configuration.baselineRunId === run.id ? " · Baseline" : ""}</th>)}</tr></thead><tbody>
            {configuration.modelChoice && <tr><th scope="row">{configuration.modelChoice.familyLabel}<small className="technical-token">{configuration.modelChoice.familyKey}</small><small>Model choice</small></th>{selectedRuns.map((run) => <td key={run.id}><span>{configuration.modelChoice?.optionLabel}</span><small>{run.id === configuration.baselineRunId ? "Baseline model" : "Same exact model version"}</small></td>)}</tr>}
            {configuration.rows.map((row) => <tr key={row.name}><th scope="row">{row.label}<small className="technical-token">{row.name}</small><small>{row.unit}</small></th>{row.cells.map((cell) => <td key={cell.runId}><span>{cell.displayValue}{cell.value !== null ? ` ${row.unit}` : ""}</span><small>{cell.runId === configuration.baselineRunId ? "Baseline" : `Δ ${formatDelta(cell.delta)}${cell.delta !== null ? ` ${row.unit}` : ""}`}</small></td>)}</tr>)}
          </tbody></table></div>
        </>}
      </section>

      <section className="analytics-results" aria-live="polite" aria-labelledby="analytics-results-title">
        <h4 id="analytics-results-title">Recorded results</h4>
        {comparison.message && <p className={comparison.state === "rejected" ? "analytics-rejection" : "analytics-help"}>{comparison.message}</p>}
        {comparison.groups.length > 0 && <div className="analytics-groups">{comparison.groups.map((group) => group.state === "rejected" ? (
          <article key={group.key} className="analytics-group analytics-group--rejected"><header><strong className="technical-token">{group.key}</strong><span>Rejected</span></header><p>{group.reason}</p></article>
        ) : (
          <article key={group.key} className="analytics-group"><header><strong className="technical-token">{group.key}</strong><span>{group.unit}</span></header><div className="analytics-table-wrap"><table><thead><tr><th scope="col">Run</th><th scope="col">Value</th></tr></thead><tbody>{group.values.map((item) => <tr key={item.runId}><th scope="row">{item.label}</th><td>{formatNumber(item.value)} {group.unit}</td></tr>)}</tbody><tfoot><tr><th scope="row">Minimum</th><td>{formatNumber(group.min)} {group.unit}</td></tr><tr><th scope="row">Maximum</th><td>{formatNumber(group.max)} {group.unit}</td></tr><tr><th scope="row">Range</th><td>{formatNumber(group.range)} {group.unit}</td></tr></tfoot></table></div></article>
        ))}</div>}
      </section>
    </div>
  );
}

export default AnalyticsDockContent;