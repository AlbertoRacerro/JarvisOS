import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const settingsSource = fs.readFileSync(path.join(root, "src/pages/Settings.tsx"), "utf8");
const consultCardSource = fs.readFileSync(path.join(root, "src/components/ai/ConsultCard.tsx"), "utf8");
const sidecarSource = fs.readFileSync(path.join(root, "src/components/ai/useJarvisSidecar.tsx"), "utf8");
const settingsCss = fs.readFileSync(path.join(root, "src/styles/final-settings.css"), "utf8");

// Spec 380: Settings card
assert.ok(settingsSource.includes("data-consult-panel"), "Settings must have data-consult-panel");
assert.ok(settingsSource.includes("Consulente"), "Settings must include 'Consulente'");
assert.ok(settingsCss.includes("[data-consult-panel]"), "CSS must show [data-consult-panel] under AI tab");

// Spec 380: ConsultCard Italian labels
const requiredLabels = [
  "Consulente",
  "Approva",
  "Modifica",
  "Rifiuta",
  "domanda inviata",
  "dice ·",
];

for (const label of requiredLabels) {
  assert.ok(
    consultCardSource.includes(label),
    `ConsultCard must contain Italian label: '${label}'`
  );
}

// Check sidecar renders ConsultCard and consult markers
assert.ok(sidecarSource.includes("ConsultCard"), "useJarvisSidecar must import/use ConsultCard");
assert.ok(sidecarSource.includes("consult:"), "useJarvisSidecar must handle consult markers");

console.log("380 consult UI checks passed");
