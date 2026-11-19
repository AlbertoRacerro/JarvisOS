import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const page = readFileSync(resolve(root, "src/pages/MemorySearch.tsx"), "utf8");
const api = readFileSync(resolve(root, "src/api/memorySearch.ts"), "utf8");
const packageJson = readFileSync(resolve(root, "package.json"), "utf8");

assert.ok(page.includes("Verifica del recupero"));
assert.ok(page.includes("Domanda"));
assert.ok(page.includes("Titolo o ID atteso"));
assert.ok(page.includes("Lessicale"));
assert.ok(page.includes("Ibrida"));
assert.ok(page.includes("Verifica recupero"));
for (const metric of ["top1_hit_rate", "top3_hit_rate", "mrr", "zero_hit_count"]) {
  assert.ok(page.includes(metric), `missing metric ${metric}`);
}
for (const label of ["Utilizzabile", "Utilizzabile con avvertenze", "Non verificato", "Non utilizzabile"]) {
  assert.ok(page.includes(label), `missing verdict ${label}`);
}
assert.ok(page.includes("Contato come fonte:"));
assert.ok(page.includes("(file {question.accepted_ids[0]})"));
assert.ok(api.includes("accepted_ids?: string[]"));
assert.ok(page.includes("question.matched_accepted && question.accepted_ids"));
assert.ok(api.includes("runRetrievalEval"));
assert.ok(api.includes("/retrieval-eval"));
assert.ok(packageJson.includes('"test:316"'));
assert.ok(packageJson.includes("npm run test:316"));
console.log("316 retrieval eval UI frontend test: OK");
