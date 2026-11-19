import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = path => readFileSync(resolve(root, path), "utf8");
const panel = read("src/components/BenchRunsPanel.tsx");
const api = read("src/api/benchRuns.ts");

assert.ok(panel.includes("Nuova esecuzione"));
assert.ok(panel.includes("Esegui"));
assert.ok(panel.includes("Formula"));
assert.ok(panel.includes("Intervallo di validità"));
assert.ok(panel.includes("Fonte"));
assert.ok(panel.includes("Correlazione"));
assert.ok(api.includes("listBenchCards"));
assert.ok(api.includes("createBenchRun"));
assert.ok(api.includes('method: "POST"'));
assert.ok(api.includes("BenchCard"));
assert.ok(api.includes("CreateBenchRunPayload"));
assert.ok(!panel.includes("Run") || panel.includes("run"));
console.log("correlation cards: OK");
