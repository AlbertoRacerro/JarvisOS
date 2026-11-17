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
assert.match(page, /locator_kind: hasPdfBacking \? "page" as const : "line" as const/);
assert.match(page, /Pages from \(optional\)/);
assert.match(page, /Pages to \(optional\)/);
assert.match(page, /This PDF has no readable text \(it may be a scan\); values on it cannot be checked yet\./);
for (const words of ["Checked against the source", "Checked, with warnings", "Not checked", "Does not match the source"]) assert.ok(page.includes(words), words);
assert.match(page, /check\.status !== "pass"/);

// Structured PDF view (spec 296): states, per-token selection, selection note, unlocated items.
assert.match(api, /\/structured`/);
assert.match(api, /reason_code: StructuredPdfReasonCode/);
for (const code of ["not_started", "no_text_layer", "unreadable", "too_many_pages", "unsafe_cache", "bundle_missing", "running", "conversion_failed", "timeout"]) assert.ok(api.includes(`"${code}"`), code);
assert.match(api, /unlocated: StructuredPdfBlock\[\]/);
assert.match(page, /Extract page text and tables locally/);
assert.match(page, /data-extraction-state=\{structured\.state\}/);
assert.match(page, /Extracting locally…/);
assert.match(page, /text\.matchAll\(NUMERIC_TOKEN\)/);
assert.match(page, /<button key=\{at\} type="button" className="literature-token" data-token=\{match\[0\]\}/);
assert.match(page, /onClick=\{\(\) => onChoose\(match\[0\], page\)\}>\{match\[0\]\}<\/button>/);
assert.match(page, /setValue\(token\); setNumber\(token\.includes\(","\) \? "" : token\.replace/);
assert.match(page, /setLineFrom\(String\(page\)\); setLineTo\(String\(page\)\)/);
assert.match(page, /The source check confirms this number appears on that PDF page, not what it means in the table\./);
assert.match(page, /nothing is saved until you save it/);
assert.match(page, /Unlocated items/);
assert.match(page, /cannot prefill a finding/);
assert.match(page, /page=\{null\}/);
assert.match(page, /if \(page === null\) return <>\{text\}<\/>/);
const chooseBody = page.slice(page.indexOf("const chooseToken"), page.indexOf("const saveEntry"));
assert.doesNotMatch(chooseBody, /createLiteratureEntry/);

assert.match(page, /Browsing does not add Jarvis context\./);
assert.doesNotMatch(page, /\b(?:postJson|putJson|deleteJson)\b/);

console.log("114 literature knowledge frontend contract: PASS");
