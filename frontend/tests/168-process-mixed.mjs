import assert from "node:assert/strict";
import fs from "node:fs";

const editor = fs.readFileSync(new URL("../src/stages/ProcessDraftEditor.tsx", import.meta.url), "utf8");
const contract = fs.readFileSync(new URL("../src/api/processDraft.ts", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../src/stages/ProcessDraftEditor.css", import.meta.url), "utf8");

for (const text of [
  "Jarvis units", "separator-derived-split", "Running mixed solve (DWSIM + Jarvis)…",
  "Converged in", "Not converged (", "Convergence ·", "max_normalized_residual",
  "draft-convergence-table", "Owner and segment listing", "Results · Jarvis",
  "jarvis_bio", "SpecifiedSeparator",
  // Failure records: tag-based segments are guarded, check findings and diffs are shown, units named.
  "Array.isArray(segment?.units)", "record.findings", "record.diffs", "failed_units",
  // Result owner wins for a Jarvis-converged Recycle; culture-only loops are not called iterations.
  "reportedOwner", "culture_only", "no cross-engine tear to iterate",
  "no finite value", "pattern_mismatch_fields", '"iteration" : "iterations"',
]) assert.ok(editor.includes(text), "missing mixed Process UI contract: " + text);
assert.match(contract, /mixed_solve\?:/);
assert.match(contract, /normalized_residuals/);
assert.match(css, /\.draft-convergence-table[\s\S]*max-height/);
assert.match(css, /overflow:\s*auto/);
assert.doesNotMatch(editor, /JSON\.stringify\(run\.mixed_solve/);

console.log("168 mixed Process frontend contract passed");
