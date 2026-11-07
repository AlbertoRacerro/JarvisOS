import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const read = (relative) => readFileSync(path.join(root, relative), "utf8");

const api = read("src/api/projectKnowledge.ts");
const panel = read("src/components/fusion/ProjectKnowledgePanel.tsx");
const pkg = JSON.parse(read("package.json"));

// API contract coverage for Spec 253
for (const pattern of [
  "applicability_set_digest: string;",
  "export type SimulationResultBatchRead",
  "export type SimulationResultValueRead",
  "export type SimulationResultNotificationRead",
  "export type SimulationResultResolveRequest",
  "export type ProjectKnowledgeValidationRead",
  "unvalidated_source: boolean;",
  "unvalidated_value_ids: string[];",
  "/result-batches",
  "/result-notifications",
  "/result-values/${valueId}/resolve",
  "/validations"
]) {
  assert.ok(api.includes(pattern), `projectKnowledge.ts missing: ${pattern}`);
}

// UI coverage for red dots, exact-target buttons, batch notification, resolution, markers and requirement verdicts
for (const pattern of [
  'aria-label="Batch notifications"',
  "Jarvis batch notifications",
  "openExceptionTarget(target.target_href, target.value_id)",
  'aria-label="Simulation result batches"',
  "red-dot-indicator",
  'aria-label="Exception red dot"',
  "Failed checks ·",
  "unvalidated-value-marker",
  "Unvalidated value",
  "Unvalidated source value",
  'aria-label="Resolution action"',
  'aria-label="Corrected value"',
  'aria-label="Resolution reason"',
  "Submit correction",
  "Confirm exception",
  'aria-label="Requirement verification verdicts"',
  "Applicability-set digest ·"
]) {
  assert.ok(panel.includes(pattern), `ProjectKnowledgePanel.tsx missing: ${pattern}`);
}

assert.equal(
  pkg.scripts?.["test:253"],
  "node tests/253-sim-results-requirement-verification.mjs",
  "package.json must expose test:253"
);
assert.ok(
  pkg.scripts?.build?.includes("npm run test:253"),
  "npm run build must execute test:253"
);

console.log("253 simulation results and requirement verification frontend conformance: PASS");
