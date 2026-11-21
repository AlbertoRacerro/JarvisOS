import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = path => readFileSync(resolve(root, path), "utf8");
const require = createRequire(import.meta.url);
const module = { exports: {} };
const code = ts.transpileModule(read("src/components/BenchRunsPanel.tsx"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
}).outputText;
new Function("require", "module", "exports", code)(name =>
  name === "../api/benchRuns" ? {} :
  name === "./ui/NumericInput" ? { __esModule: true, default: (props) => React.createElement("input", props) } :
  require(name), module, module.exports);
const { BenchRunsList, BenchRunDetailView, BenchCompareView, VERDICT_LABELS, CHECK_STATUS_LABELS, BENCH_EMPTY, verdictOf, evaluatorName } = module.exports;

assert.deepEqual(VERDICT_LABELS, {
  usable: "Utilizzabile", usable_with_warnings: "Utilizzabile con avvertenze",
  unchecked: "Non verificato", not_usable: "Non utilizzabile"
});
assert.equal(new Set(Object.values(VERDICT_LABELS)).size, 4, "four distinct verdict labels");
assert.equal(CHECK_STATUS_LABELS.not_evaluated, "Non valutato");
assert.equal(verdictOf("great"), "unchecked", "an unknown stored verdict reads as unchecked");
assert.equal(evaluatorName("bluecad.fem"), "Struttura statica (FEM)");
assert.equal(evaluatorName("bluecad.solid_code"), "Solido da codice (BlueCAD)");
assert.equal(evaluatorName("bioprocess.tube_oxygen_buildup"), "Accumulo di ossigeno in tubo");
assert.equal(evaluatorName("vendor.custom"), "vendor.custom", "unknown evaluator ids are shown as is");

const empty = renderToStaticMarkup(React.createElement(BenchRunsList, { data: { runs: [], skipped: 0 }, selected: null, marked: [] }));
assert.ok(empty.includes("i risultati compaiono qui quando l&#x27;assistente usa il banco di prova") || empty.includes("i risultati compaiono qui quando l'assistente usa il banco di prova"), "empty state");
assert.ok(BENCH_EMPTY.includes("i risultati compaiono qui quando l'assistente usa il banco di prova"));

const runs = ["usable", "usable_with_warnings", "unchecked", "not_usable"].map((verdict, i) =>
  ({ run_id: `run_${i}`, evaluator_id: "bluecad.solid", status: "succeeded", verdict, completed_at: "2026-11-17T10:00:00Z" }));
const list = renderToStaticMarkup(React.createElement(BenchRunsList, { data: { runs, skipped: 2 }, selected: null, marked: [] }));
for (const label of Object.values(VERDICT_LABELS)) assert.ok(list.includes(label), `list shows ${label}`);
assert.ok(list.includes("2 cartelle non leggibili ignorate"));

const detail = renderToStaticMarkup(React.createElement(BenchRunDetailView, { run: {
  run_id: "r", evaluator_id: "bluecad.mesh", status: "failed", verdict: "not_usable",
  failure: { code: "SOLVER", message: "non converge" }, outputs: [], outputs_withheld: true, artifacts: [], truncated: false,
  checks: [
    { id: "vol_ratio", family: "geometry", status: "not_evaluated", actual: null, limit: null, unit: null, hint: "manca la misura", source: "evaluator" },
    { id: "aspect", family: "mesh", status: "pass", actual: 1.5, limit: 3, unit: "1", hint: "ok", source: "evaluator" }
  ] } }));
assert.ok(detail.includes("Non valutato") && detail.includes("vol_ratio"), "not_evaluated shown as such");
assert.ok(detail.includes("SOLVER") && detail.includes("non converge") && detail.includes("1.5") && detail.includes("manca la misura"));
assert.ok(detail.includes("Risultati non mostrati"), "withheld outputs said plainly");

const portsDetail = renderToStaticMarkup(React.createElement(BenchRunDetailView, { run: {
  run_id: "solid-code", evaluator_id: "bluecad.solid_code", status: "succeeded", verdict: "usable",
  failure: null, outputs: [], outputs_withheld: false, artifacts: [], truncated: false, checks: [],
  ports: [{ name: "inlet", interface: "tube", origin: [0, 0, 0], direction: [-1, 0, 0], outer_d: 12 }]
} }));
assert.ok(portsDetail.includes("Porte"), "ports heading shown");
assert.ok(portsDetail.includes("inlet (tube, Ø 12 mm)"), "tube port and diameter shown");

const cmp = renderToStaticMarkup(React.createElement(BenchCompareView, { doc: {
  run_id_a: "a", run_id_b: "b", evaluator_a: "bluecad.solid", evaluator_b: "bluecad.solid", status_a: "succeeded", status_b: "succeeded",
  verdict_a: "usable", verdict_b: "unchecked", truncated: false,
  outputs: { volume: { a: { value: 12, unit: "mm3" }, b: { value: 15, unit: "mm3" }, unit: "mm3", absolute_difference: 3, relative_difference: 0.25, mark: "match" } },
  checks: { c1: { status_a: "pass", actual_a: 1, status_b: "not_evaluated", actual_b: null } } } }));
assert.ok(cmp.includes("12 mm3") && cmp.includes("3 mm3") && cmp.includes("Non valutato"));

assert.ok(read("src/pages/RunsWorkbench.tsx").includes("<BenchRunsPanel"), "the section lives on the Runs screen");
assert.ok(/createBenchRun/.test(read("src/api/benchRuns.ts")), "the bench API now supports starting correlation runs");
console.log("bench runs view: OK");
