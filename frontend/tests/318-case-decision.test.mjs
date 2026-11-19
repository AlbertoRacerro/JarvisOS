// Spec 318 (CASE-DECISION-1): the readiness board records a decision on the case's accepted verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const page = readFileSync(new URL("../src/pages/ReadinessBoard.tsx", import.meta.url), "utf8");
const client = readFileSync(new URL("../src/api/client.ts", import.meta.url), "utf8");

assert.match(page, /\n  createDecision,\n/);
assert.ok(readFileSync(new URL("../src/api/readinessBoard.ts", import.meta.url), "utf8").includes('export { createDecision } from "./client";'));
assert.match(page, /v\.case_id === selectedCase\?\.id && v\.status === "accepted"/, "only an accepted verdict of this case unlocks the form");
assert.match(page, /selectedStepId === "decide" && stepResult\.status === "red"/);
assert.match(page, /status: "accepted",\s*case_verdict_id: acceptedCaseVerdict\.id,/, "the decision is linked to the accepted verdict");
assert.match(page, /setBoard\(await getReadiness\(\)\)/, "the board reloads after recording");
for (const label of ["Titolo", "Decisione", "Motivazione (opzionale)", "Registra decisione"]) {
  assert.ok(page.includes(label), `missing label ${label}`);
}
assert.ok(page.includes('"Prima registra e accetta il verdetto nel passo Confronto."'));
assert.ok(page.includes('"Manca una decisione accettata collegata al verdetto di questo caso."'));
assert.ok(!page.includes('decide: { path: "/memory/project-basis"'), "decide no longer links to the read-only Project Basis");
assert.match(client, /case_verdict_id\?: string \| null;/);

console.log("318 case decision: ok");
