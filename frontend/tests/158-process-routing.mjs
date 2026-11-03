import assert from "node:assert/strict";
import { readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { transform } from "esbuild";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { addBend, isOrthogonal, offsetSegment, removeVertex, routeEditOps, routeStream } from "../src/stages/processRouting.ts";

const start = { at: { x: 0, y: 0 }, side: "right" };
const end = { at: { x: 200, y: 100 }, side: "left" };
const base = routeStream(start, end);
assert.equal(isOrthogonal(base), true);
assert.deepEqual(base[0], start.at);
assert.deepEqual(base.at(-1), end.at);

const moved = offsetSegment(base, 2, 20);
assert.ok(moved?.length);
const edited = routeStream(start, end, moved);
assert.equal(isOrthogonal(edited), true);
assert.notDeepEqual(edited, base);
assert.deepEqual(edited[0], start.at);
assert.deepEqual(edited.at(-1), end.at);

const bend = addBend(base, 2, { x: 100, y: 50 });
assert.ok(bend?.length);
assert.equal(isOrthogonal(routeStream(start, end, bend)), true);
assert.equal(offsetSegment(base, 0, 20), null);

console.log("158 orthogonal route edit acceptance: PASS");

// A waypoint edit (drag, bend, bend removal, reset) produces exactly one layout-only op.
const PROCESS_OPS = new Set(["add_unit", "add_stream", "delete", "move", "rename", "connect", "disconnect",
  "set_stream_spec", "set_unit_params", "set_reactions", "set_thermo"]);
const withBend = routeStream(start, end, bend);
const removed = removeVertex(withBend, 3);
assert.ok(removed);
for (const waypoints of [moved, bend, removed, []]) {
  const ops = routeEditOps("s1", waypoints);
  assert.equal(ops.length, 1);
  assert.deepEqual(Object.keys(ops[0]).sort(), ["op", "points", "stream"]);
  assert.equal(ops[0].op, "set_route");
  assert.equal(PROCESS_OPS.has(ops[0].op), false);
  assert.deepEqual(ops[0].points, waypoints);
  assert.ok(ops[0].points.every((point) => Object.keys(point).sort().join() === "x,y"));
}

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const editor = read("src/stages/ProcessDraftEditor.tsx");
const saveRoute = editor.slice(editor.indexOf("const saveRoute = "), editor.indexOf("const onPointerMove = "));
assert.ok(saveRoute.includes("apply(routeEditOps(streamId, points))"), "route edits must patch only routeEditOps");
assert.ok(!/executeDraft|validate|"run"/.test(saveRoute), "a route edit must never Validate/Run");
assert.equal((editor.match(/op: "set_route"/g) ?? []).length, 0, "set_route ops are built only by routeEditOps");
console.log("158 waypoint edit is layout-only: PASS");

// Inputs vs results: the inspector separates editable inputs from read-only DWSIM results.
assert.ok(editor.includes('className="draft-fieldset draft-inputs" aria-label="Inputs"'));
assert.ok(editor.includes('aria-label="Results (read-only)"'));
assert.ok(editor.includes('(param.classification ?? "input") === "input"'), "only input params become editable fields");

// Render ResultProperties server-side: result rows are read-only and spec/result rows are distinct.
const source = read("src/components/process/ResultProperties.tsx");
const { code } = await transform(source, { loader: "tsx", jsx: "automatic", format: "esm" });
const compiled = resolve(root, "tests", `.158-result-properties-${process.pid}.mjs`);
writeFileSync(compiled, code);
let ResultProperties;
try {
  ({ default: ResultProperties } = await import(pathToFileURL(compiled).href));
} finally {
  rmSync(compiled, { force: true });
}
const html = renderToStaticMarkup(createElement(ResultProperties, {
  label: "Result properties",
  stale: true,
  properties: [
    { id: "T", name: "Temperature", value: 300, unit: "K", group: "conditions", specification: true },
    { id: "H", name: "Enthalpy", value: 1.23456789, unit: "kJ/kg", group: "thermodynamic", specification: false },
    { id: "X", name: "Odd", value: null, unit: "", group: "unknown-group", specification: false },
  ],
}));
const inputs = html.match(/<input\b[^>]*>/g) ?? [];
assert.equal(inputs.length, 1, "only the filter is an input");
assert.match(inputs[0], /type="search"/);
assert.match(inputs[0], /class="draft-result-props__filter"/);
assert.ok(!/<(select|textarea|button)\b|contenteditable/i.test(html), "result rows must be read-only");
assert.equal((html.match(/<tr class="is-spec"/g) ?? []).length, 1);
assert.equal((html.match(/<tr class="is-result"/g) ?? []).length, 2);
assert.match(html, /class="draft-result-props is-stale"/);
assert.match(html, /<span class="draft-result-props__kind">spec<\/span>/);
assert.match(html, /<span class="draft-result-props__kind">result<\/span>/);
assert.match(html, /<td>1\.23457<\/td>/, "values are shown as reported, only display-rounded");
assert.match(html, /<summary>Other <span>\(1\)<\/span><\/summary>/, "unknown groups fall into Other");
console.log("158 inputs vs results render distinctly: PASS");
