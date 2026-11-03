// Spec 216 contract 8: reduced-model status labels and Process-pin visibility on the Design component page.
import assert from "node:assert/strict";
import fs from "node:fs";
import { canPinToProcess, OBSOLETE_TEXT, reducedModelStatusLabel, segmentValidityText, showsReasons }
  from "../src/pages/reducedModelStatus.ts";

assert.equal(OBSOLETE_TEXT, "Reduced model obsolete, rerun the study");
assert.equal(reducedModelStatusLabel("obsolete"), "Reduced model obsolete, rerun the study");
assert.equal(reducedModelStatusLabel("current"), "Current");
assert.match(reducedModelStatusLabel("unknown"), /unknown/i);
assert.match(reducedModelStatusLabel("invalid"), /invalid/i);

assert.equal(canPinToProcess({ status: "current", process_unit_pinned: false }), true);
assert.equal(canPinToProcess({ status: "current", process_unit_pinned: true }), false);
for (const status of ["obsolete", "unknown", "invalid"]) assert.equal(canPinToProcess({ status, process_unit_pinned: false }), false, status);

assert.equal(showsReasons({ status: "obsolete", obsolete_reasons: ["geometry changed"] }), true);
assert.equal(showsReasons({ status: "invalid", obsolete_reasons: ["bad"] }), true);
assert.equal(showsReasons({ status: "current", obsolete_reasons: [] }), false);

assert.equal(segmentValidityText({ regime: "turbulent", reynolds_min: 4000, reynolds_max: 50000, diameter_m: 0.05,
  density_kg_m3: 1000, dynamic_viscosity_pa_s: 0.001, turbulence_model: null, wall_treatment: "x",
  combined_uncertainty: 0.062, reference: "r" }), "turbulent · Re 4000–50000 · D 50 mm · uncertainty 6.2 %");

// The pin is never issued on load: it is only reachable from the explicit button handler.
const panel = fs.readFileSync(new URL("../src/pages/ReducedModelsPanel.tsx", import.meta.url), "utf8");
assert.equal((panel.match(/pinReducedModelToProcess\(/g) ?? []).length, 1);
assert.ok(!/useEffect\([^)]*pinReducedModelToProcess/.test(panel));
console.log("216 reduced-model status: ok");
