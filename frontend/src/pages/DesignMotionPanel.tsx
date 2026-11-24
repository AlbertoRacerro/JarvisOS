import { useCallback, useEffect, useRef, useState } from "react";
import { designTarget } from "../app/designTargets";
import {
  createBenchRun,
  getBenchRun,
  listBenchRuns,
  type BenchCheck,
  type BenchOutput,
  type BenchRunDetail,
  type BenchRunRow,
  type BenchVerdict,
} from "../api/benchRuns";
import { environmentApi } from "../components/environment/api";
import type { Profile, ProfileValues } from "../components/environment/types";
import type { DesignComponent } from "../api/designProject";
import NumericInput from "../components/ui/NumericInput";
import { DesignStatus } from "../components/design/DesignStatus";
import {
  deriveMotionDefaults,
  type MotionDefaults,
  type ProvenanceSource,
} from "../components/design/physicsHandoff";

export interface DesignMotionPanelProps {
  workspaceId: string;
  selectedComponent?: DesignComponent | null;
}

const EVALUATOR_ID = "marine.rigid_body_waves";

export default function DesignMotionPanel({
  workspaceId,
  selectedComponent,
}: Readonly<DesignMotionPanelProps>) {
  const [envProfiles, setEnvProfiles] = useState<Profile[]>([]);
  const [selectedProfile, setSelectedProfile] = useState<Profile | null>(null);
  const [profileValues, setProfileValues] = useState<ProfileValues | null>(null);

  // User input values and provenance tracking
  const [edits, setEdits] = useState<Partial<Record<keyof MotionDefaults, number>>>({});
  const [defaults, setDefaults] = useState<MotionDefaults>(() =>
    deriveMotionDefaults(null, null, selectedComponent, {}),
  );

  const [runs, setRuns] = useState<BenchRunRow[]>([]);
  const [activeRun, setActiveRun] = useState<BenchRunDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const initialAutoSelectDone = useRef(false);

  useEffect(() => {
    initialAutoSelectDone.current = false;
  }, [workspaceId]);

  // Load environment profiles
  useEffect(() => {
    let live = true;
    void environmentApi<Profile[]>(workspaceId, "/profiles")
      .then((items) => {
        if (!live) return;
        setEnvProfiles(items);
        if (items.length > 0) {
          setSelectedProfile(items[0]);
        }
      })
      .catch(() => {
        // Environment profiles optional; fallback to defaults
      });
    return () => {
      live = false;
    };
  }, [workspaceId]);

  // Load values for the selected profile
  useEffect(() => {
    let live = true;
    if (selectedProfile) {
      void environmentApi<ProfileValues>(
        workspaceId,
        `/profiles/${encodeURIComponent(selectedProfile.digest)}?offset=0&limit=5`,
      )
        .then((vals) => {
          if (live) setProfileValues(vals);
        })
        .catch(() => {
          if (live) setProfileValues(null);
        });
    } else {
      setProfileValues(null);
    }
    return () => {
      live = false;
    };
  }, [workspaceId, selectedProfile]);

  // Recalculate defaults when environment or component changes, preserving user edits
  useEffect(() => {
    const next = deriveMotionDefaults(selectedProfile, profileValues, selectedComponent, edits);
    setDefaults(next);
  }, [selectedProfile, profileValues, selectedComponent, edits]);

  // Load existing bench runs for marine.rigid_body_waves
  const reloadRuns = useCallback(async () => {
    try {
      const res = await listBenchRuns(workspaceId);
      const motionRuns = res.runs.filter((r) => r.evaluator_id === EVALUATOR_ID);
      setRuns(motionRuns);
      if (motionRuns.length > 0 && !initialAutoSelectDone.current) {
        initialAutoSelectDone.current = true;
        const first = await getBenchRun(workspaceId, motionRuns[0].run_id);
        setActiveRun(first);
      }
    } catch {
      // Best effort load
    }
  }, [workspaceId]);

  useEffect(() => {
    setLoading(true);
    void reloadRuns().finally(() => setLoading(false));
  }, [reloadRuns]);

  const handleFieldChange = (key: keyof MotionDefaults, val: number) => {
    setEdits((prev) => ({ ...prev, [key]: val }));
  };

  const handleRun = async () => {
    setRunning(true);
    setError(null);
    try {
      const inputs: Record<string, number> = {};
      for (const [k, item] of Object.entries(defaults)) {
        inputs[k] = edits[k as keyof MotionDefaults] ?? item.value;
      }
      const result = await createBenchRun(workspaceId, {
        card_id: EVALUATOR_ID,
        inputs,
        options: {},
      });
      setActiveRun(result);
      await reloadRuns();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Failed to run motion evaluation.");
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

  const provenanceBadge = (source: ProvenanceSource) => {
    return (
      <span
        className={`design-project__provenance design-project__provenance--${source.toLowerCase().replace(/ /g, "-")}`}
        title={`Value source: ${source}`}
      >
        {source}
      </span>
    );
  };

  return (
    <section
      className="design-project__panel design-project__main design-project__motion"
      aria-label="Motion and Mechanics"
      {...designTarget("design.panel.mechanics")}
    >
      <div className="design-project__node-head">
        <h2>Motion/Mechanics</h2>
        {activeRun ? (
          <DesignStatus
            state={
              activeRun.verdict === "usable"
                ? "ready"
                : activeRun.verdict === "not_usable"
                ? "failed"
                : "stale"
            }
            title={`Verdict: ${activeRun.verdict}`}
          />
        ) : (
          <span className="design-empty">No run executed</span>
        )}
      </div>

      <p className="design-project__dynamic-intro">
        Evaluates 3-DOF decoupled frequency-domain wave motion (surge, heave, pitch RAOs) and
        quasi-static mooring line tension for the floating structure (
        <code>marine.rigid_body_waves</code>).
      </p>

      {error ? (
        <p role="alert" className="design-project__error">
          {error}
        </p>
      ) : null}

      {/* Environment Profile Context */}
      <div className="design-project__motion-context">
        <label htmlFor="motion-env-profile">Environment profile</label>
        <select
          id="motion-env-profile"
          value={selectedProfile?.digest ?? ""}
          onChange={(e) => {
            const p = envProfiles.find((item) => item.digest === e.target.value) ?? null;
            setSelectedProfile(p);
          }}
        >
          {envProfiles.length === 0 ? (
            <option value="">No environment profiles found</option>
          ) : (
            envProfiles.map((p) => (
              <option key={p.digest} value={p.digest}>
                {p.name}
              </option>
            ))
          )}
        </select>
        {selectedComponent ? (
          <span className="design-project__motion-component-hint">
            Active component: <strong>{selectedComponent.name}</strong>
          </span>
        ) : null}
      </div>

      {/* Parameter Inputs Grid */}
      <div className="design-project__motion-grid">
        <div className="design-project__motion-form-container">
          <h3>Wave & Hydrodynamic Inputs</h3>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void handleRun();
            }}
            className="design-project__motion-form"
          >
            <div className="design-project__fields-grid">
              {/* Wave Inputs */}
              <div className="design-field-row">
                <label htmlFor="input-wave-amp">Wave amplitude (m)</label>
                <div className="design-field-control">
                  <NumericInput
                    id="input-wave-amp"
                    value={String(defaults.wave_amplitude_m.value)}
                    min={0.01}
                    max={50}
                    onValueChange={(val) => handleFieldChange("wave_amplitude_m", Number(val))}
                  />
                  {provenanceBadge(defaults.wave_amplitude_m.source)}
                </div>
              </div>

              <div className="design-field-row">
                <label htmlFor="input-wave-period">Wave period (s)</label>
                <div className="design-field-control">
                  <NumericInput
                    id="input-wave-period"
                    value={String(defaults.wave_period_s.value)}
                    min={0.5}
                    max={60}
                    onValueChange={(val) => handleFieldChange("wave_period_s", Number(val))}
                  />
                  {provenanceBadge(defaults.wave_period_s.source)}
                </div>
              </div>

              <div className="design-field-row">
                <label htmlFor="input-mass">Mass (kg)</label>
                <div className="design-field-control">
                  <NumericInput
                    id="input-mass"
                    value={String(defaults.mass_kg.value)}
                    min={1}
                    max={1e10}
                    onValueChange={(val) => handleFieldChange("mass_kg", Number(val))}
                  />
                  {provenanceBadge(defaults.mass_kg.source)}
                </div>
              </div>

              <div className="design-field-row">
                <label htmlFor="input-waterplane">Waterplane area (m²)</label>
                <div className="design-field-control">
                  <NumericInput
                    id="input-waterplane"
                    value={String(defaults.waterplane_area_m2.value)}
                    min={0.1}
                    max={1e6}
                    onValueChange={(val) => handleFieldChange("waterplane_area_m2", Number(val))}
                  />
                  {provenanceBadge(defaults.waterplane_area_m2.source)}
                </div>
              </div>

              <div className="design-field-row">
                <label htmlFor="input-gm">GM pitch (m)</label>
                <div className="design-field-control">
                  <NumericInput
                    id="input-gm"
                    value={String(defaults.gm_pitch_m.value)}
                    min={0.01}
                    max={100}
                    onValueChange={(val) => handleFieldChange("gm_pitch_m", Number(val))}
                  />
                  {provenanceBadge(defaults.gm_pitch_m.source)}
                </div>
              </div>

              <div className="design-field-row">
                <label htmlFor="input-pitch-inertia">Pitch inertia (kg·m²)</label>
                <div className="design-field-control">
                  <NumericInput
                    id="input-pitch-inertia"
                    value={String(defaults.pitch_inertia_kg_m2.value)}
                    min={1}
                    max={1e15}
                    onValueChange={(val) => handleFieldChange("pitch_inertia_kg_m2", Number(val))}
                  />
                  {provenanceBadge(defaults.pitch_inertia_kg_m2.source)}
                </div>
              </div>

              <div className="design-field-row">
                <label htmlFor="input-mooring-kx">Mooring stiffness surge (N/m)</label>
                <div className="design-field-control">
                  <NumericInput
                    id="input-mooring-kx"
                    value={String(defaults.mooring_kx_n_m.value)}
                    min={0}
                    max={1e9}
                    onValueChange={(val) => handleFieldChange("mooring_kx_n_m", Number(val))}
                  />
                  {provenanceBadge(defaults.mooring_kx_n_m.source)}
                </div>
              </div>

              <div className="design-field-row">
                <label htmlFor="input-damping-surge">Surge damping (N·s/m)</label>
                <div className="design-field-control">
                  <NumericInput
                    id="input-damping-surge"
                    value={String(defaults.damping_surge_n_s_m.value)}
                    min={0}
                    max={1e9}
                    onValueChange={(val) => handleFieldChange("damping_surge_n_s_m", Number(val))}
                  />
                  {provenanceBadge(defaults.damping_surge_n_s_m.source)}
                </div>
              </div>

              <div className="design-field-row">
                <label htmlFor="input-damping-heave">Heave damping (N·s/m)</label>
                <div className="design-field-control">
                  <NumericInput
                    id="input-damping-heave"
                    value={String(defaults.damping_heave_n_s_m.value)}
                    min={0}
                    max={1e9}
                    onValueChange={(val) => handleFieldChange("damping_heave_n_s_m", Number(val))}
                  />
                  {provenanceBadge(defaults.damping_heave_n_s_m.source)}
                </div>
              </div>
            </div>

            <div className="design-project__motion-actions">
              <button
                type="submit"
                className="ui-button ui-button--primary"
                disabled={running}
              >
                {running ? "Evaluating motion…" : "Run Motion Evaluation"}
              </button>
            </div>
          </form>
        </div>

        {/* Evaluation Results & RAO Display */}
        <div className="design-project__motion-results-container">
          <h3>Motion & Mooring Results</h3>
          {activeRun ? (
            <div className="design-project__motion-results">
              <div className="design-project__motion-verdict-banner">
                <span className="verdict-label">Run Verdict:</span>
                <span className={`verdict-badge verdict-badge--${activeRun.verdict}`}>
                  {activeRun.verdict.toUpperCase()}
                </span>
                <span className="run-id-label">({activeRun.run_id})</span>
              </div>

              <dl className="design-project__motion-outputs">
                <div>
                  <dt>Heave RAO amplitude</dt>
                  <dd>
                    {outputByName("heave_rao")?.value.value.toFixed(4) ?? "—"}{" "}
                    {outputByName("heave_rao")?.value.unit}
                  </dd>
                </div>
                <div>
                  <dt>Surge RAO amplitude</dt>
                  <dd>
                    {outputByName("surge_rao")?.value.value.toFixed(4) ?? "—"}{" "}
                    {outputByName("surge_rao")?.value.unit}
                  </dd>
                </div>
                <div>
                  <dt>Pitch RAO amplitude</dt>
                  <dd>
                    {outputByName("pitch_rao")?.value.value.toFixed(4) ?? "—"}{" "}
                    {outputByName("pitch_rao")?.value.unit}
                  </dd>
                </div>
                <div>
                  <dt>Max heave acceleration</dt>
                  <dd>
                    {outputByName("heave_acceleration")?.value.value.toFixed(3) ?? "—"}{" "}
                    {outputByName("heave_acceleration")?.value.unit}
                  </dd>
                </div>
                <div>
                  <dt>Quasi-static mooring tension</dt>
                  <dd>
                    {outputByName("mooring_tension") != null
                      ? (outputByName("mooring_tension")!.value.value / 1000).toFixed(1)
                      : "—"}{" "}
                    kN
                  </dd>
                </div>
                <div>
                  <dt>Heave natural period</dt>
                  <dd>
                    {outputByName("heave_natural_period")?.value.value.toFixed(2) ?? "—"} s
                  </dd>
                </div>
              </dl>

              {/* Check sheet table */}
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
              No motion evaluation has been run yet. Configure parameters and click "Run Motion
              Evaluation".
            </div>
          )}

          {/* Saved runs list for variant comparison */}
          <div className="design-project__motion-history">
            <h4>Saved Motion Runs ({runs.length})</h4>
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
    </section>
  );
}
