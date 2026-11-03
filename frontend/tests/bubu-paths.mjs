import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { componentTargetId } from "../src/app/designTargets.ts";
import { parsePointDirectives } from "../src/components/bubu/pointerDirectives.ts";

// BUBU-3 item 1: each Components-tree row is identifiable by name, so Bubu can point at "C1-CFD" as the first step of
// "CFD stage -> component -> Results" instead of at a Library row.
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => fs.readFileSync(path.join(root, rel), "utf8");

const taken = new Set();
assert.equal(componentTargetId("C1-CFD", taken), "design.tree.component.c1-cfd");
assert.equal(componentTargetId("Photobioreactor loop", taken), "design.tree.component.photobioreactor-loop");
assert.equal(componentTargetId("C1-CFD", taken), "design.tree.component.c1-cfd-2", "a repeated name stays unique");
assert.equal(componentTargetId("c1 cfd", taken), "design.tree.component.c1-cfd-3");
assert.equal(componentTargetId("  ***  ", taken), "design.tree.component.component", "a name without letters or digits still gets an id");
assert.equal(componentTargetId("Ünïcode ünit", new Set()), "design.tree.component.n-code-nit", "non-ascii letters become separators");

const long = componentTargetId("x".repeat(300), new Set());
assert.ok(long.length <= 80, "ids stay within the backend's 80-character limit");
for (const id of [...taken, long]) assert.match(id, /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,79}$/, `${id} is a pointable id for the backend`);
for (const id of taken) assert.match(id, /^[\w.:-]+$/, `${id} is accepted by the directive parser`);

// The pointer directive accepts these ids and a step number in the label (backward compatible with plain labels).
assert.deepEqual(parsePointDirectives("First [[point:design.tree.component.c1-cfd|1. C1-CFD]], then choose Results."),
  { text: "First, then choose Results.", points: [{ id: "design.tree.component.c1-cfd", label: "1. C1-CFD" }] });
assert.deepEqual(parsePointDirectives("[[point:design.stage.cfd|1. CFD]]").points, [{ id: "design.stage.cfd", label: "1. CFD" }]);

// The tree gives every component row that id, and names the row after the component (not after its twisty glyph).
const tree = read("src/components/design/DesignComponentTree.tsx");
assert.match(tree, /target: componentTargetId\(item\.name, taken\)/, "component rows get a name-based target id");
assert.match(tree, /\{\.\.\.\(row\.target \? \{ "data-jarvis-target": row\.target \} : \{\}\)\}/, "rows carry the target on the treeitem");
assert.match(tree, /aria-label=\{row\.meta \? undefined : row\.label\}/, "component rows are named by the component; node rows keep their status word");

console.log("bubu paths: ok");
