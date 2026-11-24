import fs from "node:fs";
import assert from "node:assert/strict";

const sidecar = fs.readFileSync(new URL("../src/components/ai/useJarvisSidecar.tsx", import.meta.url), "utf8");
const api = fs.readFileSync(new URL("../src/api/threads.ts", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../src/components/ai/JarvisSidecar.css", import.meta.url), "utf8");

// Requirement 4: sidecar live steps & plan UI presence
assert.match(sidecar, /jarvis-live-progress/);
assert.match(sidecar, /jarvis-turn-progress/);
assert.match(sidecar, /jarvis-plan-box/);
assert.match(sidecar, /jarvis-tool-steps-box/);
assert.match(sidecar, /Plan checklist/);
assert.match(sidecar, /Recorded findings/);
assert.match(sidecar, /Tool steps/);
assert.match(sidecar, /interaction\.turn_plan/);
assert.match(sidecar, /interaction\.live_steps/);

// Type definitions in threads.ts
assert.match(api, /export type TurnPlanStep =/);
assert.match(api, /export type TurnPlanFinding =/);
assert.match(api, /export type TurnPlan =/);
assert.match(api, /export type LiveStep =/);
assert.match(api, /turn_plan\?: TurnPlan \| null/);
assert.match(api, /live_steps\?: LiveStep\[\]/);

// CSS styles
assert.match(css, /\.jarvis-live-progress/);
assert.match(css, /\.jarvis-turn-progress/);
assert.match(css, /\.jarvis-plan-box/);
assert.match(css, /\.jarvis-live-step/);

console.log("362 Hermes saved plan and live steps source checks passed");
