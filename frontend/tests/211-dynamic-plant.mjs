import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const js = ts.transpileModule(read("src/components/dynamic/dynamicLogic.ts"), { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText;
const L = await import(`data:text/javascript;base64,${Buffer.from(js).toString("base64")}`);

const manifest = {
  fidelity: "T1", units: [{ tag: "PBR_A", model_card_qualification: "not validated" }, { tag: "PBR_B", model_card_qualification: "limited" }],
  event_log: [{}], controller_log: [], productivity: { volumetric_kg_m3_day: 0.5, harvest_kg: 6 },
  campaign: { loops: { culture: { cycles: [], summary: { draws: 2, harvested_biomass_kg: 3, last_k: 1, mean_last_k_net_volumetric_productivity_kg_m3_day: 0.2 } } } },
};
const series = { channels: { t_s: [0, 10], PBR_A_X: [1, 2], PBR_B_X: [3, 4], harvest_X_kg: [0, 5] }, returned_points: 2, total_points: 20, stride: 10 };
const metrics = L.plantMetrics(manifest, series);
const get = (id) => metrics.find((item) => item.id === id);
assert.deepEqual([get("harvest").scope, get("harvest").owner], ["plant", null]);
assert.equal(get("harvest").value, 6, "the engine manifest aggregate prevails over a sampled series point");
assert.deepEqual([get("vol_prod").scope, get("vol_prod").owner], ["plant", null]);
assert.deepEqual([get("final_PBR_A").scope, get("final_PBR_A").owner], ["equipment", "PBR_A"]);
assert.deepEqual([get("final_PBR_B").scope, get("final_PBR_B").owner], ["equipment", "PBR_B"]);
assert.deepEqual([get("campaign_draws_culture").scope, get("campaign_draws_culture").owner], ["loop", "culture"]);
assert.match(get("mean_PBR_A").qualification, /sampled points from 20/);
assert.match(get("final_PBR_B").qualification, /model card: limited/);
assert.equal(metrics.filter((item) => item.scope === "plant").some((item) => item.id.startsWith("final_")), false);
assert.equal(L.comparablePlantMetrics(get("campaign_prod_culture"), { ...get("campaign_prod_culture"), label: "Mean last-2 productivity, loop culture" }), false);
assert.equal(L.comparablePlantMetrics(get("campaign_prod_culture"), get("campaign_prod_culture")), true);

const saved = { runIds: ["r1", "removed-run", "r2", "r1"], metricIds: ["harvest", "gone", "final_PBR_A"], overviewIds: ["harvest", "final_PBR_A"], equipmentId: "removed-unit", view: "charts" };
const restored = L.restorePlantChoices(saved, ["r1", "r2"], metrics.map((item) => item.id), ["a", "b"], metrics.filter((item) => item.scope === "plant").map((item) => item.id));
assert.deepEqual(restored.choices, { runIds: ["r1", "r2"], metricIds: ["harvest", "final_PBR_A"], overviewIds: ["harvest"], equipmentId: null, view: "charts" });
assert.deepEqual(restored.removed, ["run removed-run", "metric gone", "overview KPI final_PBR_A", "equipment removed-unit"]);
assert.deepEqual(L.movePlantMetric(["harvest", "events", "final_PBR_A"], "final_PBR_A", -1), ["harvest", "final_PBR_A", "events"]);
assert.deepEqual(L.movePlantMetric(["harvest", "events"], "harvest", -1), ["harvest", "events"]);
assert.notEqual(L.plantChoiceKey("ws1", "draft"), L.plantChoiceKey("ws2", "draft"));

const view = read("src/components/dynamic/PlantView.tsx");
assert.match(view, /Process flow diagram · PFD/);
assert.match(view, /not a piping and instrumentation diagram \(P&ID\)/);
assert.match(view, /aria-label="Shared KPI comparison"/);
assert.match(view, /onKeyDown=.*Enter.*event\.key === " "/s);
assert.match(read("src/components/dynamic/DynamicPanel.tsx"), /<PlantView/);
console.log("211 Dynamic Studio plant information architecture: passed");
