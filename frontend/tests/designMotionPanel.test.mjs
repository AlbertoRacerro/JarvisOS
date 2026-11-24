import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

// 1. Stage 1: DesignMotionPanel & DesignProject
const motionPanel = read("src/pages/DesignMotionPanel.tsx");
const designProject = read("src/pages/DesignProject.tsx");
const designTargets = read("src/app/designTargets.ts");

assert.ok(motionPanel.includes('marine.rigid_body_waves'), "DesignMotionPanel must be backed by marine.rigid_body_waves");
assert.ok(motionPanel.includes('design.panel.mechanics'), "DesignMotionPanel must declare design.panel.mechanics target");
assert.ok(motionPanel.includes('heave_rao') && motionPanel.includes('surge_rao') && motionPanel.includes('pitch_rao'), "DesignMotionPanel must display heave, surge, and pitch RAOs");
assert.ok(motionPanel.includes('heave_acceleration'), "DesignMotionPanel must display max acceleration");
assert.ok(motionPanel.includes('mooring_tension'), "DesignMotionPanel must display quasi-static mooring tension");
assert.ok(motionPanel.includes('Check Sheet'), "DesignMotionPanel must include check sheet");
assert.ok(motionPanel.includes('Saved Motion Runs'), "DesignMotionPanel must list saved motion runs");

// Verify DesignProject has removed placeholder and renders live panel
assert.ok(!designProject.includes('"Motion/Mechanics (placeholder)"'), "DesignProject must not contain placeholder suffix");
assert.ok(!designProject.includes("Motion/Mechanics is a placeholder"), "DesignProject must not contain placeholder help message");
assert.ok(designProject.includes("<DesignMotionPanel"), "DesignProject must render DesignMotionPanel");
assert.ok(designTargets.includes('"design.stage.mechanics": { role: "button", name: "Motion/Mechanics"'), "Target registration must name Motion/Mechanics without placeholder");

// 2. Stage 2: Axial O2 (tube) in DesignDynamicPanel and Plant link
const axialPanel = read("src/components/design/AxialOxygenPanel.tsx");
const dynamicPanel = read("src/pages/DesignDynamicPanel.tsx");

assert.ok(axialPanel.includes('bioprocess.tube_oxygen_buildup'), "AxialOxygenPanel must be backed by bioprocess.tube_oxygen_buildup");
assert.ok(axialPanel.includes('photobioreactor.tube_oxygen'), "AxialOxygenPanel must reference photobioreactor.tube_oxygen");
assert.ok(axialPanel.includes('outlet_concentration'), "AxialOxygenPanel must display outlet O2 concentration");
assert.ok(axialPanel.includes('outlet_saturation_fraction'), "AxialOxygenPanel must display outlet saturation ratio");
assert.ok(axialPanel.includes('max_length'), "AxialOxygenPanel must display max safe run length");
assert.ok(axialPanel.includes('Saved Axial O₂ Runs'), "AxialOxygenPanel must list saved runs for comparison");
assert.ok(dynamicPanel.includes("<AxialOxygenPanel"), "DesignDynamicPanel must host AxialOxygenPanel");
assert.ok(designProject.includes("Axial O₂ (tube)"), "Plant stage must provide link to Axial O2 for tubular units");

// 3. Stage 3: physicsHandoff helper & variant comparison
const handoff = read("src/components/design/physicsHandoff.ts");
assert.ok(handoff.includes("deriveMotionDefaults"), "physicsHandoff must export deriveMotionDefaults");
assert.ok(handoff.includes("deriveAxialO2Defaults"), "physicsHandoff must export deriveAxialO2Defaults");
assert.ok(handoff.includes("buildVariantStageSummary"), "physicsHandoff must export buildVariantStageSummary");
assert.ok(handoff.includes('"from Process"') && handoff.includes('"from Environment"') && handoff.includes('"manual"'), "physicsHandoff must define provenance labels");
assert.ok(designProject.includes("Variant stage verdicts") && designProject.includes("buildVariantStageSummary"), "DesignProject must render variant stage verdicts summary");

// Test pure physics handoff logic
const { deriveMotionDefaults, deriveAxialO2Defaults, buildVariantStageSummary } = await import("../src/components/design/physicsHandoff.ts");

// Provenance logic for motion defaults
const envProfile = { profile_id: "p1", digest: "d1", name: "Offshore", channels: { wave_height: "m", wave_period: "s" }, start: "", end: "", resolution_minutes: 60, provenance: {}, parent_digest: null };
const envValues = { ...envProfile, timestamps: [], channels: { wave_height: [2.4], wave_period: [7.5] }, units: {}, total: 1, indices: [0], offset: 0 };
const motionDefaults = deriveMotionDefaults(envProfile, envValues, null, {});
assert.equal(motionDefaults.wave_amplitude_m.value, 1.2, "wave amplitude derived from wave_height / 2");
assert.equal(motionDefaults.wave_amplitude_m.source, "from Environment", "provenance must be from Environment");
assert.equal(motionDefaults.wave_period_s.value, 7.5, "wave period from environment");
assert.equal(motionDefaults.wave_period_s.source, "from Environment");

// User edit preservation
const editedMotion = deriveMotionDefaults(envProfile, envValues, null, { wave_amplitude_m: 3.0 });
assert.equal(editedMotion.wave_amplitude_m.value, 3.0, "user edit preserved");
assert.equal(editedMotion.wave_amplitude_m.source, "manual", "user edit provenance must be manual");

// Axial O2 defaults
const mockUnit = { id: "u1", kind: "unit", type: "PhotobioreactorT1", tag: "PBR-01", params: { tube_length: { value: 65, unit: "m" }, velocity: { value: 0.8, unit: "m/s" }, oxygen_production_rate: { value: 0.004, unit: "mol/(m3*s)" } } };
const axialDefaults = deriveAxialO2Defaults({ draft_id: "d1", name: "D1", revision: "1", seq: 1, objects: [mockUnit] }, mockUnit, null, null, {});
assert.equal(axialDefaults.length.value, 65, "length mapped from Process unit");
assert.equal(axialDefaults.length.source, "from Process");
assert.equal(axialDefaults.velocity.value, 0.8, "velocity mapped from Process unit");
assert.equal(axialDefaults.velocity.source, "from Process");
assert.equal(axialDefaults.oxygen_production_rate.value, 0.004, "oxygen production rate mapped from Process unit");
assert.equal(axialDefaults.oxygen_production_rate.source, "from Process");

// Variant summary with Motion and Axial O2 verdicts
const summary = buildVariantStageSummary({
  benchRuns: [
    { run_id: "r1", evaluator_id: "marine.rigid_body_waves", status: "succeeded", verdict: "usable", completed_at: "2026-11-24T00:00:00Z" },
    { run_id: "r2", evaluator_id: "bioprocess.tube_oxygen_buildup", status: "succeeded", verdict: "usable_with_warnings", completed_at: "2026-11-24T00:00:00Z" },
  ],
  cfdRuns: [{ id: "c1", project_id: "p", version_id: "v", component_id: "comp", title: "Mesh 1", state: "finished", created_at: "", updated_at: "" }],
});

const motionSummary = summary.find(s => s.stage === "Motion/Mechanics");
assert.ok(motionSummary && motionSummary.latestVerdict === "usable", "Motion verdict surfaced in variant summary");

const axialSummary = summary.find(s => s.stage === "Axial O2");
assert.ok(axialSummary && axialSummary.latestVerdict === "usable_with_warnings", "Axial O2 verdict surfaced in variant summary");

console.log("350 designMotionPanel: PASS");
