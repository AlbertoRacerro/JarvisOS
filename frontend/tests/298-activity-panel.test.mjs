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
const code = ts.transpileModule(read("src/components/ActivityPanel.tsx"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
}).outputText;
new Function("require", "module", "exports", code)(name => name === "../api/coding" ? {} : require(name), module, module.exports);
const { ActivityView, ACTIVITY_LABEL } = module.exports;

assert.equal(ACTIVITY_LABEL, "Attività");
assert.ok(read("src/components/Layout.tsx").includes("<ActivityControl />"), "the control lives in the shell on every screen");

const row = (extra = {}) => ({ key: "k", kind: "job", title: "Un lavoro di calcolo registrato", runner: "lavoro notturno",
  started_at: "2026-11-17T11:00:00Z", ended_at: null, state: "running", state_word: "in corso", progress: null,
  step: "sto elaborando", outcome: null, links: [], ...extra });
const data = rows => ({ generated_at: "x", running: rows.length, rows, groups: [{ id: "dynamic", title: "Simulazioni dinamiche", state: "non disponibile", reason: "lettura non riuscita" }],
  quota: { state: "ok", pools: [{ label: "Claude", percent_free: 60 }] }, downloads: [], downloads_empty: "nessun download registrato" });
const now = Date.parse("2026-11-17T12:00:00Z");
const render = rows => renderToStaticMarkup(React.createElement(ActivityView, { data: data(rows), now }));

const none = render([row()]);
assert.ok(none.includes("nessun download registrato"), "empty download group");
assert.ok(none.includes("activity-spinner") && !none.includes("<progress"), "no total: indeterminate spinner");
assert.ok(none.includes("1 h 0 min"), "elapsed time shown");
assert.ok(none.includes("Claude 60% libero") && none.includes("non disponibile (lettura non riuscita)"));
const bar = render([row({ progress: { done: 40, total: 100, unit: "%" } })]);
assert.ok(bar.includes("<progress") && !bar.includes("activity-spinner"), "progress bar only with a total");
console.log("activity panel: OK");
