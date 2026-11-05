// Spec 235 slice 1: feature inspector source-pattern test.
import assert from "node:assert/strict";
import fs from "node:fs";
const read = (path) => fs.readFileSync(new URL(path, import.meta.url), "utf8");
const api = read("../src/api/designProject.ts");
const inspector = read("../src/pages/DesignFeatureInspector.tsx");
const page = read("../src/pages/DesignProject.tsx");
const pkg = read("../package.json");

// Client functions sit next to attachDesignGeometry and carry the optimistic-concurrency fields.
assert.match(api, /export const getComponentFeatureHistory = /);
assert.match(api, /export const putComponentFeatureHistory = /);
assert.match(api, /\/geometry\/feature-history`, "PUT", \{\s*sequence, expected_revision: component\.revision, expected_active_version_id: versionId,\s*expected_draft_revision: draftRevision/);
assert.match(api, /stale_against_attached_candidate: boolean/);

// Mounted in the Geometry node; Process-linked components pass the draft revision.
assert.match(page, /selectedNode === "Geometry" && active \? <DesignFeatureInspector /);
assert.match(page, /draftRevision=\{selected\.source_draft_id \? sourceRevision : null\}/);
assert.match(page, /onApplied=\{reload\}/);

// Controls: Add per catalogue kind, enabled, remove, rebuild, undo.
for (const kind of ["tube_run", "bend", "manifold", "serpentine"]) assert.match(inspector, new RegExp(`kind: "${kind}"`));
assert.match(inspector, /Add \{item\.label\.toLowerCase\(\)\}<\/button>/);
assert.match(inspector, /Enabled/);
assert.match(inspector, /Remove \$\{feature\.feature_id\}/);
assert.match(inspector, />Rebuild geometry</);
assert.match(inspector, />Undo</);
assert.match(inspector, /connect_to: connect \? \{ from_port: "port_a", to_feature_id: previous\.feature_id, to_port: "port_b" \}/);

// Client-side refusals mirror 234 and run before any request.
assert.match(inspector, /must be a finite number/);
assert.match(inspector, /must be greater than 0/);
assert.match(inspector, /Wall must be less than half the outer Ø/);
assert.match(inspector, /const issue = featureDraftProblem\(features\);\s*if \(issue\) \{ setProblem\(issue\); return; \}/);

// Failure keeps the edit and says it was not applied; success shows the lineage line.
assert.match(inspector, /Not applied: \{refused\}/);
assert.match(inspector, /The previous geometry is still attached/);
assert.match(inspector, /stale against the attached geometry/);
assert.match(inspector, /Feature history revision \$\{read\.revision\}/);

// Undo stack is session-local and reset when the component or version changes; undo PUTs the previous sequence.
assert.match(inspector, /const scope = `\$\{projectId\}:\$\{versionId\}:\$\{component\.id\}`/);
assert.match(inspector, /setUndoStack\(\[\]\); setRead\(null\)/);
assert.match(inspector, /void send\(last, true\)/);

// $param references are shown read-only and sent back unchanged; frames are editable since slice 2 (235b test),
// and a tilted direction is preserved until the heading is edited.
assert.match(inspector, /Parameter reference, preserved unchanged/);
assert.match(inspector, /tilted direction is preserved until you edit the heading/);
assert.match(inspector, /parameters: base\?\.parameters \?\? \[\]/);

// No direct provider, filesystem or execution access.
assert.doesNotMatch(inspector, /fetch\(|ollama|localStorage|node:fs|child_process/i);
assert.match(pkg, /npm run test:235/);
assert.match(pkg, /"test:235": "node tests\/235_feature_inspector\.test\.mjs"/);
