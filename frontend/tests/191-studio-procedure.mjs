import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

// Spec 191: procedure authoring in Studio (wizard, step editor, test bench).
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const js = ts.transpileModule(read("src/components/studio/procedureLogic.ts"), { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText;
const L = await import(`data:text/javascript;base64,${Buffer.from(js).toString("base64")}`);

// Honest limits text is the contract's, verbatim.
assert.equal(L.NOTICE, "Simulation only. Actions change modelled tank inventories. This does not command any equipment. Unqualified.");

// Human time units convert to seconds exactly and read back in the largest exact unit.
assert.equal(L.toSeconds(48, "h"), 172800);
assert.equal(L.toSeconds(2, "d"), 172800);
assert.deepEqual(L.fromSeconds(172800), { value: 2, unit: "d" });
assert.deepEqual(L.fromSeconds(5400), { value: 90, unit: "min" });
assert.deepEqual(L.fromSeconds(7), { value: 7, unit: "s" });

// Changing the display unit must leave the stored seconds untouched for every time field.
const timeField = read("src/components/studio/ProcedureAuthoring.tsx");
const unitSelector = timeField.match(/<select aria-label=\{`\$\{label\} unit`\}[^>]*onChange=\{([^}]*)\}/)?.[1];
assert.ok(unitSelector, "time fields expose a unit selector");
assert.match(unitSelector, /setTimeUnit/);
assert.doesNotMatch(unitSelector, /\bonChange\s*\(/, "switching the display unit must not rewrite stored seconds");
assert.equal(L.toSeconds(48, "h") / 86400, 2, "48 hours displays as 2 days");

// The action forms are exactly the five 185 shapes, with exclusive fields where the contract has them.
assert.deepEqual(L.ACTION_TYPES, ["draw", "separate", "refill", "dose", "inoculate"]);
assert.deepEqual(L.ACTIONS.draw.choice.map((x) => x.path[0]), ["volume_m3", "loop_fraction"]);
assert.deepEqual(L.ACTIONS.refill.choice.map((x) => x.path[0]), ["volume_m3", "to_volume_m3"]);
assert.deepEqual(L.ACTIONS.separate.fields.map((x) => x.path[0]), ["recovery", "concentration_factor"]);
assert.deepEqual(L.ACTIONS.inoculate.fields.map((x) => x.path.join(".")), ["volume_m3", "culture.X", "culture.N", "culture.O2"]);
assert.equal(L.ACTIONS.dose.fields.find((x) => x.path[0] === "volume_m3").optional, true);

// Parameter binding is {"$param": name} and nested paths set and clear without leaving placeholders.
let draw = L.setPath({ type: "draw", tank: "TankA" }, ["loop_fraction"], L.bindParam("draw_fraction"));
assert.deepEqual(draw, { type: "draw", tank: "TankA", loop_fraction: { $param: "draw_fraction" } });
assert.equal(L.isRef(draw.loop_fraction), true);
assert.equal(L.isRef(0.2), false);
assert.deepEqual(L.setPath(draw, ["loop_fraction"], undefined), { type: "draw", tank: "TankA" });
const refill = L.setPath(L.setPath({ type: "refill" }, ["medium", "N"], 0.5), ["medium", "O2"], 0.008);
assert.deepEqual(refill.medium, { N: 0.5, O2: 0.008 });
assert.equal("medium" in L.setPath(L.setPath(refill, ["medium", "N"], undefined), ["medium", "O2"], undefined), false);
assert.deepEqual(L.changeActionType({ type: "draw", tank: "TankA", loop_fraction: 0.2 }, "dose"), { type: "dose", tank: "TankA" });
assert.deepEqual(L.changeActionType({ type: "draw", tank: "TankA" }, "separate"), { type: "separate" });

// Recurrence and condition are mutually exclusive: switching kind removes the other kind's fields.
const step = { label: "Weekly", time_s: 172800, every_s: 172800, count: 10, actions: [{ type: "draw" }] };
assert.equal(L.stepKind(step), "recurring");
const conditioned = L.setStepKind(step, "condition");
assert.equal(L.stepKind(conditioned), "condition");
for (const key of ["every_s", "count", "end_s"]) assert.equal(key in conditioned, false);
assert.deepEqual(conditioned.condition.max_fires, 1);
const back = L.setStepKind(conditioned, "recurring");
assert.equal("condition" in back, false);
assert.equal(L.stepKind(L.setStepKind(back, "once")), "once");
assert.deepEqual(L.splitObserved("PBR.loop_N"), { unit: "PBR", channel: "loop_N" });
assert.equal(L.joinObserved("PBR", "X"), "PBR.X");
assert.deepEqual([...L.OBSERVED_CHANNELS], ["X", "N", "O2", "loop_X", "loop_N"]);

// Client hints name what the backend would refuse, and never edit the steps.
const params = [{ name: "draw_fraction", label: "Draw", unit: "percent", description: "", min: 0, max: 100 }];
const good = [{ label: "Draw", time_s: 0, actions: [{ type: "draw", tank: "TankA", loop_fraction: { $param: "draw_fraction" } }] }];
assert.deepEqual(L.stepIssues(good, params), []);
assert.ok(L.stepIssues([{ ...good[0], actions: [{ type: "draw", tank: "TankA", volume_m3: 1, loop_fraction: 0.2 }] }], params).some((x) => /exactly one/.test(x)));
assert.ok(L.stepIssues([{ ...good[0], actions: [{ type: "draw", tank: "TankA", loop_fraction: { $param: "nope" } }] }], params).some((x) => /not declared/.test(x)));
assert.ok(L.stepIssues([{ ...good[0], every_s: 1, condition: L.blankCondition() }], params).some((x) => /cannot also recur/.test(x)));
assert.ok(L.stepIssues([{ ...good[0], actions: [] }], params).length);
assert.ok(L.stepIssues([{ ...good[0], label: " " }], params).length);
assert.deepEqual(good[0].actions[0], { type: "draw", tank: "TankA", loop_fraction: { $param: "draw_fraction" } });
assert.ok(L.parameterValuesIssues(params, {}).length);
assert.deepEqual(L.parameterValuesIssues(params, { draw_fraction: 20 }), []);

// Test cases start with a value per parameter; publish needs a passing report bound to the exact hashes.
assert.deepEqual(L.blankCase(params, "d1", "run").parameter_values, { draw_fraction: 50 });
const report = { passed: true, source_sha256: "s", contract_sha256: "c", cases: [{ name: "base", passed: true }] };
const model = { status: "draft", source_sha256: "s", contract_sha256: "c", last_test_report: report };
assert.equal(L.canPublishProcedure(model, false), true);
assert.equal(L.canPublishProcedure(model, true), false);
assert.equal(L.canPublishProcedure({ ...model, status: "published" }, false), false);
assert.equal(L.canPublishProcedure({ ...model, last_test_report: { ...report, passed: false } }, false), false);
assert.equal(L.canPublishProcedure({ ...model, last_test_report: { ...report, source_sha256: "old" } }, false), false);
assert.equal(L.canPublishProcedure({ ...model, last_test_report: { ...report, cases: [] } }, false), false);
assert.equal(L.canPublishProcedure({ ...model, last_test_report: null }, false), false);

// Bench: pin state, hand edit and newer version are all shown; a modified or missing link cannot run.
const link = { id: "l1", state: "applied", state_text: "Applied", version_label: "Cycle v1", latest_version_label: "Cycle v1", update_available: false };
assert.equal(L.canRun(link), true);
assert.equal(L.linkBadges(link).length, 1);
assert.equal(L.updateLabel(link), null);
const superseded = { ...link, update_available: true, latest_version_label: "Cycle v2" };
assert.equal(L.updateLabel(superseded), "Update to v2");
assert.match(L.linkBadges(superseded)[1].text, /Superseded: Cycle v2 is published.*stays on Cycle v1 until you choose Update to v2\./);
const modified = { ...link, state: "modified" };
assert.equal(L.canRun(modified), false);
assert.match(L.linkBadges(modified)[0].text, /Modified, no longer the pinned procedure\./);
assert.equal(L.canRun({ ...link, state: "missing" }), false);
assert.equal(L.canRun(null), false);

// Results: balance status comes from the event records and the run status.
const results = { status: "succeeded", balances: { aggregate: { biomass: { residual_abs_kg: 0, residual_rel: 1e-12 } } }, event_log: [{ type: "actions", balance: { closed: true } }, { type: "actions", balance: { closed: true } }] };
assert.equal(L.balanceStatus(results).closed, true);
assert.equal(L.balanceStatus({ ...results, event_log: [...results.event_log, { type: "actions", balance: { closed: false } }] }).closed, false);
assert.equal(L.balanceStatus({ ...results, status: "failed" }).closed, false);
assert.equal(L.chartPaths([0, 1], [{ name: "a", values: [0, 1] }], 100, 10)[0].d, "M0.0,10.0L100.0,0.0");
assert.deepEqual(L.chartPaths([0], [{ name: "a", values: [0] }], 100, 10), []);

// Source wiring: wizard, step editor, test and publish reuse the 188 lifecycle; the notice and typed codes are on screen.
const page = read("src/pages/Studio.tsx");
for (const text of ["New procedure", "Create procedure draft", "StepEditor", "ProcedureCases", "ProcedureReportView", "ProcedureBench", "canPublishProcedure", "tabTitle", "Test bench", "Refusal code={errorCode}", "Procedure · apply it from its Test bench", "category: \"procedure\""]) assert.ok(page.includes(text), text);
assert.ok(/available: true \},\n\s*\{ id: "geometry"/.test(page) || page.includes('id: "procedure", label: "Procedure", available: true'), "procedure category is available");
const authoring = read("src/components/studio/ProcedureAuthoring.tsx");
for (const text of ["SimulationNotice", "data-refusal-code", "Bind ", "Repeats every...", "When a condition holds", "Observed unit", "Hysteresis", "Cooldown", "Max fires", "Compiled schedule SHA-256", "Move up", "+ Add action", "+ Add step", "Also simulate the scenario"]) assert.ok(authoring.includes(text), text);
const bench = read("src/components/studio/ProcedureBench.tsx");
for (const text of ["Apply", "Run", "updateLabel", "Create a minimal scenario", "studio-link-provenance", "Apply anyway (overwrite)", "SimulationNotice", "View results", "RunResultsView"]) assert.ok(bench.includes(text), text);
const sharedResults = read("src/components/dynamic/RunResultsView.tsx");
for (const text of ["Per-cycle KPIs", "Event log", "Balances:", "SimulationNotice", "getDynamicSeries"]) assert.ok(sharedResults.includes(text), text);
assert.equal((bench.match(/<SimulationNotice \/>/g) ?? []).length >= 3, true, "notice on bench, apply and link; shared results carries its own notice");
const api = read("src/api/studioProcedure.ts");
assert.ok(api.includes("/procedure/apply") === false && api.includes("/apply`"), "apply route");
assert.doesNotMatch(read("src/api/studio.ts") + api, /"PATCH"/, "the backend CORS does not allow PATCH");
assert.doesNotMatch(api + authoring + bench, /dangerouslySetInnerHTML|eval\(/);
assert.match(read("package.json"), /npm run test:191/);
// HTML pattern attributes are compiled with the unicode-sets flag: an unescaped "-" in a class is a console error and disables the check.
for (const file of ["src/components/studio/ProcedureAuthoring.tsx", "src/components/studio/ProcedureBench.tsx"]) {
  for (const [, pattern] of read(file).matchAll(/pattern="([^"]*)"/g)) assert.doesNotThrow(() => new RegExp(pattern, "v"), `${file} pattern ${pattern}`);
}

console.log("191 Studio procedure frontend ok");
