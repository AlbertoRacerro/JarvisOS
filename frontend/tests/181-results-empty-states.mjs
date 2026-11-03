import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { fieldLabel, firstSentence, outcomeHeadline, resultsLayout, streamLabel } from "../src/components/process/resultsLayout.ts";

// Results, run outcome and solver console: the same fields with or without a run, `empty` / `n/a` in the cells,
// the reason said once, streams named by their endpoints.
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");

const objects = [
  { id: "u1", kind: "unit", type: "Mixer", tag: "MIX-1", x: 0, y: 0 },
  { id: "u2", kind: "unit", type: "PhotobioreactorT1", tag: "PBR-1", x: 0, y: 0 },
  { id: "u3", kind: "unit", type: "SpecifiedSeparator", tag: "SEP-1", x: 0, y: 0 },
  { id: "s1", kind: "stream", type: "MaterialStream", tag: "FEED", x: 0, y: 0, source: null, target: { unit: "u1", port: 0 } },
  { id: "s2", kind: "stream", type: "MaterialStream", tag: "MIX_TO_PBR", x: 0, y: 0, source: { unit: "u1", port: 0 }, target: { unit: "u2", port: 0 } },
  { id: "s3", kind: "stream", type: "MaterialStream", tag: "PBR_TO_SEP", x: 0, y: 0, source: { unit: "u2", port: 0 }, target: { unit: "u3", port: 0 } },
  { id: "s4", kind: "stream", type: "MaterialStream", tag: "PRODUCT", x: 0, y: 0, source: { unit: "u3", port: 0 }, target: null },
  { id: "e1", kind: "stream", type: "EnergyStream", tag: "Q-1", x: 0, y: 0, source: null, target: { unit: "u1", port: 0 } },
];
const layout = resultsLayout(objects);
assert.deepEqual(layout.streams.map((s) => [s.tag, s.role, s.from, s.to]), [
  ["FEED", "feed", null, "MIX-1"], ["MIX_TO_PBR", "internal", "MIX-1", "PBR-1"],
  ["PBR_TO_SEP", "internal", "PBR-1", "SEP-1"], ["PRODUCT", "product", "SEP-1", null],
], "material streams only, sorted by tag, with endpoint unit tags");
assert.deepEqual(layout.units.find((u) => u.tag === "PBR-1"), { tag: "PBR-1", inlets: ["MIX_TO_PBR"], outlets: ["PBR_TO_SEP"] });

// KPI rows before a run mirror backend results_view.kpis: same ids in order, labels and definitions.
assert.deepEqual(layout.kpis.map((k) => k.id), [
  "biomass_product_rate", "pbr_volumetric_productivity:PBR-1", "pbr_net_biomass_production:PBR-1",
  "pbr_projected_area_productivity:PBR-1", "culture_throughput:PBR-1", "outlet_biomass_concentration:PRODUCT",
  "separator_biomass_recovery:SEP-1", "water_input", "nitrogen_input", "mass_balance_closure", "co2_feed", "co2_uptake", "co2_per_biomass",
]);
assert.ok(layout.kpis.every((k) => k.value === null && k.status === "unavailable"), "no KPI value before a run");
const backend = read("../backend/app/modules/process_stack/results_view.py").replace(/"\s*\n\s*"/g, "");
for (const kpi of layout.kpis) {
  assert.ok(backend.includes(kpi.definition), `backend defines KPI text: ${kpi.definition}`);
  assert.ok(backend.includes(`"${kpi.unit}"`), `backend uses KPI unit ${kpi.unit}`);
  const label = kpi.label.replace(/^(PBR-1|PRODUCT|SEP-1) /, "");
  assert.ok(backend.includes(label), `backend labels KPI: ${label}`);
}

// Streams by their endpoints; feeds and products, and unknown tags, keep the tag.
assert.equal(streamLabel(layout, "MIX_TO_PBR"), "MIX-1 → PBR-1");
assert.equal(streamLabel(layout, "FEED"), "FEED");
assert.equal(streamLabel(layout, "GONE"), "GONE");
assert.equal(fieldLabel("mass_flow_kg_s"), "mass flow");
assert.equal(fieldLabel("temperature_K"), "temperature");
assert.equal(fieldLabel("mass_fraction.Water"), "Water mass fraction");
assert.equal(fieldLabel("biomass"), "biomass");

// The outcome headline: one line, first sentence of the error, failing stream by its endpoints, no machine codes.
const longError = "System.Exception: Temperature 2633.2 K is outside the IAPWS-IF97 range 273.15-1073.15 K. At DWSIM.Thermodynamics.PropertyPackages...\n   at frame 2";
assert.equal(firstSentence(longError), "Temperature 2633.2 K is outside the IAPWS-IF97 range 273.15-1073.15 K.");
assert.ok(firstSentence("x".repeat(400)).length <= 160);
const words = (code) => code.replace(/_/g, " ");
const failed = { state: "failed", label: "Failed", reason: "segment_failed", message: longError, residual: 17401, iterations: 5,
  worst_tear: "MIX_TO_PBR", results_available: "none", artifact: null, failing: [{ tag: "PBR-1", stage: "mixed-6-0", error: longError }] };
const headline = outcomeHeadline(failed, (tag) => streamLabel(layout, tag), words);
assert.equal(headline, "Failed at PBR-1: Temperature 2633.2 K is outside the IAPWS-IF97 range 273.15-1073.15 K.");
assert.doesNotMatch(headline, /segment_failed|mixed-6-0|System\.Exception/);
assert.equal(outcomeHeadline({ ...failed, failing: [{ tag: "MIX_TO_PBR", stage: null, error: "MIX_TO_PBR: bad state" }] }, (tag) => streamLabel(layout, tag), words),
  "Failed at MIX-1 → PBR-1: bad state");
// A unit failing on a stream: the engine's raw stream-tag prefix is named by the stream's endpoints, not repeated.
assert.equal(outcomeHeadline({ ...failed, failing: [{ tag: "PBR-1", stage: null, error: "MIX_TO_PBR: bad state" }] }, (tag) => streamLabel(layout, tag), words),
  "Failed at PBR-1, stream MIX-1 → PBR-1: bad state");
assert.equal(outcomeHeadline({ ...failed, state: "non_converged", label: "Not converged", reason: "max_iterations", failing: [] }, (t) => t, words),
  "Not converged: max iterations");
assert.equal(outcomeHeadline({ ...failed, state: "converged", label: "Converged", failing: [] }, (t) => t, words), "Converged");

// Source contracts: one status line per table, `empty` / `n/a` cells, the error once with Details folded.
const runResults = read("src/components/process/RunResults.tsx");
const consoleSource = read("src/components/process/SolverConsole.tsx");
const editor = read("src/stages/ProcessDraftEditor.tsx");
for (const text of ["No run yet", 'word={notModelled(kpi) ? "n/a" : "empty"}', "n/a: not modelled", "<summary>Details</summary>", "results-status__dot"])
  assert.ok(runResults.includes(text), `RunResults keeps ${text}`);
for (const gone of ["unavailable: ", "last iterate only — not a solution", "No stream values", "This unit reported no quantities", "No calculation state reported"])
  assert.ok(!runResults.includes(gone), `RunResults no longer says ${gone}`);
assert.doesNotMatch(runResults, /kpi\.status\.replace\(\/_\/g, " "\)\}: \{kpi\.reason\}/, "no per-row KPI reason");
for (const gone of ["not yet estimated", "not valid yet", "is not observable, so no live residuals"])
  assert.ok(!consoleSource.includes(gone), `console no longer says ${gone}`);
assert.match(consoleSource, /streamName\(latest\.worst_tear\)/);
assert.match(consoleSource, /fieldLabel\(latest\.worst_field\)/);
assert.doesNotMatch(editor, /No results yet\. Run compiles/);
assert.doesNotMatch(editor, /No run yet\. Press Run/);
assert.match(editor, /<OutcomeDetails outcome=\{outcome\} streamName=\{streamName\} details=\{engineOutput\} \/>/);
assert.match(editor, /\{!outcome && engineOutput\}/, "engine text is shown directly only without an outcome");
assert.doesNotMatch(editor, /status \{run\.status\.replace/, "the run header carries no machine status");

console.log("181 results empty states: PASS");
