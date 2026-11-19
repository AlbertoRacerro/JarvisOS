import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const [barCode, pageCode, apiCode] = await Promise.all([
  readFile(new URL("../src/components/design/GuidedFlowBar.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/DesignProject.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/api/guidedFlow.ts", import.meta.url), "utf8"),
]);

// 1. Api module exports the 3 required endpoints and types
assert.match(apiCode, /export async function getGuidedQuestions/);
assert.match(apiCode, /export async function createGuidedQuestion/);
assert.match(apiCode, /export async function updateGuidedQuestion/);
assert.match(apiCode, /\/engineering\/guided-questions/);

// 2. Italian labels in GuidedFlowBar
const requiredLabels = [
  "Guidato",
  "Libero",
  "Domande",
  "Inquadra",
  "Fonti",
  "Modello",
  "Calcolo",
  "Esplora",
  "Confronta",
  "Decidi",
  "Prossimo passo",
];

for (const label of requiredLabels) {
  assert.ok(
    barCode.includes(label),
    `GuidedFlowBar must contain visible label "${label}"`,
  );
}

// 3. LocalStorage persistence for Guidato / Libero
assert.match(barCode, /localStorage\.getItem/);
assert.match(barCode, /localStorage\.setItem/);
assert.match(barCode, /"guidato"/);
assert.match(barCode, /"libero"/);

// 4. Question list, create form, close/reopen, skip-explore reason
assert.match(barCode, /isCreating/);
assert.match(barCode, /Crea domanda/);
assert.match(barCode, /what_changes/);
assert.match(barCode, /Chiudi/);
assert.match(barCode, /Riapri/);
assert.match(barCode, /explore_skip_reason/);
assert.match(barCode, /Salva motivazione/);

// 5. 7-step path bar with states and navigation
assert.match(barCode, /effectiveSteps\.map/);
assert.match(barCode, /data-step=/);
assert.match(barCode, /data-state=/);
assert.match(barCode, /step\.route/);

// 6. Prossimo passo card with single action button
assert.match(barCode, /guided-flow-bar__next-card/);
assert.match(barCode, /guided-flow-bar__next-btn/);
assert.match(barCode, /effectiveNextAction\.route/);
assert.match(barCode, /effectiveNextAction\.action_label_it/);

// 7. Rendered at top of DesignProject with active data_workspace_id or projectId
assert.match(pageCode, /import GuidedFlowBar from "\.\.\/components\/design\/GuidedFlowBar"/);
assert.match(
  pageCode,
  /<GuidedFlowBar\s+workspaceId=\{active\?\.data_workspace_id\s+\?\?\s+projectId\}\s+navigate=\{navigate\}\s*\/>/,
);

console.log("312-guided-flow: PASS");
