import { useEffect, useState } from "react";
import { SEED_MODES, SOLVER_DEFAULTS, SOLVER_METHODS, TOLERANCE_FIELDS, formFromSettings, solverFromForm, validateForm,
  type SolverForm, type SolverSettings } from "./solverSettings";

import NumericInput from "../ui/NumericInput";
/** Spec 183: Solver section of the Process Setup drawer. `solver` is the document's stored settings (absent = defaults). */
export function SolverSection({ solver, onApply }: Readonly<{ solver?: SolverSettings | null; onApply: (solver: SolverSettings | null) => unknown }>) {
  const stored = JSON.stringify(solver ?? null);
  const [form, setForm] = useState<SolverForm>(() => formFromSettings(solver));
  const [showErrors, setShowErrors] = useState(false);
  useEffect(() => { setForm(formFromSettings(solver)); setShowErrors(false); }, [stored]); // eslint-disable-line react-hooks/exhaustive-deps
  const errors = validateForm(form);
  const set = (patch: Partial<SolverForm>) => setForm((current) => ({ ...current, ...patch }));
  const error = (key: string) => (showErrors && errors[key] ? <small className="draft-field-error" role="alert" id={`solver-err-${key}`}>{errors[key]}</small> : null);
  const aria = (key: string) => (showErrors && errors[key] ? { "aria-invalid": true, "aria-describedby": `solver-err-${key}` } : {});
  const apply = () => {
    setShowErrors(true);
    if (Object.keys(errors).length) return;
    void onApply(solverFromForm(form, solver));
  };
  return (
    <fieldset className="draft-fieldset draft-solver" aria-label="Solver">
      <legend>Settings for the next run</legend>
      <p className="draft-hint">How the recycle loop is solved. Change a value, press Apply, then Run. Defaults while nothing is set: {SOLVER_DEFAULTS.max_iterations} iterations, damping {SOLVER_DEFAULTS.damping}, {SOLVER_DEFAULTS.wall_s} s.</p>
      <label className="draft-field"><span>Method</span>
        <select aria-label="Solver method" value={form.method} onChange={(event) => set({ method: event.target.value })} {...aria("method")}>
          {SOLVER_METHODS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>{error("method")}</label>
      <label className="draft-field"><span>Max iterations (1 to 5000)</span>
        <NumericInput integer aria-label="Max iterations" min={1} max={5000} value={form.max_iterations}
          onValueChange={(text) => set({ max_iterations: text })} {...aria("max_iterations")} />{error("max_iterations")}</label>
      <label className="draft-field"><span>Damping (0.05 to 1)</span>
        <NumericInput aria-label="Damping" min={0.05} max={1} value={form.damping}
          onValueChange={(text) => set({ damping: text })} {...aria("damping")} />{error("damping")}</label>
      <label className="draft-field"><span>Wall budget, seconds (10 to 600)</span>
        <NumericInput aria-label="Wall budget seconds" min={10} max={600} value={form.wall_s}
          onValueChange={(text) => set({ wall_s: text })} {...aria("wall_s")} />{error("wall_s")}</label>
      <label className="draft-field"><span>Seed mode</span>
        <select aria-label="Seed mode" value={form.seed_mode} onChange={(event) => set({ seed_mode: event.target.value })} {...aria("seed_mode")}>
          {SEED_MODES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>{error("seed_mode")}</label>
      <details className="draft-solver-advanced">
        <summary>Advanced tolerances</summary>
        <p className="draft-hint">Leave blank to keep the default tolerance.</p>
        {TOLERANCE_FIELDS.map((field) => (
          <label className="draft-field" key={field.key}><span>{field.label}</span>
            <input aria-label={`Tolerance ${field.key}`} type="text" inputMode="decimal" placeholder={`default (${field.low.toExponential(0)} to ${field.high.toExponential(0)})`}
              value={form.tolerances[field.key] ?? ""} onChange={(event) => set({ tolerances: { ...form.tolerances, [field.key]: event.target.value } })}
              {...aria(`tolerances.${field.key}`)} />{error(`tolerances.${field.key}`)}</label>
        ))}
      </details>
      <div className="draft-solver-actions">
        <button type="button" onClick={apply}>Apply solver settings</button>
        <button type="button" onClick={() => void onApply(null)} disabled={solver == null}>Reset to defaults</button>
      </div>
    </fieldset>
  );
}
