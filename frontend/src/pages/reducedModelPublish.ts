// Spec 216 contract 7: pure helpers for publishing a reduced model from server-measured CFD runs.
// Every rule mirrors backend study_from_runs.py / reduced_model.py; the server stays the authority and re-checks all of it.
import type { CfdRun, ReducedModelPublication, ReducedModelRegime } from "../api/designProject";

export const REGIMES: ReducedModelRegime[] = ["laminar", "turbulent_rans"];
/** study_from_runs.STUDY_RUNS: a mesh study is exactly three distinct runs. */
export const MESH_STUDY_RUNS = 3;
/** reduced_model.py: a segment needs runs at two or more Reynolds numbers. */
export const MIN_REYNOLDS_PER_REGIME = 2;

export type RunEligibility = { run: CfdRun; eligible: boolean; reasons: string[] };

const isNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const measured = (run: CfdRun) => run.measurement?.status === "measured" ? run.measurement : null;

/** Why a run cannot be study evidence (study_from_runs._load, reduced_model.py:207-211); empty means eligible. */
export function runIneligibility(run: CfdRun): string[] {
  if (run.state !== "finished") return [`${run.state}, not finished`];
  const reasons: string[] = [];
  const measurement = run.measurement;
  if (!measurement) reasons.push("unmeasured: no measurement recorded");
  else if (measurement.status !== "measured") reasons.push(`unmeasured: ${measurement.reason || "no measurement recorded"}`);
  else {
    if (!measurement.case_manifest_digest) reasons.push("no case manifest digest recorded");
    if (!isNumber(measurement.pressure_gradient_pa_m)) reasons.push("no pressure gradient recorded");
    else if (measurement.pressure_gradient_pa_m <= 0) reasons.push("no positive pressure gradient");
  }
  if (!run.residual_converged) reasons.push("residuals did not meet their target (not converged)");
  return reasons;
}

export function classifyRuns(runs: CfdRun[]): Record<ReducedModelRegime, RunEligibility[]> {
  const out: Record<ReducedModelRegime, RunEligibility[]> = { laminar: [], turbulent_rans: [] };
  for (const run of runs) {
    const bucket = out[run.regime as ReducedModelRegime];
    if (!bucket) continue;
    const reasons = runIneligibility(run);
    bucket.push({ run, eligible: reasons.length === 0, reasons });
  }
  return out;
}

export type PublishSelection = {
  modelId: string;
  /** Run ids chosen as study points, and the three mesh-study runs, per regime. */
  points: Record<ReducedModelRegime, string[]>;
  meshStudy: Record<ReducedModelRegime, string[]>;
  tolerances: { laminar: string; turbulent_rans: string };
};
export const emptySelection = (): PublishSelection => ({
  modelId: "", points: { laminar: [], turbulent_rans: [] }, meshStudy: { laminar: [], turbulent_rans: [] },
  tolerances: { laminar: "", turbulent_rans: "" }
});

export type PublishPlan = { body: ReducedModelPublication | null; reasons: string[] };

const parseTolerance = (text: string): number | null => {
  const value = text.trim() === "" ? NaN : Number(text);
  return Number.isFinite(value) && value > 0 && value < 1 ? value : null;  // Tolerances: gt=0, lt=1
};

/** The request body, or the plain reasons publication is not possible from this selection. */
export function planPublication(runs: CfdRun[], selection: PublishSelection, component: { revision: number }, activeVersionId: string): PublishPlan {
  const byId = new Map(runs.map((run) => [run.run_id, run]));
  const reasons: string[] = [];
  const modelId = selection.modelId.trim();
  if (!modelId) reasons.push("Enter a reduced model id.");
  const used = REGIMES.filter((regime) => selection.points[regime].length > 0);
  if (!used.length) reasons.push("Select at least one study run.");
  const allPoints = used.flatMap((regime) => selection.points[regime]);
  if (new Set(allPoints).size !== allPoints.length) reasons.push("A run is selected more than once.");
  for (const regime of REGIMES) {
    const meshIds = selection.meshStudy[regime];
    if (!used.includes(regime)) {
      if (meshIds.length) reasons.push(`${regime}: mesh-study runs were chosen but no ${regime} study run is selected.`);  // study_from_runs.py:128
      continue;
    }
    const chosen = selection.points[regime].map((id) => byId.get(id));
    const pointRuns = chosen.filter((run): run is CfdRun => run !== undefined);
    if (pointRuns.length !== chosen.length) reasons.push(`${regime}: a selected study run no longer exists.`);
    for (const run of pointRuns) for (const why of runIneligibility(run)) reasons.push(`${regime}: run ${run.run_id.slice(0, 8)} is ${why}.`);
    const reynolds = new Set(pointRuns.map((run) => { const m = measured(run); return isNumber(m?.reynolds) ? Number(m.reynolds.toFixed(6)) : null; }));
    if (reynolds.has(null)) reasons.push(`${regime}: a study run recorded no Reynolds number.`);
    else if (reynolds.size < MIN_REYNOLDS_PER_REGIME) reasons.push(`${regime}: study runs need two or more Reynolds numbers.`);  // reduced_model.py:229
    const levels = new Set(pointRuns.map((run) => measured(run)?.mesh_level));
    if (levels.size > 1) reasons.push(`${regime}: study runs must share one mesh level.`);  // study_from_runs.py:79
    // Mesh study: exactly three distinct runs (study_from_runs.py:83).
    if (meshIds.length !== MESH_STUDY_RUNS || new Set(meshIds).size !== MESH_STUDY_RUNS) {
      reasons.push(`${regime}: the mesh study needs exactly ${MESH_STUDY_RUNS} distinct runs (selected ${meshIds.length}).`);
      continue;
    }
    const meshRuns = meshIds.map((id) => byId.get(id)).filter((run): run is CfdRun => run !== undefined);
    if (meshRuns.length !== MESH_STUDY_RUNS) { reasons.push(`${regime}: a selected mesh-study run no longer exists.`); continue; }
    for (const run of meshRuns) for (const why of runIneligibility(run)) reasons.push(`${regime} mesh study: run ${run.run_id.slice(0, 8)} is ${why}.`);
    const meshMeasures = meshRuns.map(measured);
    if (new Set(meshMeasures.map((m) => m?.mesh_level)).size !== MESH_STUDY_RUNS) reasons.push(`${regime}: the mesh study needs three distinct mesh levels.`);  // :101
    if (new Set(meshMeasures.map((m) => isNumber(m?.reynolds) ? Number(m.reynolds.toFixed(6)) : null)).size !== 1) reasons.push(`${regime}: mesh-study runs must share one Reynolds number.`);  // :99
    if (meshMeasures.some((m) => !isNumber(m?.cells) || (m?.cells ?? 0) <= 0)) reasons.push(`${regime}: a mesh-study run recorded no mesh cell count.`);  // :107
    if (levels.size === 1 && !meshMeasures.some((m) => m?.mesh_level === [...levels][0])) reasons.push(`${regime}: the study runs' mesh level is not in the mesh study.`);  // :103
  }
  const tolerances = { laminar: parseTolerance(selection.tolerances.laminar), turbulent_rans: parseTolerance(selection.tolerances.turbulent_rans) };
  for (const regime of REGIMES) if (tolerances[regime] === null) reasons.push(`Enter a ${regime} tolerance greater than 0 and less than 1.`);
  if (reasons.length) return { body: null, reasons: [...new Set(reasons)] };
  const meshStudy: ReducedModelPublication["mesh_study_run_ids"] = {};
  for (const regime of used) meshStudy[regime] = [...selection.meshStudy[regime]];
  return { body: { model_id: modelId, run_ids: allPoints, mesh_study_run_ids: meshStudy,
    tolerances: { laminar: tolerances.laminar!, turbulent_rans: tolerances.turbulent_rans! },
    expected_revision: component.revision, expected_active_version_id: activeVersionId }, reasons: [] };
}
