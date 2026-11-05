import assert from "node:assert/strict";
import fs from "node:fs";
const read = (path) => fs.readFileSync(new URL(path, import.meta.url), "utf8");
const inspector = read("../src/pages/DesignFeatureInspector.tsx");
const viewer = read("../src/components/BluecadGlbViewer.tsx");
assert.match(inspector, /Placed freely/);
assert.match(inspector, /connect_to: null, frame:/);
assert.match(inspector, /frame: _frame, placement: _placement/);
assert.match(inspector, /Number\.isFinite\(Number\(value\)\)/);
assert.match(inspector, /export const headingToDirection/);
assert.match(inspector, /export const directionToHeading/);
assert.match(viewer, /export const cadToGltf/);
assert.match(viewer, /export const gltfToCad/);
assert.match(viewer, /TransformControls/);
assert.match(viewer, /controls.enabled = !dragging/);
// Heading maps to +angle about glTF Y and is read back from the rotated axis, not the clamped Euler y.
assert.match(viewer, /marker\.rotation\.set\(0, THREE\.MathUtils\.degToRad\(value\.heading\), 0\)/);
assert.match(viewer, /Math\.atan2\(-axis\.z, axis\.x\)/);
// The handle callback must not overwrite other features' edits with a stale list.
assert.match(inspector, /setFeatures\(\(current\) => current\.map/);
assert.doesNotMatch(inspector + viewer, /fetch\(|ollama|localStorage|node:fs|child_process/i);
// Evaluate the exported pure helpers themselves, including non-axis-aligned round trips.
import ts from "typescript";
import vm from "node:vm";
const helper = (source, name) => {
  const line = source.split("\n").find((item) => item.startsWith(`export const ${name} =`));
  assert.ok(line, `${name} missing`);
  const js = ts.transpileModule(line.replace("export const ", "const ") + `\nresult = ${name};`, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
  const context = { result: null, Math }; vm.runInNewContext(js, context); return context.result;
};
const toDirection = helper(inspector, "headingToDirection"), toHeading = helper(inspector, "directionToHeading");
for (const heading of [-170, -45, 0, 60, 179]) assert.ok(Math.abs(toHeading(toDirection(heading)) - heading) < 1e-10);
const toGltf = helper(viewer, "cadToGltf"), toCad = helper(viewer, "gltfToCad");
for (const point of [[0, 0, 0], [12, -35, 100]]) assert.deepEqual(Array.from(toCad(toGltf(point))), point);
