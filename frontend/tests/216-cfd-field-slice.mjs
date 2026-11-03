// Spec 216: field slice model - association averaging, projection/fit, colormap, strict validation, honest passthrough.
import assert from "node:assert/strict";
import fs from "node:fs";
import { buildPolygons, colorFor, COLOR_STOPS, fitTransform, parseFieldPayload, polygonValues, projectPoints, toScreen } from "../src/pages/cfdFieldSlice.ts";

const points = [[0, 0, 0], [2, 0, 0], [2, 1, 0], [0, 1, 0], [4, 1, 0]];
const polygons = [[0, 1, 2, 3], [1, 4, 2]];
const base = (association, pv, uv) => ({
  status: "available", time: "100", plane: { normal: [0, 0, 1], axis: "x" }, association, points, polygons, point_count: 5, cell_count: 2, source_files: ["a.vtk"],
  fields: { p: { values: pv, min: Math.min(...pv), max: Math.max(...pv), units: "m2/s2" }, U: { values: uv, magnitude_min: 0, magnitude_max: 5, units: "m/s" } },
});
const pointPayload = base("point", [0, 4, 8, 4, 12], [[3, 4, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]]);
const cellPayload = base("cell", [10, 20], [[3, 4, 0], [0, 0, 2]]);

const point = parseFieldPayload(pointPayload);
assert.equal(point.kind, "available");
assert.deepEqual(polygonValues(point.slice, "p"), [(0 + 4 + 8 + 4) / 4, (4 + 12 + 8) / 3]);
assert.equal(polygonValues(point.slice, "Umag")[0], 5 / 4);
const cell = parseFieldPayload(cellPayload);
assert.deepEqual(polygonValues(cell.slice, "p"), [10, 20]);
assert.deepEqual(polygonValues(cell.slice, "Umag"), [5, 2]);

// projection drops the normal axis; fit preserves aspect and stays inside the viewport
const p2 = projectPoints(point.slice);
assert.deepEqual(p2[4], [4, 1]);
const fit = fitTransform(p2, 200, 100, 10);
assert.equal(fit.scale, 45);  // x span 4 -> 180 available
const screen = p2.map((p) => toScreen(fit, p));
for (const [x, y] of screen) { assert.ok(x >= 10 - 1e-9 && x <= 190 + 1e-9 && y >= 10 - 1e-9 && y <= 90 + 1e-9); }
assert.ok(screen[2][1] < screen[1][1]);  // y up on screen
// Spec 229: the points decide the dropped axis. A declared normal the flat points disagree with is not followed
// (here the points are flat in z although x is declared); a declared axis the points are flat along is kept.
const side = projectPoints({ ...point.slice, normal: [1, 0, 0] });
assert.deepEqual(side[1], [2, 0]);
const onY = projectPoints({ ...point.slice, points: point.slice.points.map(([x, y]) => [x, 0, y]), normal: [0, 0, 1] });
assert.deepEqual(onY[2], [2, 1], "a section on y = 0 declared with normal z keeps its x and z extent");
const flatX = projectPoints({ ...point.slice, points: point.slice.points.map(([x, y]) => [0, x, y]), normal: [1, 0, 0] });
assert.deepEqual(flatX[4], [4, 1]);
const degenerate = fitTransform([[1, 1], [1, 1]], 100, 100);
assert.ok(Number.isFinite(degenerate.scale) && Number.isFinite(degenerate.offsetX) && Number.isFinite(degenerate.offsetY));
assert.equal(buildPolygons(point.slice, "p", 200, 100).polygons.length, 2);

// colormap endpoints, clamping, min == max, non-finite
assert.equal(colorFor(0, 0, 1), `rgb(${COLOR_STOPS[0].join(",")})`);
assert.equal(colorFor(1, 0, 1), `rgb(${COLOR_STOPS.at(-1).join(",")})`);
assert.equal(colorFor(-5, 0, 1), colorFor(0, 0, 1));
assert.equal(colorFor(9, 0, 1), colorFor(1, 0, 1));
assert.equal(colorFor(3, 3, 3), colorFor(0.5, 0, 1));
assert.ok(!/NaN/.test(colorFor(3, 3, 3)) && !/NaN/.test(colorFor(NaN, 0, 1)));

// malformed payloads are rejected, never repaired
const bad = (mutate) => { const copy = structuredClone(pointPayload); mutate(copy); return parseFieldPayload(copy); };
assert.equal(bad((p) => p.fields.p.values.pop()).kind, "invalid");
assert.equal(bad((p) => p.fields.U.values.pop()).kind, "invalid");
assert.equal(bad((p) => { p.association = "cell"; }).kind, "invalid");  // 5 values vs 2 cells
assert.equal(bad((p) => { p.polygons[0][0] = 99; }).kind, "invalid");
assert.equal(bad((p) => { p.polygons[0][0] = -1; }).kind, "invalid");
assert.equal(bad((p) => { p.polygons[0] = [0, 1]; }).kind, "invalid");
assert.equal(bad((p) => { p.points[0] = [0, 0]; }).kind, "invalid");
assert.equal(bad((p) => { p.fields.p.values[0] = null; }).kind, "invalid");
assert.equal(bad((p) => { p.fields.p.min = 99; }).kind, "invalid");
assert.equal(bad((p) => { p.association = "face"; }).kind, "invalid");
assert.equal(bad((p) => { p.plane.normal = [1, 1, 0]; }).kind, "invalid");
assert.equal(parseFieldPayload(null).kind, "invalid");
assert.equal(parseFieldPayload({ status: "weird" }).kind, "invalid");

// unavailable / too_large pass their reason through verbatim
assert.deepEqual(parseFieldPayload({ status: "unavailable", reason: "the run is running; fields exist only for a finished run" }),
  { kind: "unavailable", reason: "the run is running; fields exist only for a finished run" });
assert.deepEqual(parseFieldPayload({ status: "too_large", reason: "slice exceeds 40000 cells", time: "5", cells: 50000 }),
  { kind: "too_large", reason: "slice exceeds 40000 cells", time: "5" });
assert.equal(parseFieldPayload({ status: "unavailable" }).kind, "invalid");

const panel = fs.readFileSync(new URL("../src/pages/CfdFieldSlicePanel.tsx", import.meta.url), "utf8");
assert.match(panel, /state === "finished"/);
console.log("216 cfd field slice ok");
