import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const [app, api, page] = await Promise.all([
  readFile(new URL("../src/App.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/api/literature.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/LiteratureKnowledge.tsx", import.meta.url), "utf8")
]);

assert.match(app, /case "memory-literature"/);
assert.match(app, /<LiteratureKnowledge\s+kind="literature"\s+workspaceId=/);
assert.match(api, /\/literature\/sources/);

assert.match(page, /Loading literature…/);
assert.match(page, /Literature unavailable/);
assert.match(page, /No literature sources yet/);
assert.match(page, /Backing unavailable\./);
assert.match(page, /Safe preview unavailable\./);

assert.match(page, /<details className="final-fusion__disclosure"/);
assert.match(page, /filtered\.map\(\(source\) => <SourceDisclosure/);
assert.match(page, /openIds/);
assert.match(page, /createLiteratureSource/);
assert.match(page, /createLiteratureEntry/);
assert.match(page, /onKnowledgeSelectionChange/);
assert.match(api, /request_key/);
assert.match(api, /\/literature\/files\?filename=/);
assert.match(api, /\/backing`/);
assert.match(api, /value_number\?: number/);
assert.match(page, /Source file \(optional\)/);
assert.match(page, /Attach source file/);
assert.match(page, /Number \(optional\)/);
assert.match(page, /Lines from \(optional\)/);
assert.match(page, /Lines to \(optional\)/);
assert.match(page, /locator_kind: "line" as const/);
for (const words of ["Checked against the source", "Checked, with warnings", "Not checked", "Does not match the source"]) assert.ok(page.includes(words), words);
assert.match(page, /check\.status !== "pass"/);

assert.match(page, /Browsing does not add Jarvis context\./);
assert.doesNotMatch(page, /\b(?:postJson|putJson|deleteJson)\b/);

console.log("114 literature knowledge frontend contract: PASS");
