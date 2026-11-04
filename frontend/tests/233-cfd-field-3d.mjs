import assert from "node:assert/strict";
import fs from "node:fs";
import { radialStretch, reflectedSample, transforms, validField3d } from "../src/pages/cfdField3d.ts";
const surface = { name: "wall", kind: "wall", provenance: "computed", points: [[1, 2, 3], [2, 2, 3], [1, 3, 3]], polygons: [[0, 1, 2]],
  fields: { p: { values: [4, 5, 6], min: 4, max: 6, units: "m2/s2" }, U: { values: [[7, 8, 9], [1, 2, 3], [2, 3, 4]], magnitude_min: 0, magnitude_max: 14, units: "m/s" } } };
const payload = { status: "available", run_id: "r", time: "1", surfaces: [surface],
  symmetry: { domain: "quarter", axis: "x", mirror_planes: ["y=0", "z=0"], basis: "quarter case" },
  reflection_operators: { "y=0": { position: [1, -1, 1], vector_field: [1, -1, 1], scalar: "unchanged" }, "z=0": { position: [1, 1, -1], vector_field: [1, 1, -1], scalar: "unchanged" } } };
assert.equal(radialStretch([surface]), 1);
assert.ok(Math.abs(radialStretch([{ ...surface, points: [[0, 0, 0], [2.6, 0.023, 0], [1.3, 0, 0.01]] }]) - 0.25 * 2.6 / 0.023) < 1e-9);
assert.equal(validField3d(payload), true);
assert.deepEqual(transforms(payload, false).map((copy) => copy.planes), [[]]);
assert.deepEqual(transforms(payload, true).map((copy) => copy.planes), [[], ["y=0"], ["z=0"], ["y=0", "z=0"]]);
const mirrored = reflectedSample(surface, 0, transforms(payload, true)[3]);
assert.deepEqual(mirrored.position, [1, -2, -3]);
assert.deepEqual(mirrored.U, [7, -8, -9]);
assert.equal(mirrored.p, 4);
assert.deepEqual(mirrored.source, [1, 2, 3]);
const noPlanes = structuredClone(payload);
noPlanes.symmetry.mirror_planes = [];
noPlanes.reflection_operators = {};
assert.equal(validField3d(noPlanes), true);
assert.equal(transforms(noPlanes, true).length, 1);
const missing = structuredClone(payload);
delete missing.reflection_operators["z=0"];
assert.equal(validField3d(missing), false);
const panel = fs.readFileSync(new URL("../src/pages/CfdField3dPanel.tsx", import.meta.url), "utf8");
assert.match(panel, /Mirrored from the computed quarter, not computed/);
assert.match(panel, /available\.symmetry\.mirror_planes\.length > 0/);
assert.match(panel, /transforms\(available, full\)/);
assert.match(panel, /Display only: the radial axes \(y, z\) are stretched/);
assert.match(panel, /radialStretch\(available\.surfaces\)/);
assert.match(panel, /computed source point/);
assert.match(fs.readFileSync(new URL("../src/pages/CfdResultsCompare.tsx", import.meta.url), "utf8"), /run\.state === "finished" \? <CfdField3dPanel/);
console.log("233 cfd field 3d ok");
