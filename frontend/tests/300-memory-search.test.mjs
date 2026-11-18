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
const code = ts.transpileModule(read("src/pages/MemorySearch.tsx"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
}).outputText;
new Function("require", "module", "exports", code)(
  (name) => (name === "../api/memorySearch" || name === "../api/client" ? {} : require(name)),
  module,
  module.exports
);

const MemorySearch = module.exports.default;
const {
  MEMORY_SEARCH_TITLE,
  MEMORY_SEARCH_EMPTY,
  FOLDER_BOX_TITLE,
  FOLDER_UNREGISTERED,
  FOLDER_INVALID_MESSAGE,
  OWNER_NAMES,
  ownerLabel
} = module.exports;

// 1. Page title "Ricerca"
assert.equal(MEMORY_SEARCH_TITLE, "Ricerca", "page title is Ricerca");

// 2. Empty state text
assert.ok(MEMORY_SEARCH_EMPTY.length > 0, "empty-state text is defined");
assert.equal(MEMORY_SEARCH_EMPTY, "Nessun risultato trovato.");

// 3. Plain Italian owner names
assert.deepEqual(OWNER_NAMES, {
  project_knowledge: "Base di progetto",
  literature: "Letteratura",
  modeling: "Modelli",
  process_stack: "Processo",
  engineering: "Ingegneria",
  bluecad: "CAD",
  ai_threads: "Conversazioni",
  development: "Sviluppo",
  workspace_documents: "Documenti"
});
assert.equal(new Set(Object.values(OWNER_NAMES)).size, 9, "nine distinct owner names");
assert.equal(ownerLabel("project_knowledge"), "Base di progetto");
assert.equal(ownerLabel("literature"), "Letteratura");
assert.equal(ownerLabel("modeling"), "Modelli");
assert.equal(ownerLabel("process_stack"), "Processo");
assert.equal(ownerLabel("engineering"), "Ingegneria");
assert.equal(ownerLabel("bluecad"), "CAD");
assert.equal(ownerLabel("ai_threads"), "Conversazioni");
assert.equal(ownerLabel("development"), "Sviluppo");
assert.equal(ownerLabel("workspace_documents"), "Documenti");
assert.equal(ownerLabel("unseen_owner_tag"), "unseen_owner_tag", "unknown owner shown as stored");

// 4. "Cartella documenti" labels and refusal text
assert.equal(FOLDER_BOX_TITLE, "Cartella documenti");
assert.equal(FOLDER_UNREGISTERED, "Nessuna cartella registrata.");
assert.equal(
  FOLDER_INVALID_MESSAGE,
  "Cartella non valida: serve una cartella esistente, non un collegamento, fuori dai dati di Jarvis e dal codice."
);

// 5. Rendered markup assertions
const html = renderToStaticMarkup(React.createElement(MemorySearch, { workspaceId: "ws-test" }));
assert.ok(html.includes("Ricerca"), "rendered page contains title Ricerca");
assert.ok(html.includes(MEMORY_SEARCH_EMPTY), "rendered page contains empty-state text");
assert.ok(html.includes("Cartella documenti"), "rendered page contains Cartella documenti header");
assert.ok(html.includes("Nessuna cartella registrata."), "rendered page contains unregistered message");
assert.ok(html.includes("Percorso della cartella"), "rendered page contains Percorso della cartella input label");
assert.ok(html.includes("Registra cartella"), "rendered page contains Registra cartella control");

// 6. Route wiring in routes.ts and App.tsx
const routes = read("src/app/routes.ts");
assert.ok(routes.includes('"memory-search"'), "routes.ts declares memory-search RouteId");
assert.ok(routes.includes('path: "/memory/search"'), "routes.ts declares /memory/search path");
assert.ok(routes.includes('title: "Ricerca"'), "routes.ts declares Ricerca title");
assert.ok(routes.includes('label: "Ricerca"'), "routes.ts includes Ricerca in PEER_NAV_ITEMS.memory");

const app = read("src/App.tsx");
assert.ok(app.includes('case "memory-search":'), "App.tsx routes memory-search");
assert.ok(app.includes("<MemorySearch"), "App.tsx renders MemorySearch");

const header = read("src/components/fusion/FinalWorkspaceHeader.tsx");
assert.ok(header.includes('["Ricerca", "/memory/search", "search"]'), "FinalWorkspaceHeader includes Ricerca tab");

console.log("300 memory search frontend test: OK");
