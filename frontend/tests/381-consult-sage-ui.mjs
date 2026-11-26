import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const consultCardSource = fs.readFileSync(path.join(root, "src/components/ai/ConsultCard.tsx"), "utf8");
const sidecarSource = fs.readFileSync(path.join(root, "src/components/ai/useJarvisSidecar.tsx"), "utf8");
const threadsSource = fs.readFileSync(path.join(root, "src/api/threads.ts"), "utf8");
const sidecarCss = fs.readFileSync(path.join(root, "src/components/ai/JarvisSidecar.css"), "utf8");

// Spec 381: Sage reasoning status indicator in sidecar chat
assert.ok(
  sidecarSource.includes("Sage sta ragionando in locale"),
  "useJarvisSidecar must include 'Sage sta ragionando in locale' indicator"
);

// Spec 381: ConsultCard displays swap time in Italian
assert.ok(
  consultCardSource.includes("tempo di swap:"),
  "ConsultCard must contain Italian swap time label: 'tempo di swap:'"
);

assert.ok(
  consultCardSource.includes("swap_timing"),
  "ConsultCard must inspect swap_timing from ConsultBlock"
);

// Spec 381: threads.ts contract includes swap_timing on ConsultBlock
assert.ok(
  threadsSource.includes("swap_timing"),
  "threads.ts ConsultBlock type must include swap_timing"
);

// Spec 381: CSS styling for swap time display
assert.ok(
  sidecarCss.includes(".consult-card__swap"),
  "JarvisSidecar.css must define styling for .consult-card__swap"
);

console.log("381 consult Sage UI checks passed");
