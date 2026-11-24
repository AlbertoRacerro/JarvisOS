import fs from "node:fs";
import assert from "node:assert/strict";

const sidecar = fs.readFileSync(new URL("../src/components/ai/useJarvisSidecar.tsx", import.meta.url), "utf8");
const api = fs.readFileSync(new URL("../src/api/threads.ts", import.meta.url), "utf8");
const generated = fs.readFileSync(new URL("../src/api/generated/numberProvenance.ts", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../src/components/ai/JarvisSidecar.css", import.meta.url), "utf8");

// Requirement 3: Sidecar provenance bar and chips UI presence
assert.match(sidecar, /jarvis-number-provenance/);
assert.match(sidecar, /jarvis-provenance-summary/);
assert.match(sidecar, /jarvis-provenance-badge--verified/);
assert.match(sidecar, /jarvis-provenance-badge--unverified/);
assert.match(sidecar, /jarvis-provenance-chips/);
assert.match(sidecar, /jarvis-provenance-chip/);
assert.match(sidecar, /Provenienza numeri/);
assert.match(sidecar, /Non verificato/);
assert.match(sidecar, /interaction\.number_provenance/);

// Type definitions in threads.ts
assert.match(api, /export type \{ NumberProvenance, NumberProvenanceEntry \} from/);
assert.match(generated, /export type NumberProvenanceEntry =/);
assert.match(generated, /export type NumberProvenance =/);
assert.match(api, /number_provenance\?: NumberProvenance \| null/);

// CSS styles
assert.match(css, /\.jarvis-number-provenance/);
assert.match(css, /\.jarvis-provenance-summary/);
assert.match(css, /\.jarvis-provenance-badge--verified/);
assert.match(css, /\.jarvis-provenance-badge--unverified/);
assert.match(css, /\.jarvis-provenance-chips/);
assert.match(css, /\.jarvis-provenance-chip--verified/);
assert.match(css, /\.jarvis-provenance-chip--unverified/);

console.log("363 Hermes number provenance source checks passed");
