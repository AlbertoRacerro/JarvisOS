import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

// Spec 183: solver settings validation, op payloads and diagnostics text; wiring into the Setup drawer and run panel.
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const source = read("src/components/process/solverSettings.ts");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText;
const m = await import(`data:text/javascript;base64,${Buffer.from(js).toString("base64")}`);

const defaults = m.formFromSettings(null);
assert.deepEqual(m.validateForm(defaults), {});
assert.deepEqual(m.solverFromForm(defaults, null), {}, "untouched defaults send an empty object");
assert.deepEqual(m.solverFromForm({ ...defaults, method: "broyden", max_iterations: "80" }, null), { method: "broyden", max_iterations: 80 });
assert.deepEqual(m.solverFromForm({ ...defaults, method: "wegstein" }, { seeds: { R1: { mass_flow_kg_s: 1 } } }).seeds, { R1: { mass_flow_kg_s: 1 } }, "existing seeds are kept");

const bad = m.validateForm({ ...defaults, max_iterations: "5001", damping: "0.01", wall_s: "9", tolerances: { ...defaults.tolerances, mass_flow_rel: "1", culture_rel: "abc" } });
assert.deepEqual(Object.keys(bad).sort(), ["damping", "max_iterations", "tolerances.culture_rel", "tolerances.mass_flow_rel", "wall_s"]);
assert.ok(m.validateForm({ ...defaults, max_iterations: "2.5" }).max_iterations);
assert.deepEqual(m.validateForm({ ...defaults, max_iterations: "5000", damping: "0.05", wall_s: "600" }), {});

for (const reason of ["iteration_budget_insufficient", "acceleration_breakdown", "non_finite_evaluation", "max_iterations", "converged"]) {
  const text = m.stopReasonText(reason);
  assert.ok(text.length > 5 && !text.includes("undefined") && !text.includes("_"), reason);
}
assert.equal(m.stopReasonText(undefined), "not recorded");
assert.deepEqual(m.diagnosticRows(undefined), []);
// An absent or invalid estimate is the one word `empty`, never a number.
assert.deepEqual(m.diagnosticRows({}).find((r) => r.label === "Estimated fixed-point error")?.value, "empty");
assert.equal(m.diagnosticRows({}).find((r) => r.label === "q̂ (estimate)")?.value, "empty");
const rows = Object.fromEntries(m.diagnosticRows({ method: "direct_substitution", q_hat: 0.99941, classification: "near_neutral", estimated_error_normalized: 12.3456,
  estimate_valid: true, iterations: 25, max_iterations: 25, direct_substitution_iterations_required: 19000 }).map((r) => [r.label, r.value]));
assert.equal(rows["q̂ (estimate)"], "0.9994");
assert.equal(rows["Estimated fixed-point error"], "12.3 ×tol");
assert.equal(rows["Iterations"], "25 / 25");
assert.match(rows["Direct substitution would need"], /19,000/);
assert.equal(m.diagnosticRows({ q_hat: 0.5, estimate_valid: false, estimated_error_normalized: 3 }).find((r) => r.label === "Estimated fixed-point error").value, "empty");

const editor = read("src/stages/ProcessDraftEditor.tsx");
assert.match(editor, /<SolverSection solver=\{draft\.solver\} onApply=\{\(solver\) => apply\(\[\{ op: "set_solver", solver \}\]\)\} \/>/);
assert.match(editor, /Solver diagnostics/);
assert.match(editor, /diagnostics\?\.recommendation/);
const section = read("src/components/process/SolverSection.tsx");
assert.match(section, /Reset to defaults/);
assert.match(section, /Advanced tolerances/);
console.log("183 solver settings frontend ok");
