import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");
const view = read("components/process/PbrResults.tsx");
const types = read("api/processDraft.ts");

for (const key of ["circulation_flow_m3_h", "pass_transit_time_s", "circulation_to_throughflow_ratio"]) {
  assert.match(view, new RegExp(key), `${key} must appear in the PBR results view`);
  assert.match(types, new RegExp(`${key}\\?: ReportedValue`), `${key} must be typed`);
}
assert.match(view, /title: "Internal circulation"/);
assert.equal((view.match(/process-inlet basis/g) ?? []).length, 3,
  "dilution, residence time and volumetric flow must say process-inlet basis");
assert.match(types, /PbrCirculationOutputs/);

console.log("184 PBR circulation frontend contract passed");
