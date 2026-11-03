import fs from "node:fs";
import assert from "node:assert/strict";

const sidecar = fs.readFileSync(new URL("../src/components/ai/useJarvisSidecar.tsx", import.meta.url), "utf8");
const api = fs.readFileSync(new URL("../src/api/threads.ts", import.meta.url), "utf8");

assert.match(sidecar, /jarvis-bubble__metadata/);
assert.match(sidecar, /Show answer information/);
assert.match(sidecar, /interaction\.input_tokens/);
assert.match(sidecar, /interaction\.latency_ms/);
assert.match(sidecar, /interaction\.activity \?\? "Thinking…"/);
assert.match(sidecar, /Math\.floor\(\(now - Date\.parse\(interaction\.created_at\)\) \/ 1000\)/);
assert.match(sidecar, /Escalate</);
assert.match(sidecar, /draftInteractionEscalation/);
assert.match(sidecar, /Approve text and escalate/);
assert.match(sidecar, /Review edited text/);
assert.match(sidecar, /cloudDraft\.text_digest && cloudEditText === cloudDraft\.text/);
assert.match(sidecar, /Checking cloud request…/);
assert.doesNotMatch(sidecar, /Approved derivative ID|Source reference \(for example/);
assert.match(sidecar, /Send task to Relay/);
assert.match(sidecar, /submitRelayRun\(workspaceId, targetThread/);
assert.match(api, /interactions\/.*\/escalation-draft/);
assert.match(api, /interactions\/.*\/escalate/);
assert.match(api, /readonly detail: string \| null/);
console.log("159 Sidecar conversation UX source checks passed");
