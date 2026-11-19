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

// 1. Check frontend api module caseVerdicts.ts
const apiSource = read("src/api/caseVerdicts.ts");
assert.ok(apiSource.includes("export async function listCaseVerdicts"), "exports listCaseVerdicts");
assert.ok(apiSource.includes("export async function createCaseVerdict"), "exports createCaseVerdict");
assert.ok(apiSource.includes("export async function updateCaseVerdict"), "exports updateCaseVerdict");
assert.ok(apiSource.includes("/engineering/case-verdicts"), "calls engineering/case-verdicts endpoint");

// 2. Transpile and load ReadinessBoard
const code = ts.transpileModule(read("src/pages/ReadinessBoard.tsx"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
}).outputText;

const module = { exports: {} };
new Function("require", "module", "exports", code)(
  (name) => (name === "../api/readinessBoard" || name === "../app/AppLink" || name === "../api/caseVerdicts" ? {} : require(name)),
  module,
  module.exports
);

const ReadinessBoard = module.exports.default;
assert.ok(ReadinessBoard, "ReadinessBoard component exported");

// 3. Exact Italian labels in ReadinessBoard.tsx source
const pageSource = read("src/pages/ReadinessBoard.tsx");
const requiredLabels = [
  "Registra verdetto",
  "Accetta verdetto",
  "Entro tolleranza",
  "Fuori tolleranza",
  "Registrato",
  "Accettato",
];

for (const label of requiredLabels) {
  assert.ok(
    pageSource.includes(label),
    `ReadinessBoard must contain visible Italian label "${label}"`
  );
}

// 4. Source code structure assertions
assert.ok(pageSource.includes("readiness-board__verdict-panel"), "renders verdict panel");
assert.ok(pageSource.includes("readiness-board__verdict-badge"), "renders verdict badge");
assert.ok(pageSource.includes("readiness-board__verdict-status"), "renders verdict status");
assert.ok(pageSource.includes("readiness-board__rationale-input"), "renders rationale input");
assert.ok(pageSource.includes("readiness-board__record-verdict-button"), "renders record verdict button");
assert.ok(pageSource.includes("readiness-board__accept-verdict-button"), "renders accept verdict button");

// 5. Initial render works cleanly without errors
const initialHtml = renderToStaticMarkup(React.createElement(ReadinessBoard, { navigate() {} }));
assert.ok(initialHtml.includes("Caricamento"), "initial render shows loading state");

console.log("314 case verdicts frontend test: OK");
