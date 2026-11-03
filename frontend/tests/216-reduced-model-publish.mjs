// Spec 216 contract 7: publishing a reduced model names server-measured runs only and shows why runs are unusable.
import assert from "node:assert/strict";
import fs from "node:fs";
import { classifyRuns, emptySelection, planPublication, runIneligibility } from "../src/pages/reducedModelPublish.ts";

const m = (extra = {}) => ({ status: "measured", reason: null, pressure_gradient_pa_m: 2.5, reynolds: 1000, mesh_level: 1, cells: 1000, case_manifest_digest: "d", ...extra });
const run = (id, extra = {}, regime = "laminar") => ({ run_id: id + "-xxxxxxxx", regime, state: "finished", residual_converged: true, measurement: m(), ...extra });
const good = [run("p1", { measurement: m({ reynolds: 500 }) }), run("p2"),
  run("m1", { measurement: m({ mesh_level: 0, cells: 500 }) }), run("m2"), run("m3", { measurement: m({ mesh_level: 2, cells: 2000 }) })];
const id = (s) => good.find((r) => r.run_id.startsWith(s)).run_id;
const component = { revision: 4 };
const sel = (over = {}) => ({ ...emptySelection(), modelId: "pipe-1", tolerances: { laminar: "0.1", turbulent_rans: "0.2" },
  points: { laminar: [id("p1"), id("p2")], turbulent_rans: [] }, meshStudy: { laminar: [id("m1"), id("m2"), id("m3")], turbulent_rans: [] }, ...over });

// ineligible runs carry reasons, never usable
assert.deepEqual(runIneligibility(run("a")), []);
assert.match(runIneligibility(run("a", { state: "stopped" }))[0], /stopped, not finished/);
assert.match(runIneligibility(run("a", { state: "running" }))[0], /running/);
assert.match(runIneligibility(run("a", { measurement: { status: "unmeasured", reason: "no probe files" } }))[0], /unmeasured: no probe files/);
assert.match(runIneligibility(run("a", { measurement: undefined }))[0], /unmeasured/);
assert.match(runIneligibility(run("a", { residual_converged: false })).join(), /not converged/);
assert.match(runIneligibility(run("a", { measurement: m({ case_manifest_digest: null }) })).join(), /manifest/);
assert.match(runIneligibility(run("a", { measurement: m({ pressure_gradient_pa_m: null }) })).join(), /pressure gradient/);
assert.match(runIneligibility(run("a", { measurement: m({ pressure_gradient_pa_m: -1 }) })).join(), /positive/);
const grouped = classifyRuns([run("a"), run("b", { state: "stopped" }), run("t", {}, "turbulent_rans")]);
assert.equal(grouped.laminar.length, 2); assert.equal(grouped.turbulent_rans.length, 1);
assert.equal(grouped.laminar.find((x) => x.run.run_id.startsWith("b")).eligible, false);

// valid selection yields the exact backend body: run ids only, no points or discretization
const ok = planPublication(good, sel(), component, "v1");
assert.deepEqual(ok.reasons, []);
assert.deepEqual(Object.keys(ok.body).sort(), ["expected_active_version_id", "expected_revision", "mesh_study_run_ids", "model_id", "run_ids", "tolerances"]);
assert.deepEqual(ok.body.mesh_study_run_ids, { laminar: sel().meshStudy.laminar });
assert.equal(ok.body.expected_revision, 4); assert.equal(ok.body.tolerances.turbulent_rans, 0.2);

const refused = (s, re, runs = good) => { const p = planPublication(runs, s, component, "v1"); assert.equal(p.body, null); assert.match(p.reasons.join("\n"), re); };
refused(sel({ modelId: " " }), /model id/);
refused(sel({ points: { laminar: [], turbulent_rans: [] } }), /at least one study run/);
refused(sel({ points: { laminar: [id("p2")], turbulent_rans: [] } }), /two or more Reynolds/);
refused(sel({ meshStudy: { laminar: [id("m1"), id("m2")], turbulent_rans: [] } }), /exactly 3 distinct/);
refused(sel({ meshStudy: { laminar: [id("m1"), id("m2"), id("m2")], turbulent_rans: [] } }), /exactly 3 distinct/);
refused(sel({ meshStudy: { laminar: [], turbulent_rans: [id("m1")] } }), /no turbulent_rans study run/);
refused(sel({ tolerances: { laminar: "1", turbulent_rans: "0" } }), /tolerance/);
refused(sel(), /did not meet|not converged/, good.map((r) => r.run_id.startsWith("m3") ? { ...r, residual_converged: false } : r));
refused(sel(), /unmeasured: gone/, good.map((r) => r.run_id.startsWith("p1") ? { ...r, measurement: { status: "unmeasured", reason: "gone" } } : r));
refused(sel(), /distinct mesh levels/, good.map((r) => r.run_id.startsWith("m3") ? { ...r, measurement: m({ mesh_level: 1, cells: 2000 }) } : r));
refused(sel(), /mesh level is not in the mesh study/, good.map((r) => r.run_id.startsWith("p") ? { ...r, measurement: { ...r.measurement, mesh_level: 5 } } : r));
refused(sel(), /no mesh cell count/, good.map((r) => r.run_id.startsWith("m1") ? { ...r, measurement: m({ mesh_level: 0, cells: null }) } : r));
refused(sel(), /share one Reynolds/, good.map((r) => r.run_id.startsWith("m1") ? { ...r, measurement: m({ mesh_level: 0, cells: 500, reynolds: 900 }) } : r));

const panel = fs.readFileSync(new URL("../src/pages/ReducedModelPublishPanel.tsx", import.meta.url), "utf8");
assert.match(panel, /Not usable/);
assert.match(panel, /nothing is qualified here/);
assert.doesNotMatch(panel, /qualified reduced model|validated model/i);
const api = fs.readFileSync(new URL("../src/api/designProject.ts", import.meta.url), "utf8");
assert.match(api, /measurement\?: CfdRunMeasurement/);
assert.doesNotMatch(api.slice(api.indexOf("ReducedModelPublication = {")), /discretization:|points:/);
console.log("216 reduced model publish ok");
