import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const [page, api] = await Promise.all([
  readFile(new URL("../src/pages/LiteratureKnowledge.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/api/literature.ts", import.meta.url), "utf8"),
]);

// 1. Api wrapper for accept
assert.match(api, /export async function acceptLiteratureEntry/);
assert.match(api, /\/sources\/\$\{encodeURIComponent\(sourceId\)\}\/entries\/\$\{encodeURIComponent\(entryId\)\}\/accept/);

// 2. Quantity field in Add a finding and non-accepted datum
assert.match(page, /Quantity name/);
assert.match(page, /Accept as fact/);

// 3. Disabled when verdict is not usable/usable_with_warnings
assert.match(page, /entry\.verdict === "usable" \|\| entry\.verdict === "usable_with_warnings"/);
assert.match(page, /disabled=\{accepting \|\| !isUsable\}/);

// 4. Parameter creation for accepted datum with value_number and unit
assert.match(page, /Parameter name/);
assert.match(page, /Use as parameter/);
assert.match(page, /source_ref:\s*`literature_entry:\$\{entry\.id\}`/);
assert.match(page, /value_status:\s*"literature"/);
assert.match(page, /status:\s*"draft"/);

// 5. Re-read source and status line
assert.match(page, /getLiteratureSource\(source\.workspace_id,\s*source\.id\)/);
assert.match(page, /Parameter\s+\$\{createdName\}\s+created as draft\./);

// 6. Errors with role="alert" inside the entry
assert.match(page, /role="alert"/);

console.log("311-literature-fact-input frontend contract: PASS");
