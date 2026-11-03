// Spec 224 owner-surface slice (ADR-061 gates 1-2), acceptance 1: epochs move exactly when a published field changes,
// owner revisions propagate, and the Process owner predicate completes only on a newer revision of the selected unit.
// The real-browser measurement is e2e/owner_predicate_walk.mjs.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
function load(path) {
  const module = { exports: {} };
  const code = ts.transpileModule(read(path), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  new Function("require", "module", "exports", code)(() => ({}), module, module.exports);
  return module.exports;
}
globalThis.window = new EventTarget();
const surface = load("src/app/workspaceActionSurface.ts");
const predicates = load("src/app/ownerPredicates.ts");

// ---- Process: epoch monotonicity, unchanged content keeps the epoch, revision propagates ----
const seen = [];
const stop = surface.listenForProcessSurface((next) => seen.push(next));
const unit = { kind: "unit", id: "u-1", tag: "PBR-1" };
const start = surface.currentProcessSurface().epoch;
surface.publishProcessSurface({ draft_id: "d-1", revision: "3:aaaa", process_selection: [unit] });
const e1 = surface.currentProcessSurface().epoch;
assert.ok(e1 > start, "a changed surface bumps the epoch");
surface.publishProcessSurface({ draft_id: "d-1", revision: "3:aaaa", process_selection: [{ ...unit }] });
assert.equal(surface.currentProcessSurface().epoch, e1, "republishing identical content keeps the epoch");
surface.publishProcessSurface({ draft_id: "d-1", revision: "4:bbbb", process_selection: [unit] });
const e2 = surface.currentProcessSurface().epoch;
assert.ok(e2 > e1, "a same-draft revision with an unchanged selection moves the epoch (P31A)");
assert.equal(surface.currentProcessSurface().revision, "4:bbbb", "the owner revision is published");
surface.publishProcessSurface({ draft_id: "d-1", revision: "3:aaaa", process_selection: [unit] });
assert.ok(surface.currentProcessSurface().epoch > e2, "returning to earlier content is still a new epoch");
assert.deepEqual(seen.map((s) => s.epoch), [e1, e1, e2, e2 + 1], "listeners receive the stamped surface");
stop();

// ---- Design: epoch, version_id propagation, null clears ----
const design = (over = {}) => ({ route_id: "design-project", workspace_id: "w-1", example: false, version_id: "v-1", version_label: "v1",
  stage: "CFD", component_id: "c-1", component_name: "C1", node: "Results", panels: { cfd_field_slice: "loading" }, disabled: {}, ...over });
surface.publishDesignSurface(design());
const d1 = surface.currentDesignSurface().epoch;
surface.publishDesignSurface(design());
assert.equal(surface.currentDesignSurface().epoch, d1, "identical Design content keeps the epoch");
surface.publishDesignSurface(design({ panels: { cfd_field_slice: "available" } }));
const d2 = surface.currentDesignSurface().epoch;
assert.ok(d2 > d1, "a panel state change moves the epoch");
surface.publishDesignSurface(design({ version_id: "v-2", panels: { cfd_field_slice: "available" } }));
assert.equal(surface.currentDesignSurface().version_id, "v-2");
assert.ok(surface.currentDesignSurface().epoch > d2, "a version change moves the epoch");
const d3 = surface.currentDesignSurface().epoch;
surface.publishDesignSurface(null);
assert.equal(surface.currentDesignSurface(), null);
surface.publishDesignSurface(design({ version_id: "v-2", panels: { cfd_field_slice: "available" } }));
assert.ok(surface.currentDesignSurface().epoch > d3, "reopening identical Design content after a clear gets a fresh epoch");
surface.publishDesignSurface(null);
surface.publishWorkspaceListSurface("loading");
assert.equal(surface.currentDesignSurface().panels.workspaces, "loading", "R07: the workspace list publishes not-yet");
assert.equal(surface.currentDesignSurface().workspace_id, null);

// ---- Owner predicate ----
const { processUnitRevised, revisionSeq } = predicates;
assert.equal(revisionSeq("12:abcd"), 12);
assert.equal(revisionSeq(null), null);
assert.equal(revisionSeq("x:abcd"), null);
const expected = { draft_id: "d-1", unit_id: "u-1", observed_revision: "3:aaaa" };
const at = (revision, over = {}) => ({ draft_id: "d-1", revision, process_selection: [unit], epoch: 1, ...over });
assert.equal(processUnitRevised(at("4:bbbb"), expected), true, "completes on a newer revision of the selected unit");
assert.equal(processUnitRevised(at("3:aaaa"), expected), false, "no-op / failed / pending: same revision does not complete");
assert.equal(processUnitRevised(at("2:cccc"), expected), false, "an older revision does not complete");
assert.equal(processUnitRevised(at("4:bbbb", { process_selection: [{ ...unit, id: "u-2" }] }), expected), false, "another unit does not complete");
assert.equal(processUnitRevised(at("4:bbbb", { process_selection: [] }), expected), false, "no selection does not complete");
assert.equal(processUnitRevised(at("4:bbbb", { draft_id: "d-2" }), expected), false, "another draft does not complete");
assert.equal(processUnitRevised(null, expected), false);

// ---- Wiring: owners publish the facts; the canvas exposes the owner id; nothing is sent anywhere ----
const editor = read("src/stages/ProcessDraftEditor.tsx");
assert.ok(editor.includes("revision: draft?.revision ?? null"), "the Process editor publishes its draft revision");
assert.ok(editor.includes("data-process-unit-id={item.id}"), "canvas units expose the owner id");
const page = read("src/pages/DesignProject.tsx");
assert.ok(page.includes("cfd_field_slice:") && page.includes("[ATTACH_MODEL]: attachDisabled"), "Design publishes field-slice state and the disabled reason");
assert.ok(read("src/App.tsx").includes("publishWorkspaceListSurface("), "the shell publishes the workspace list state");
for (const path of ["src/app/workspaceActionSurface.ts", "src/app/ownerPredicates.ts"]) assert.ok(!/fetch\(|api\//.test(read(path)), `${path} makes no request`);
console.log("224 owner-surface: ok");
