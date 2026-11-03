// Spec 183: tear solver settings model, client-side validation (mirrors backend tear_solver.parse_settings) and
// readable text for solver stop reasons and classifications.
export type SolverMethod = "direct_substitution" | "wegstein" | "broyden";
export type SeedMode = "legacy" | "feed";
export type SolverSettings = {
  method?: SolverMethod; max_iterations?: number; damping?: number; wall_s?: number; seed_mode?: SeedMode;
  tolerances?: Record<string, number>; [key: string]: unknown;
};

export const SOLVER_METHODS: { value: SolverMethod; label: string }[] = [
  { value: "direct_substitution", label: "Direct substitution" },
  { value: "wegstein", label: "Wegstein" },
  { value: "broyden", label: "Broyden" },
];
export const SEED_MODES: { value: SeedMode; label: string }[] = [
  { value: "legacy", label: "Legacy (tiny flow, zero culture)" },
  { value: "feed", label: "Feed (feed flow and culture)" },
];
export const SOLVER_DEFAULTS = { method: "direct_substitution" as SolverMethod, max_iterations: 25, damping: 1, wall_s: 90, seed_mode: "legacy" as SeedMode };
export const TOLERANCE_FIELDS: { key: string; label: string; low: number; high: number }[] = [
  { key: "mass_flow_rel", label: "Mass flow (relative)", low: 1e-10, high: 1e-2 },
  { key: "temperature_abs", label: "Temperature (absolute, K)", low: 1e-6, high: 1 },
  { key: "pressure_rel", label: "Pressure (relative)", low: 1e-12, high: 1e-2 },
  { key: "mass_fraction_abs", label: "Mass fraction (absolute)", low: 1e-12, high: 1e-3 },
  { key: "culture_rel", label: "Culture (relative)", low: 1e-10, high: 1e-2 },
];

export type SolverForm = {
  method: string; max_iterations: string; damping: string; wall_s: string; seed_mode: string; tolerances: Record<string, string>;
};

export const formFromSettings = (solver?: SolverSettings | null): SolverForm => ({
  method: solver?.method ?? SOLVER_DEFAULTS.method,
  max_iterations: String(solver?.max_iterations ?? SOLVER_DEFAULTS.max_iterations),
  damping: String(solver?.damping ?? SOLVER_DEFAULTS.damping),
  wall_s: String(solver?.wall_s ?? SOLVER_DEFAULTS.wall_s),
  seed_mode: solver?.seed_mode ?? SOLVER_DEFAULTS.seed_mode,
  tolerances: Object.fromEntries(TOLERANCE_FIELDS.map((field) => [field.key, solver?.tolerances?.[field.key] === undefined ? "" : String(solver.tolerances[field.key])])),
});

const parse = (text: string) => (text.trim() === "" ? NaN : Number(text));

/** Inline errors keyed by field ("tolerances.<key>" for tolerances); empty when the form is valid. */
export function validateForm(form: SolverForm): Record<string, string> {
  const errors: Record<string, string> = {};
  const iterations = parse(form.max_iterations);
  if (!Number.isInteger(iterations) || iterations < 1 || iterations > 5000) errors.max_iterations = "Whole number from 1 to 5000.";
  const damping = parse(form.damping);
  if (!Number.isFinite(damping) || damping < 0.05 || damping > 1) errors.damping = "Number from 0.05 to 1.";
  const wall = parse(form.wall_s);
  if (!Number.isFinite(wall) || wall < 10 || wall > 600) errors.wall_s = "Seconds from 10 to 600.";
  if (!SOLVER_METHODS.some((item) => item.value === form.method)) errors.method = "Choose a method.";
  if (!SEED_MODES.some((item) => item.value === form.seed_mode)) errors.seed_mode = "Choose a seed mode.";
  for (const field of TOLERANCE_FIELDS) {
    const text = form.tolerances[field.key] ?? "";
    if (text.trim() === "") continue;
    const value = Number(text);
    if (!Number.isFinite(value) || value < field.low || value > field.high) errors[`tolerances.${field.key}`] = `Number from ${field.low.toExponential(0)} to ${field.high.toExponential(0)}.`;
  }
  return errors;
}

/** The solver object to send: the document's existing settings (seeds, bounds) with the form's values on top.
 *  Fields left at their default are only written when already stored, so an untouched form stays minimal. */
export function solverFromForm(form: SolverForm, existing?: SolverSettings | null): SolverSettings {
  const next: SolverSettings = { ...(existing ?? {}) };
  const set = (key: "method" | "max_iterations" | "damping" | "wall_s" | "seed_mode", value: string | number) => {
    if (value !== SOLVER_DEFAULTS[key] || key in next) (next as Record<string, unknown>)[key] = value;
  };
  set("method", form.method);
  set("max_iterations", Number(form.max_iterations));
  set("damping", Number(form.damping));
  set("wall_s", Number(form.wall_s));
  set("seed_mode", form.seed_mode);
  const tolerances: Record<string, number> = {};
  for (const field of TOLERANCE_FIELDS) {
    const text = form.tolerances[field.key] ?? "";
    if (text.trim() !== "") tolerances[field.key] = Number(text);
  }
  if (Object.keys(tolerances).length) next.tolerances = tolerances; else delete next.tolerances;
  return next;
}

const STOP_REASONS: Record<string, string> = {
  converged: "Converged",
  max_iterations: "Iteration limit reached",
  iteration_budget_insufficient: "Iteration budget insufficient: at the estimated convergence rate the loop cannot converge within the remaining iterations",
  acceleration_breakdown: "Acceleration breakdown: the accelerated step could not make progress",
  non_finite_evaluation: "Non-finite evaluation: the flowsheet returned a value that is not a finite number",
  damping_exhausted: "Damping exhausted without improvement",
  wall_budget: "Wall-clock budget exhausted",
  balance: "Mass balance not closed",
  light_full_mismatch: "Light and full solves disagree",
  segment_failed: "A DWSIM segment failed",
  validation_failed: "Validation failed",
  full_path_failed: "Full-path solve failed",
};
export const stopReasonText = (reason?: string | null) =>
  !reason ? "not recorded" : STOP_REASONS[reason] ?? reason.replace(/_/g, " ");

export const methodLabel = (method?: string | null) =>
  !method ? "not recorded" : SOLVER_METHODS.find((item) => item.value === method)?.label ?? method.replace(/_/g, " ");

const CLASSES: Record<string, string> = {
  fast: "fast", moderate: "moderate", slow: "slow", near_neutral: "near neutral (very slow)",
  oscillatory: "oscillatory", non_contractive: "non-contractive (diverging)", unknown: "unknown",
};
export const classificationText = (value?: string | null) => (!value ? "unknown" : CLASSES[value] ?? value.replace(/_/g, " "));

export type SolveDiagnostics = {
  method?: string; q_hat?: number | null; classification?: string; estimated_error_normalized?: number | null;
  estimate_valid?: boolean; iterations?: number; max_iterations?: number; growth_events?: number; acceleration_resets?: number;
  direct_substitution_iterations_required?: number; recommendation?: string;
};
const num = (value: unknown) => typeof value === "number" && Number.isFinite(value);

/**
 * Label/value rows for the convergence panel; absent diagnostics (older run records) yield no rows. A value the
 * solver did not estimate, or estimated without a valid error bound, is `empty`, never a number.
 */
export function diagnosticRows(diagnostics?: SolveDiagnostics | null): { label: string; value: string }[] {
  if (!diagnostics || typeof diagnostics !== "object") return [];
  const rows: { label: string; value: string }[] = [];
  if (diagnostics.method) rows.push({ label: "Method", value: methodLabel(diagnostics.method) });
  rows.push({ label: "q̂ (estimate)", value: num(diagnostics.q_hat) ? (diagnostics.q_hat as number).toFixed(4) : "empty" });
  rows.push({ label: "Classification", value: classificationText(diagnostics.classification) });
  rows.push({
    label: "Estimated fixed-point error",
    value: diagnostics.estimate_valid && num(diagnostics.estimated_error_normalized)
      ? `${Number(diagnostics.estimated_error_normalized).toPrecision(3)} ×tol` : "empty",
  });
  if (num(diagnostics.iterations)) rows.push({ label: "Iterations", value: `${diagnostics.iterations}${num(diagnostics.max_iterations) ? ` / ${diagnostics.max_iterations}` : ""}` });
  if (num(diagnostics.direct_substitution_iterations_required)) rows.push({ label: "Direct substitution would need", value: `about ${Math.round(diagnostics.direct_substitution_iterations_required as number).toLocaleString("en-US")} iterations` });
  return rows;
}
