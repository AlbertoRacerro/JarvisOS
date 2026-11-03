import assert from "node:assert/strict";
import fs from "node:fs";

const editor = fs.readFileSync(new URL("../src/stages/ProcessDraftEditor.tsx", import.meta.url), "utf8");
const contract = fs.readFileSync(new URL("../src/api/processDraft.ts", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../src/stages/ProcessDraftEditor.css", import.meta.url), "utf8");

for (const text of [
  "Culture medium", "This feed carries a culture", "Biomass X", "Dissolved nitrogen (as N)",
  "Dissolved phosphorus (as P)", "Dissolved O₂", "DIC", "Salinity", "set_stream_culture",
  "Culture · Jarvis", "draft-owner-badge", "Culture rule",
]) assert.ok(editor.includes(text), `missing Process culture UI contract: ${text}`);
assert.match(editor, /mass_concentration/);
assert.match(editor, /molar_concentration/);
assert.match(contract, /culture_rule: string/);
assert.match(contract, /culture\?: Record<string, StoredQuantity>/);
assert.match(contract, /culture\?: Record<string, CultureResult>/);
assert.match(editor, /aria-label="Culture results read-only"/);
assert.match(editor, /aria-label="This feed carries a culture"/);
assert.match(editor, /aria-label="Draft findings"/);
assert.match(editor, /maximumSignificantDigits: 5/);
assert.match(css, /is-culture-stream/);
assert.match(css, /draft-owner-badge/);
assert.doesNotMatch(editor, /JSON\.stringify\(cultureResult/);

console.log("167 Process culture frontend contract passed");
