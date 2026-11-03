import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

// Spec 190: custom process units in the Studio wizard and the Process editor (palette, ports, inspector, results).
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const load = async (path, patch = (text) => text) => {
  const js = ts.transpileModule(patch(read(path)), { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(js).toString("base64")}`);
};
const logic = await load("src/components/process/studioUnitLogic.ts");
const authoring = await load("src/components/studio/unitLogic.ts", (text) => text.replace(/import \{ CULTURE_FIELDS[^\n]*\n/, 'const CULTURE_FIELDS = ["biomass", "nitrogen", "phosphorus", "oxygen", "dic", "salinity"];\n'));

const entry = {
  type: "StudioUnit", label: "Nutrient dosing", owner: "jarvis_bio", culture_rule: "studio", model_spec_id: "spec1", version_id: "v-1", version_label: "v1", version_number: 1,
  source_sha256: "a".repeat(64), contract_sha256: "b".repeat(64), inlets: ["culture", "dose"], inlet_labels: ["Culture feed", "Nutrient dose"], outlets: ["outlet", "unused_dose"],
  outlet_labels: ["Dosed culture", "Unused dose"], required_inlets: 2, params: [{ key: "dose_split", label: "Dose split", unit: "percent", description: "", minimum: 0, maximum: 100 }],
  symbol: null, culture_generation: [], limits: logic.STUDIO_LIMITS, steady_only: true
};
const placed = {
  id: "u1", kind: "unit", type: "StudioUnit", tag: "SU-1", x: 0, y: 0, params: {}, model: { version_id: "v-1", source_sha256: entry.source_sha256, contract_sha256: entry.contract_sha256 },
  studio: { model_spec_id: "spec1", name: "Nutrient dosing", version_label: "v1", version_number: 1, contract: {
    inlets: [{ name: "culture", label: "Culture feed" }, { name: "dose", label: "Nutrient dose" }], outlets: [{ name: "outlet", label: "Dosed culture" }, { name: "unused_dose", label: "Unused dose" }],
    parameters: [{ name: "dose_split", label: "Dose split", unit: "percent", description: "", min: 0, max: 100 }], symbol: null, culture_generation: [] } }
};

// Palette entry: the registry projection becomes a placement pinned to the published version.
assert.deepEqual(logic.paletteSpec(entry).inlets, ["culture", "dose"]);
assert.deepEqual(logic.paletteSpec(entry).outlets, ["outlet", "unused_dose"]);
assert.deepEqual(logic.addStudioUnitOp(entry, "u1", "SU-1", { x: 80, y: 90 }), { op: "add_unit", id: "u1", type: "StudioUnit", tag: "SU-1", version_id: "v-1", x: 80, y: 90 });

// Port rendering: ports come from the placed unit's pinned contract and carry the declared labels.
assert.deepEqual(logic.placedSpec(placed).inlets, ["culture", "dose"]);
assert.equal(logic.placedSpec(placed).required_inlets, 2);
assert.deepEqual(logic.portLabels(placed, "outlets"), ["Dosed culture (outlet)", "Unused dose (unused_dose)"]);
assert.equal(logic.placedSpec({ ...placed, type: "Heater" }), undefined);

// Inspector: parameter values leave in the declared unit; bad input is refused before any request.
assert.deepEqual(logic.parameterOp(placed, { dose_split: "35" }), { op: "set_unit_params", unit: "u1", values: { dose_split: { value: 35, unit: "percent" } } });
assert.equal(logic.parameterOp(placed, { dose_split: "abc" }), null);
assert.equal(logic.parameterOp(placed, { nope: "1" }), null);
assert.equal(logic.inspectHref(placed), "/design/studio?model=spec1");
assert.deepEqual(logic.updateStudioUnitOp(placed, { update_available: true, latest_version_id: "v-2", latest_version_number: 2 }), { op: "update_studio_unit", unit: "u1", version_id: "v-2" });
assert.equal(logic.updateStudioUnitOp(placed, { update_available: false, latest_version_id: null, latest_version_number: null }), null);
assert.equal(logic.STUDIO_LIMITS, "Steady only; no energy balance is checked; outlet temperature and pressure are set by the model; unqualified");

// Results: provenance tolerates absent optional fields and marks superseded runs.
const base = { version_id: "v-1", version_label: "v1", source_sha256: entry.source_sha256, contract_sha256: entry.contract_sha256, call_count: 5, runtime_classifications: ["ok"], duration_s: 0.5,
  version_number: 1, name: "Nutrient dosing", superseded: false, latest_version_number: 1, limits: logic.STUDIO_LIMITS };
const labels = (item) => logic.provenanceRows(item).map((row) => row.label);
assert.deepEqual(labels(base), ["Version", "Source SHA-256", "Contract SHA-256", "Calls", "Runtime", "Duration"]);
assert.deepEqual(labels({ ...base, cache_hits: 3, sandbox_seconds: 0.4 }).slice(-2), ["Cache hits", "Sandbox time"]);
assert.equal(logic.supersededText(base), null);
assert.match(logic.supersededText({ ...base, superseded: true, latest_version_number: 2 }), /Superseded: this run used v1; v2 is published/);

// Wizard: port and contract rules the backend would refuse are shown before Create.
const ok = { inlets: [{ name: "culture", label: "Culture" }], outlets: [{ name: "outlet", label: "Outlet" }], parameters: [] };
assert.deepEqual(authoring.contractIssues(ok), []);
assert.ok(authoring.contractIssues({ ...ok, inlets: [] }).length);
assert.ok(authoring.contractIssues({ ...ok, outlets: [{ name: "1bad", label: "x" }] }).length);
assert.ok(authoring.contractIssues({ ...ok, outlets: [{ name: "_bad", label: "x" }] }).length);
assert.ok(authoring.contractIssues({ ...ok, outlets: [{ name: "x".repeat(65), label: "x" }] }).length);
assert.ok(authoring.contractIssues({ ...ok, inlets: Array.from({ length: 5 }, (_, i) => ({ name: `p${i}`, label: "x" })) }).length);
assert.equal(authoring.blankUnitCase({ inlets: ok.inlets, parameters: [] }).tolerance, 1e-6);

// Source wiring: the editor uses the registry projection, the placed contract and the explicit actions.
const editor = read("src/stages/ProcessDraftEditor.tsx");
for (const text of ["registry.studio_units", "palette-studio-unit", "addStudioUnitOp", "placedSpec(unit) ?? unitSpec(unit.type)", "StudioUnitInspector", "resultsView.studio_units", "steady only"]) assert.ok(editor.includes(text), text);
assert.doesNotMatch(editor, /unitSpec\(\w+\.type\)\?\./, "unit ports must come from specOf, never from the type alone");
const inspector = read("src/components/process/StudioUnitInspector.tsx");
for (const text of ["Update to v", "Open model in Studio", "studio-failure", "studio-limits", "studio-superseded", "Apply inputs"]) assert.ok(inspector.includes(text), text);
const page = read("src/pages/Studio.tsx");
for (const text of ["PortForm", "SymbolField", "UnitCases", "UnitReport", "studio_symbol_invalid", "Symbol refused"]) assert.ok(page.includes(text) || read("src/components/studio/UnitAuthoring.tsx").includes(text), text);
assert.match(read("src/api/studio.ts"), /"PUT"/);
assert.doesNotMatch(read("src/api/studio.ts"), /"PATCH"/, "the backend CORS does not allow PATCH");
assert.match(read("package.json"), /npm run test:190/);
console.log("190 Studio process unit frontend ok");
