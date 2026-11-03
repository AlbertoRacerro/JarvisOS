// CFD Studies and Results are one node: Results shows the compared runs stacked in the viewport, and the inspector
// lists every run with a checkbox (latest finished checked by default) above the folded study templates.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const require = createRequire(import.meta.url);
function load(path, imports = {}) {
  const module = { exports: {} };
  const code = ts.transpileModule(read(path), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 }
  }).outputText;
  new Function("require", "module", "exports", code)((name) => imports[name] ?? require(name), module, module.exports);
  return module.exports;
}

// ---- One merged node; old links still land on it ----
const targets = load("src/app/designTargets.ts");
const tree = load("src/components/design/DesignComponentTree.tsx", { "../../app/designTargets": targets });
const view = load("src/pages/designView.ts", { "../components/design/DesignComponentTree": tree });
assert.ok(tree.DESIGN_NODES.includes("Results") && !tree.DESIGN_NODES.includes("Studies"), "Studies is merged into Results");
assert.equal(view.STAGE_NODE.CFD, "Results", "the CFD stage opens on Results");
assert.equal(view.readDesignView("?stage=cfd&component=c-1&node=studies").node, "Results", "node=studies maps to Results");
assert.ok(!("design.tree.node.studies" in targets.DESIGN_TARGETS) && "design.tree.node.results" in targets.DESIGN_TARGETS);
const { cfdNodeStatus } = load("src/components/design/designNodeStatus.ts");
assert.equal(cfdNodeStatus({ state: "ready", templates: 1, runs: [], reduced: [] }, "Studies"), undefined);

// ---- Compare selection ----
const compare = load("src/pages/cfdResultsCompare.ts");
const runs = [
  { run_id: "a", state: "finished", queued_at: "2026-10-01T10:00:00Z", finished_at: "2026-10-01T10:05:00Z" },
  { run_id: "b", state: "failed", queued_at: "2026-10-03T10:00:00Z" },
  { run_id: "c", state: "finished", queued_at: "2026-10-02T10:00:00Z", finished_at: "2026-10-02T10:05:00Z" }
];
assert.deepEqual(compare.newestFirst(runs).map((run) => run.run_id), ["b", "c", "a"]);
assert.deepEqual(compare.defaultCompareSelection(runs), ["c"], "the latest finished run is compared by default");
assert.deepEqual(compare.defaultCompareSelection([runs[1]]), ["b"], "without a finished run, the latest run");
assert.deepEqual(compare.defaultCompareSelection([]), []);
assert.deepEqual(compare.comparedRuns(runs, null).map((run) => run.run_id), ["c"]);
const both = compare.toggleCompared(runs, null, "a");
assert.deepEqual(both, ["c", "a"], "checking a run adds it to the default selection");
assert.deepEqual(compare.comparedRuns(runs, both).map((run) => run.run_id), ["c", "a"], "compared runs stack newest first");
assert.deepEqual(compare.toggleCompared(runs, both, "c"), ["a"], "unchecking removes it");
assert.deepEqual(compare.comparedRuns(runs, ["gone", "a"]).map((run) => run.run_id), ["a"], "unknown ids are dropped");
assert.deepEqual(compare.runBadge(runs[0]), { state: "ready", detail: "Finished" });
assert.equal(compare.runBadge(runs[1]).state, "failed");
assert.equal(compare.runBadge({ ...runs[1], state: "running" }).state, "running");

// ---- Wiring on the page ----
const page = read("src/pages/DesignProject.tsx");
const blocks = read("src/pages/CfdResultsCompare.tsx");
const slice = read("src/pages/CfdFieldSlicePanel.tsx");
assert.ok(page.includes("<CfdResultBlocks") && page.includes("<CfdRunPicker runs={cfdRuns} chosen={compareIds} onChoose={setCompareIds} />"), "Results shows the blocks and the picker");
assert.ok(page.includes('node: stage === "CFD" ? "Results" : "Parameters"'), "selecting a component on CFD opens Results");
assert.ok(/<summary>Study templates<\/summary>\s*<StudyTemplatesPanel/.test(page), "study templates are folded under the comparison");
assert.ok(!page.includes('selectedNode === "Studies"'), "no separate Studies screen");
assert.ok(page.includes('designTarget("design.field.expand")'), "the Expand field toggle stays on Results");
assert.ok(blocks.includes('type="checkbox"') && blocks.includes('<legend>Compare runs</legend>') && blocks.includes('designTarget("design.field.run")'), "each run has a compare checkbox");
assert.ok(blocks.includes("shown.map((run, index)") && blocks.includes("primary={index === 0}"), "one block per compared run, only the first carries the stable targets");
assert.ok(blocks.includes('<DesignStatus state={badge.state}'), "without a run the same block reads empty with a status dot");
assert.ok(slice.includes("run: CfdRun | null") && !slice.includes("listComponentCfdRuns"), "the field section renders the run it is given");

assert.match(read("package.json"), /"test:cfd-compare": "node tests\/cfd-results-compare\.mjs"/);
assert.match(read("package.json"), /npm run test:cfd-compare/);
console.log("cfd results compare ok");
