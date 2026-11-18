import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const require = createRequire(import.meta.url);
const module = { exports: {} };
const code = ts.transpileModule(read("src/components/ActivityPanel.tsx"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
new Function("require", "module", "exports", code)(
  (name) => (name === "../api/coding" ? {} : require(name)),
  module,
  module.exports
);
const { ActivityView, NO_DOWNLOADS } = module.exports;

assert.equal(NO_DOWNLOADS, "nessun download registrato");

const baseData = (downloads = []) => ({
  generated_at: "2026-11-18T12:00:00Z",
  running: downloads.filter((d) => d.state === "running").length,
  rows: [],
  groups: [],
  quota: { state: "ok", pools: [] },
  downloads,
  downloads_empty: NO_DOWNLOADS,
});

const now = Date.parse("2026-11-18T12:00:00Z");
const render = (downloads) =>
  renderToStaticMarkup(React.createElement(ActivityView, { data: baseData(downloads), now }));

// 1. Empty downloads section
const emptyHtml = render([]);
assert.ok(emptyHtml.includes("nessun download registrato"), "shows empty text when no downloads");
assert.ok(!emptyHtml.includes("<progress"), "no progress bar when no downloads");

// 2. Download rows with progress bar and state
const runningDownload = {
  key: "download:d-run",
  kind: "download",
  title: "Download di weights.gguf",
  runner: "motore locale",
  started_at: "2026-11-18T11:55:00Z",
  ended_at: null,
  state: "running",
  state_word: "in corso",
  progress: { done: 500, total: 1000, unit: "MB" },
  step: "sto scaricando",
  outcome: null,
  links: [],
};

const finishedDownload = {
  key: "download:d-fin",
  kind: "download",
  title: "Download di dataset.parquet",
  runner: "motore locale",
  started_at: "2026-11-18T11:40:00Z",
  ended_at: "2026-11-18T11:50:00Z",
  state: "done",
  state_word: "completato",
  progress: { done: 200, total: 200, unit: "MB" },
  step: null,
  outcome: "completato",
  links: [],
};

const filledHtml = render([runningDownload, finishedDownload]);
assert.ok(!filledHtml.includes("nessun download registrato"), "does not show empty text when downloads present");
assert.ok(filledHtml.includes("Download di weights.gguf"), "shows running download title");
assert.ok(filledHtml.includes("Download di dataset.parquet"), "shows finished download title");
assert.ok(filledHtml.includes("motore locale"), "shows runner motore locale");
assert.ok(filledHtml.includes("sto scaricando"), "shows step sto scaricando");
assert.ok(filledHtml.includes('<progress value="500" max="1000"'), "renders megabyte progress bar");
assert.ok(filledHtml.includes('<progress value="200" max="200"'), "renders completed progress bar");

console.log("download state tests: OK");
