// Spec 328 (CASE-WALK-1): e2e/case_walk.mjs only reads over HTTP and takes its case from the pack files.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const script = readFileSync(new URL("../../e2e/case_walk.mjs", import.meta.url), "utf8");

for (const method of ["POST", "PATCH", "PUT", "DELETE"]) {
  assert.ok(!script.includes(method), `the walk script must not contain a ${method} request`);
}
assert.ok(!/\bmethod\s*:/.test(script), "the walk script sets no fetch method");
assert.ok(!script.includes("page.request") && !script.includes("context.request"), "no request through the browser context");
assert.ok(script.includes("readiness_cases.toml"), "the case is read from the pack's readiness_cases.toml");
assert.ok(script.includes("case_walks"), "the walk input is read from the pack's case_walks/ directory");
assert.ok(script.includes("tomllib"), "TOML is read through the venv Python");

console.log("328 case walk script: ok");
