import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const read = (relative) => readFileSync(path.join(root, relative), "utf8");

const api = read("src/api/processDraft.ts");
const panel = read("src/components/process/DofPanel.tsx");
const editor = read("src/stages/ProcessDraftEditor.tsx");
const pkg = JSON.parse(read("package.json"));

// 1. API contract coverage for Spec 257
for (const pattern of [
  "export type DofReport",
  "export type DofEntry",
  "export type DofCounts",
  "export type DofDesignValue",
  "export type DofEntryStatus",
  "getDraftDof",
  "/dof",
]) {
  assert.ok(api.includes(pattern), `processDraft.ts missing API contract pattern: ${pattern}`);
}

// 2. DofPanel UI coverage
for (const pattern of [
  "Degrees of freedom",
  "Set",
  "Unset",
  "Available from a run",
  "Not validated",
  "red_dot",
  "getDraftDof",
  "{entry.object} · {entry.label}",
]) {
  assert.ok(panel.includes(pattern), `DofPanel.tsx missing pattern: ${pattern}`);
}

// Assert DofPanel calls getDraftDof
assert.ok(
  panel.includes("getDraftDof(workspaceId, draftId)"),
  "DofPanel.tsx must call getDraftDof with workspaceId and draftId"
);

// Assert "Not validated" appears for red_dot and not "validated" alone
assert.ok(
  panel.includes("entry.design_value.validation === \"red_dot\""),
  "DofPanel.tsx must branch on red_dot validation state"
);
assert.ok(
  panel.includes("Not validated"),
  "DofPanel.tsx must show 'Not validated' when validation is red_dot"
);
assert.ok(
  !panel.includes('"validated"') && !panel.includes("'validated'"),
  "DofPanel.tsx must never use the word 'validated' alone for red_dot"
);

// Assert DofPanel contains no write/mutating HTTP methods (read-only conformance)
for (const method of ["POST", "PATCH", "PUT", "DELETE"]) {
  assert.ok(
    !panel.includes(`"${method}"`) && !panel.includes(`'${method}'`),
    `DofPanel.tsx must not contain write fetch method: ${method}`
  );
}

// 3. ProcessDraftEditor Pre-Run integration
assert.ok(editor.includes("DofPanel"), "ProcessDraftEditor.tsx must import DofPanel");
assert.ok(editor.includes("<DofPanel"), "ProcessDraftEditor.tsx must render <DofPanel");
assert.ok(
  editor.includes("Degrees of freedom"),
  "ProcessDraftEditor.tsx must include 'Degrees of freedom' toggle button"
);
assert.ok(
  editor.includes("shownFindings") && editor.includes("countLabel"),
  "ProcessDraftEditor.tsx must keep Pre-Run shownFindings and countLabel"
);

// 4. package.json script registration
assert.equal(
  pkg.scripts?.["test:257"],
  "node tests/257-dof-panel.mjs",
  "package.json must expose test:257"
);
assert.ok(
  pkg.scripts?.build?.includes("npm run test:257"),
  "npm run build must include test:257"
);
assert.ok(
  pkg.scripts?.build?.startsWith("npm run test:257 && "),
  "npm run build must prepend npm run test:257 &&"
);

console.log("257 degrees of freedom panel frontend conformance: PASS");
