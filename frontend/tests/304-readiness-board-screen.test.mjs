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

const code = ts.transpileModule(read("src/pages/ReadinessBoard.tsx"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
}).outputText;

new Function("require", "module", "exports", code)(
  (name) => (name === "../api/readinessBoard" || name === "../app/AppLink" ? {} : require(name)),
  module,
  module.exports
);

const ReadinessBoard = module.exports.default;
const {
  READINESS_TITLE,
  READINESS_EMPTY_STATE,
  STEP_ORDER,
  STEP_LABELS,
  REASON_WORKSPACE_MISSING,
  REASON_NOT_DECLARED,
  REASON_UNIT_MISMATCH,
  REASON_OUT_OF_TOLERANCE,
  MISSING_RECORD_SENTENCES,
  STEP_LINKS,
  getReasonText,
  formatPercentSigFigs
} = module.exports;

// 1. Page title and empty state text
assert.equal(READINESS_TITLE, "Prontezza", "title is Prontezza");
assert.equal(
  READINESS_EMPTY_STATE,
  "Nessun caso di letteratura installato: i casi arrivano dal pacchetto di progetto",
  "empty state sentence verbatim"
);

// 2. Seven steps and short Italian labels
assert.deepEqual(
  STEP_ORDER,
  ["paper", "facts", "bound", "run", "compare", "decide", "answer"],
  "seven steps in order"
);

assert.equal(STEP_LABELS.paper, "Articolo");
assert.equal(STEP_LABELS.facts, "Dati");
assert.equal(STEP_LABELS.bound, "Ingressi legati");
assert.equal(STEP_LABELS.run, "Calcolo");
assert.equal(STEP_LABELS.compare, "Confronto");
assert.equal(STEP_LABELS.decide, "Decisione");
assert.equal(STEP_LABELS.answer, "Risposta");
assert.equal(new Set(Object.values(STEP_LABELS)).size, 7, "seven distinct step labels");

// 3. Reason sentences in Italian
assert.equal(REASON_WORKSPACE_MISSING, "Il workspace del caso non esiste");
assert.equal(REASON_NOT_DECLARED, "Il caso non dichiara un valore pubblicato da confrontare");
assert.equal(REASON_UNIT_MISMATCH, "Le unità del risultato e del valore pubblicato non sono confrontabili");
assert.equal(REASON_OUT_OF_TOLERANCE, "Il risultato è fuori dalla tolleranza del caso");
assert.equal(
  MISSING_RECORD_SENTENCES.facts,
  "Mancano dati accettati e verificati per:",
  "facts missing_record verbatim example from spec"
);
assert.equal(
  MISSING_RECORD_SENTENCES.compare,
  "Nessun risultato utilizzabile riporta la grandezza confrontata:",
  "compare missing_record verbatim from spec 305"
);
for (const step of STEP_ORDER) {
  assert.ok(MISSING_RECORD_SENTENCES[step]?.length > 0, `missing_record sentence for ${step}`);
}

assert.equal(getReasonText("paper", "workspace_missing"), "Il workspace del caso non esiste");
assert.equal(getReasonText("compare", "not_declared"), "Il caso non dichiara un valore pubblicato da confrontare");
assert.equal(getReasonText("compare", "unit_mismatch"), "Le unità del risultato e del valore pubblicato non sono confrontabili");
assert.equal(getReasonText("compare", "out_of_tolerance"), "Il risultato è fuori dalla tolleranza del caso");
assert.equal(getReasonText("compare", "missing_record"), "Nessun risultato utilizzabile riporta la grandezza confrontata:");
assert.equal(getReasonText("facts", "missing_record"), "Mancano dati accettati e verificati per:");
for (const sentence of Object.values(MISSING_RECORD_SENTENCES)) {
  assert.ok(!/[a-z]+[._][a-z]+/.test(sentence), `no record-kind code in visible sentence: ${sentence}`);
}
assert.ok(!read("src/pages/ReadinessBoard.tsx").includes("err.message : "), "load failure shown in Italian, not the raw error");

// 4. Step link targets
assert.equal(STEP_LINKS.paper.path, "/memory/literature");
assert.equal(STEP_LINKS.facts.path, "/memory/literature");
assert.equal(STEP_LINKS.bound.path, "/memory/literature");
assert.equal(STEP_LINKS.run.path, "/runs");
assert.equal(STEP_LINKS.compare.path, "/runs");
assert.equal(STEP_LINKS.decide, undefined);
assert.equal(STEP_LINKS.answer.path, "/memory/search");

// 5. Comparison formatting
assert.equal(formatPercentSigFigs(0), "0%");
assert.equal(formatPercentSigFigs(1.1e-16), "0%");
assert.equal(formatPercentSigFigs(0.00012), "0.012%");
assert.equal(formatPercentSigFigs(0.00342), "0.34%");
assert.equal(formatPercentSigFigs(0.0215), "2.1%");
assert.equal(formatPercentSigFigs(0.05), "5.0%");
assert.equal(formatPercentSigFigs(0.154), "15%");
assert.equal(formatPercentSigFigs(1.2), "120%");

// 5. Source code constraints
const pageSource = read("src/pages/ReadinessBoard.tsx");
assert.ok(!pageSource.includes("stepResult.detail"), "never render detail from StepResult");
assert.ok(!pageSource.includes(".detail}"), "never render detail property in JSX");
assert.ok(pageSource.includes("onWorkspaceChange?.(selectedCase.workspace_id)"), "link calls onWorkspaceChange with workspace_id");
assert.ok(pageSource.includes('Primo passo mancante:'), "leftmost red cell Italian label in summary");
assert.ok(pageSource.includes("<th>Calcolato</th>"), "comparison table has Calcolato header");
assert.ok(pageSource.includes("<th>Pubblicato</th>"), "comparison table has Pubblicato header");
assert.ok(pageSource.includes("<th>Scarto</th>"), "comparison table has Scarto header");
assert.ok(pageSource.includes("<th>Tolleranza</th>"), "comparison table has Tolleranza header");
assert.ok(pageSource.includes("<th>Run</th>"), "comparison table has Run header");
assert.ok(
  pageSource.includes("stepResult.comparison.run_label ?? stepResult.comparison.run_id"),
  "comparison table prefers run_label and posts the raw run_id unchanged"
);
assert.ok(
  pageSource.includes("run_id: stepResult.comparison.run_id"),
  "verdict form posts comparison.run_id unchanged"
);

// 6. Route wiring in routes.ts and App.tsx
const routes = read("src/app/routes.ts");
assert.ok(routes.includes('"development-readiness"'), "routes.ts declares development-readiness RouteId");
assert.ok(routes.includes('path: "/development/readiness"'), "routes.ts declares /development/readiness path");
assert.ok(routes.includes('title: "Prontezza"'), "routes.ts declares Prontezza title");
assert.ok(routes.includes('{ label: "Prontezza", href: "/development/readiness" }'), "routes.ts includes Prontezza in PEER_NAV_ITEMS.development");

const header = read("src/components/fusion/FinalWorkspaceHeader.tsx");
assert.ok(header.includes('["Prontezza", "/development/readiness", "readiness"]'), "FinalWorkspaceHeader includes Prontezza tab");

const app = read("src/App.tsx");
assert.ok(app.includes('case "development-readiness":'), "App.tsx routes development-readiness");
assert.ok(app.includes('<FinalWorkspaceHeader group="development" active="readiness"'), "App.tsx renders FinalWorkspaceHeader with group development and active readiness");
assert.ok(app.includes("<ReadinessBoard"), "App.tsx renders ReadinessBoard");
assert.ok(app.includes('"development-readiness"') && app.includes("WORKSPACE_OPTIONAL_ROUTES"), "development-readiness is workspace optional");

// 7. Initial loading render
const html = renderToStaticMarkup(React.createElement(ReadinessBoard, { navigate() {} }));
assert.ok(html.includes("Caricamento"), "rendered page has loading state");

console.log("304 readiness board frontend test: OK");
