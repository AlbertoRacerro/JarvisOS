import { useCallback, useEffect, useState } from "react";
import { designTarget } from "../../app/designTargets";
import {
  createBenchRun,
  getBenchRun,
  listBenchRuns,
  type BenchCheck,
  type BenchOutput,
  type BenchRunDetail,
  type BenchRunRow,
} from "../../api/benchRuns";
import type { DraftObject, DraftProjection } from "../../api/processDraft";
import NumericInput from "../ui/NumericInput";
import { DesignStatus } from "./DesignStatus";
import {
  deriveAxialO2Defaults,
  type AxialO2Defaults,
  type ProvenanceSource,
} from "./physicsHandoff";

export interface AxialOxygenPanelProps {
  workspaceId: string;
  draft?: DraftProjection | null;
  selectedUnit?: DraftObject | null;
  initialOpen?: boolean;
}

const EVALUATOR_ID = "bioprocess.tube_oxygen_buildup";
const EVALUATOR_ALT = "photobioreactor.tube_oxygen";

export default function AxialOxygenPanel({
  workspaceId,
  draft,
  selectedUnit,
  initialOpen = true,
}: Readonly<AxialOxygenPanelProps>) {
  const [isOpen, setIsOpen] = useState(initialOpen);
  const [innerDiameterMm, setInnerDiameterMm] = useState("50");
  const [temperatureC, setTemperatureC] = useState("20");

  const [edits, setEdits] = useState<Partial<Record<keyof AxialO2Defaults, number>>>({});
  const [defaults, setDefaults] = useState<AxialO2Defaults>(() =>
    deriveAxialO2Defaults(draft, selectedUnit, null, null, {}),
  );

  const [runs, setRuns] = useState<BenchRunRow[]>([]);
  const [activeRun, setActiveRun] = useState<BenchRunDetail | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // When draft, unit or user edits change, recalculate defaults
  useEffect(() => {
    const next = deriveAxialO2Defaults(draft, selectedUnit, null, null, edits);
    setDefaults(next);
  }, [draft, selectedUnit, edits]);

  // Load saved runs
  const reloadRuns = useCallback(async () => {
    try {
      const res = await listBenchRuns(workspaceId);
      const tubeRuns = res.runs.filter(
        (r) => r.evaluator_id === EVALUATOR_ID || r.evaluator_id === EVALUATOR_ALT,
      );
      setRuns(tubeRuns);
      if (tubeRuns.length > 0 && !activeRun) {
        const first = await getBenchRun(workspaceId, tubeRuns[0].run_id);
        setActiveRun(first);
      }
    } catch {
      // Best effort load
    }
  }, [workspaceId, activeRun]);

  useEffect(() => {
    void reloadRuns();
  }, [reloadRuns]);

  const handleFieldChange = (key: keyof AxialO2Defaults, val: number) => {
    setEdits((prev) => ({ ...prev, [key]: val }));
  };

  const handleRun = async () => {
    setRunning(true);
    setError(null);
    try {
      const inputs: Record<string, number> = {
        length: edits.length ?? defaults.length.value,
        velocity: edits.velocity ?? defaults.velocity.value,
        oxygen_production_rate:
          edits.oxygen_production_rate ?? defaults.oxygen_production_rate.value,
        saturation_concentration:
          edits.saturation_concentration ?? defaults.saturation_concentration.value,
        inlet_saturation_fraction:
          edits.inlet_saturation_fraction ?? defaults.inlet_saturation_fraction.value,
        max_saturation_fraction:
          edits.max_saturation_fraction ?? defaults.max_saturation_fraction.value,
      };

      const result = await createBenchRun(workspaceId, {
        card_id: EVALUATOR_ID,
        inputs,
        options: {},
      });
      setActiveRun(result);
      await reloadRuns();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Failed to run axial O2 evaluation.");
    } finally {
      setRunning(false);
    }
  };

  const selectRun = async (runId: string) => {
    try {
      const detail = await getBenchRun(workspaceId, runId);
      setActiveRun(detail);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load run details.");
    }
  };

  const outputByName = (name: string): BenchOutput | undefined => {
    return activeRun?.outputs?.find((o) => o.name === name);
  };

  const provenanceBadge = (source: ProvenanceSource) => (
    <span
      className={`design-project__provenance design-project__provenance--${source
        .toLowerCase()
        .replace(/ /g, "-")}`}
      title={`Source: ${source}`}
    >
      {source}
    </span>
  );

  return (
    <details
      className="design-dynamic__axial-drawer"
      open={isOpen}
      onToggle={(e) => setIsOpen((e.target as HTMLDetailsElement).open)}
      {...designTarget("design.panel.axial-o2")}
    >
      <summary className="design-dynamic__axial-summary">
        <span className="summary-title">Axial O₂ (tube) · photobioreactor.tube_oxygen</span>
        {activeRun ? (
          <span className={`verdict-badge verdict-badge--${activeRun.verdict}`}>
            {activeRun.verdict.toUpperCase()}
          </span>
        ) : (
          <span className="design-empty">No run yet</span>
        )}
      </summary>

      <div className="design-dynamic__axial-content">
        <p className="design-project__dynamic-intro">
          Calculates steady plug-flow dissolved oxygen build-up along a photobioreactor tube
          cross-section (Molina Grima et al. 2001). Exceeding the maximum saturation ratio indicates
          photo-oxidative stress risks.
        </p>

        {error ? (
          <p role="alert" className="design-project__error">
            {error}
          </p>
        ) : null}

        <div className="design-project__motion-grid">
          {/* Inputs */}
          <div className="design-project__motion-form-container">
            <h4>Tube Hydraulics & Oxygen Parameters</h4>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void handleRun();
              }}
              className="design-project__motion-form"
            >
              <div className="design-project__fields-grid">
                <div className="design-field-row">
                  <label htmlFor="input-axial-length">Tube length (m)</label>
                  <div className="design-field-control">
                    <NumericInput
                      id="input-axial-length"
                      value={String(defaults.length.value)}
                      min={0.1}
                      max={10000}
                      onValueChange={(val) => handleFieldChange("length", Number(val))}
                    />
                    {provenanceBadge(defaults.length.source)}
                  </div>
                </div>

                <div className="design-field-row">
                  <label htmlFor="input-axial-diam">Tube inner Ø (mm)</label>
                  <div className="design-field-control">
                    <NumericInput
                      id="input-axial-diam"
                      value={innerDiameterMm}
                      min={1}
                      max={1000}
                      onValueChange={setInnerDiameterMm}
                    />
                    <span className="design-project__provenance design-project__provenance--from-process">
                      from Process
                    </span>
                  </div>
                </div>

                <div className="design-field-row">
                  <label htmlFor="input-axial-vel">Liquid velocity (m/s)</label>
                  <div className="design-field-control">
                    <NumericInput
                      id="input-axial-vel"
                      value={String(defaults.velocity.value)}
                      min={0.01}
                      max={10}
                      onValueChange={(val) => handleFieldChange("velocity", Number(val))}
                    />
                    {provenanceBadge(defaults.velocity.source)}
                  </div>
                </div>

                <div className="design-field-row">
                  <label htmlFor="input-axial-temp">Liquid temperature (°C)</label>
                  <div className="design-field-control">
                    <NumericInput
                      id="input-axial-temp"
                      value={temperatureC}
                      min={0}
                      max={50}
                      onValueChange={setTemperatureC}
                    />
                    <span className="design-project__provenance design-project__provenance--from-process">
                      from Process
                    </span>
                  </div>
                </div>

                <div className="design-field-row">
                  <label htmlFor="input-axial-prod">Volumetric O₂ production (mol/(m³·s))</label>
                  <div className="design-field-control">
                    <NumericInput
                      id="input-axial-prod"
                      value={String(defaults.oxygen_production_rate.value)}
                      min={1e-5}
                      max={1}
                      onValueChange={(val) =>
                        handleFieldChange("oxygen_production_rate", Number(val))
                      }
                    />
                    {provenanceBadge(defaults.oxygen_production_rate.source)}
                  </div>
                </div>

                <div className="design-field-row">
                  <label htmlFor="input-axial-csat">O₂ saturation conc. (mol/m³)</label>
                  <div className="design-field-control">
                    <NumericInput
                      id="input-axial-csat"
                      value={String(defaults.saturation_concentration.value)}
                      min={0.01}
                      max={2}
                      onValueChange={(val) =>
                        handleFieldChange("saturation_concentration", Number(val))
                      }
                    />
                    {provenanceBadge(defaults.saturation_concentration.source)}
                  </div>
                </div>

                <div className="design-field-row">
                  <label htmlFor="input-axial-fin">Inlet saturation ratio (1)</label>
                  <div className="design-field-control">
                    <NumericInput
                      id="input-axial-fin"
                      value={String(defaults.inlet_saturation_fraction.value)}
                      min={0}
                      max={10}
                      onValueChange={(val) =>
                        handleFieldChange("inlet_saturation_fraction", Number(val))
                      }
                    />
                    {provenanceBadge(defaults.inlet_saturation_fraction.source)}
                  </div>
                </div>

                <div className="design-field-row">
                  <label htmlFor="input-axial-fmax">Max allowable saturation ratio (1)</label>
                  <div className="design-field-control">
                    <NumericInput
                      id="input-axial-fmax"
                      value={String(defaults.max_saturation_fraction.value)}
                      min={0.1}
                      max={20}
                      onValueChange={(val) =>
                        handleFieldChange("max_saturation_fraction", Number(val))
                      }
                    />
                    {provenanceBadge(defaults.max_saturation_fraction.source)}
                  </div>
                </div>
              </div>

              <div className="design-project__motion-actions">
                <button
                  type="submit"
                  className="ui-button ui-button--primary"
                  disabled={running}
                >
                  {running ? "Calculating build-up…" : "Run Axial O₂ Calculation"}
                </button>
              </div>
            </form>
          </div>

          {/* Results */}
          <div className="design-project__motion-results-container">
            <h4>Build-Up & Safe Run Length Results</h4>
            {activeRun ? (
              <div className="design-project__motion-results">
                <div className="design-project__motion-verdict-banner">
                  <span className="verdict-label">Calculation Verdict:</span>
                  <span className={`verdict-badge verdict-badge--${activeRun.verdict}`}>
                    {activeRun.verdict.toUpperCase()}
                  </span>
                  <span className="run-id-label">({activeRun.run_id})</span>
                </div>

                <dl className="design-project__motion-outputs">
                  <div>
                    <dt>Outlet O₂ concentration</dt>
                    <dd>
                      {outputByName("outlet_concentration")?.value.value.toFixed(4) ?? "—"} mol/m³
                    </dd>
                  </div>
                  <div>
                    <dt>Outlet saturation ratio</dt>
                    <dd>
                      {outputByName("outlet_saturation_fraction")?.value.value.toFixed(3) ?? "—"} (
                      {(
                        (outputByName("outlet_saturation_fraction")?.value.value ?? 0) * 100
                      ).toFixed(1)}
                      % air-sat)
                    </dd>
                  </div>
                  <div>
                    <dt>Max safe run length (L_max)</dt>
                    <dd>
                      <strong>
                        {outputByName("max_length")?.value.value.toFixed(1) ?? "—"} m
                      </strong>
                    </dd>
                  </div>
                  <div>
                    <dt>Hydraulic residence time</dt>
                    <dd>{outputByName("residence_time")?.value.value.toFixed(1) ?? "—"} s</dd>
                  </div>
                </dl>

                {/* Checks */}
                <div className="design-project__motion-checks">
                  <h4>Check Sheet</h4>
                  <table>
                    <thead>
                      <tr>
                        <th>Check</th>
                        <th>Status</th>
                        <th>Limit / Hint</th>
                      </tr>
                    </thead>
                    <tbody>
                      {activeRun.checks?.map((chk: BenchCheck) => (
                        <tr key={chk.id}>
                          <td>{chk.id}</td>
                          <td>
                            <span className={`check-badge check-badge--${chk.status}`}>
                              {chk.status}
                            </span>
                          </td>
                          <td>{chk.hint || (chk.limit != null ? `<= ${chk.limit}` : "—")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : (
              <div className="design-empty">
                No axial oxygen calculation has been run yet.
              </div>
            )}

            {/* History */}
            <div className="design-project__motion-history">
              <h4>Saved Axial O₂ Runs ({runs.length})</h4>
              {runs.length === 0 ? (
                <p className="design-empty">No runs saved for this workspace.</p>
              ) : (
                <ul className="design-project__runs-list">
                  {runs.map((r) => (
                    <li key={r.run_id}>
                      <button
                        type="button"
                        className={`design-project__run-item ${
                          activeRun?.run_id === r.run_id ? "is-active" : ""
                        }`}
                        onClick={() => void selectRun(r.run_id)}
                      >
                        <span className="run-id">{r.run_id}</span>
                        <span className={`run-verdict run-verdict--${r.verdict}`}>{r.verdict}</span>
                        <span className="run-time">
                          {r.completed_at ? new Date(r.completed_at).toLocaleTimeString() : ""}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      </div>
    </details>
  );
}
