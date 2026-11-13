import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const read = (relative) => readFileSync(path.join(root, relative), "utf8");

const api = read("src/api/projectKnowledge.ts");
const generatedApi = read("src/api/generated/projectKnowledge.ts");
const workbench = read("src/pages/RunsWorkbench.tsx");
const modal = read("src/components/fusion/ValueHistoryModal.tsx");
const panel = read("src/components/fusion/ProjectKnowledgePanel.tsx");
const pkg = JSON.parse(read("package.json"));

// 1. API client functions and generated contract types for Spec 258
for (const pattern of [
  "RunInputsManifestRead",
  "ValueVersionChainRead",
  "ValueComparisonRead",
  "RunLinkedParameterItem",
  "ValueVersionItem",
  "BoundRunRef",
  "BoundRequirementCheckRef",
  "getRunInputsManifest",
  "getParameterHistory",
  "getResultValueHistory",
  "compareValues",
  "/values-used",
  "/history",
  "/values/compare"
]) {
  assert.ok(api.includes(pattern), `projectKnowledge.ts missing: ${pattern}`);
}

for (const pattern of [
  "export type RunLinkedParameterItem",
  "export type RunInputsManifestRead",
  "export type ValueVersionItem",
  "export type ValueVersionChainRead",
  "export type BoundRunRef",
  "export type BoundRequirementCheckRef",
  "export type ValueComparisonSide",
  "export type ValueComparisonRead",
  "newer_version_exists: boolean;",
  "intermediate_versions_unknown: boolean;"
]) {
  assert.ok(generatedApi.includes(pattern), `generated/projectKnowledge.ts missing: ${pattern}`);
}

// 2. RunsWorkbench: "Values used" section, "newer version exists", and history_not_recorded
for (const pattern of [
  'aria-label="Values used"',
  "Values used",
  "newer version exists",
  "history_not_recorded",
  "manifest.reason",
  "getRunInputsManifest",
  "ValueHistoryModal"
]) {
  assert.ok(workbench.includes(pattern), `RunsWorkbench.tsx missing: ${pattern}`);
}

// 3. ValueHistoryModal: ordered versions, intermediate_versions_unknown, exception red-dot, Compare action, users
for (const pattern of [
  'aria-label="Value history"',
  "intermediate_versions_unknown",
  "Version chain",
  "red-dot-indicator",
  'aria-label="Exception red dot"',
  "Compare versions",
  "Compare",
  "Runs used",
  "Requirement checks",
  "No runs used this version",
  "No requirement checks used this version"
]) {
  assert.ok(modal.includes(pattern), `ValueHistoryModal.tsx missing: ${pattern}`);
}

// Ensure no snapshot button or snapshot page exists in the value history surface
assert.ok(!modal.toLowerCase().includes("snapshot"), "ValueHistoryModal must not have a snapshot button or page");

// 4. Reachability from ProjectKnowledgePanel
for (const pattern of [
  "ValueHistoryModal",
  'onClick={() => setActiveHistoryTarget({ kind: "simulation_result_value"',
  'onClick={() => setActiveHistoryTarget({ kind: "parameter"'
]) {
  assert.ok(panel.includes(pattern), `ProjectKnowledgePanel.tsx missing: ${pattern}`);
}

// 5. package.json build script wiring
assert.equal(
  pkg.scripts?.["test:258"],
  "node tests/258-value-history.mjs",
  "package.json must expose test:258"
);
assert.ok(
  pkg.scripts?.build?.includes("npm run test:258"),
  "npm run build must execute test:258"
);

console.log("258 value history frontend conformance: PASS");
