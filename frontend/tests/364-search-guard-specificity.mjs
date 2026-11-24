import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const apiSource = fs.readFileSync(path.join(root, "src/api/settings.ts"), "utf8");
const pageSource = fs.readFileSync(path.join(root, "src/pages/Settings.tsx"), "utf8");

// Criterion 3: Operator visibility for blocked queries in Settings/AI
assert.ok(pageSource.includes("Filtro ricerche"), "Settings page must render 'Filtro ricerche' header");
assert.ok(pageSource.includes("Ricerche bloccate"), "Settings page must render 'Ricerche bloccate' count label");
assert.ok(pageSource.includes("data-search-guard-panel"), "Settings page must include data-search-guard-panel attribute");
assert.ok(pageSource.includes("loadSearchGuardBlockedSummary"), "Settings page must call loadSearchGuardBlockedSummary");
assert.ok(pageSource.includes("entry.reason_it"), "Settings page must render plain Italian reason");
assert.ok(pageSource.includes("entry.sha256_prefix"), "Settings page must display sha256_prefix");
assert.ok(pageSource.includes("entry.reason_code"), "Settings page must display rule category/reason_code");

// Criterion 3: Privacy & leak guard - never expose raw query or secrets in the UI
assert.ok(!pageSource.includes("entry.query"), "Settings page must NEVER expose raw query text");
assert.ok(!pageSource.includes("entry.raw_query"), "Settings page must NEVER expose raw query text");
assert.ok(!pageSource.includes("entry.query_text"), "Settings page must NEVER expose raw query text");
assert.ok(!pageSource.includes("entry.path"), "Settings page must NEVER expose internal log file paths");

// API layer checks
assert.ok(apiSource.includes("SearchGuardBlockedSummary"), "api/settings.ts must export SearchGuardBlockedSummary type");
assert.ok(apiSource.includes("SearchGuardBlockedEntry"), "api/settings.ts must export SearchGuardBlockedEntry type");
assert.ok(apiSource.includes("loadSearchGuardBlockedSummary"), "api/settings.ts must export loadSearchGuardBlockedSummary function");
assert.ok(apiSource.includes('"/ai/search-guard/blocked"'), "api/settings.ts must target /ai/search-guard/blocked");

// Transpile API for Node execution test
function compileApiForNode(source) {
  const withoutClientImport = source.replace(
    /import \{[\s\S]*?\} from "\.\/client";/,
    'const API_BASE_URL = "";'
  );
  return ts.transpileModule(withoutClientImport, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 }
  }).outputText;
}

async function importModuleSource(source) {
  const encoded = Buffer.from(source).toString("base64");
  return import(`data:text/javascript;base64,${encoded}#${Date.now()}-${Math.random()}`);
}

const api = await importModuleSource(compileApiForNode(apiSource));
const calls = [];
globalThis.fetch = async (url, init = {}) => {
  calls.push({ url: String(url), init });
  return new Response(JSON.stringify({
    total_blocked: 2,
    recent_blocked: [
      {
        ts: "2026-11-24T12:00:00Z",
        sha256_prefix: "abcdef0123456789",
        reason_code: "exact_combination",
        reason_it: "Combinazione protetta di progetto rilevata",
        rule_index: 0
      }
    ]
  }), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  });
};

const result = await api.loadSearchGuardBlockedSummary();
assert.equal(calls.at(-1).url, "/ai/search-guard/blocked");
assert.equal(result.total_blocked, 2);
assert.equal(result.recent_blocked.length, 1);
assert.equal(result.recent_blocked[0].reason_code, "exact_combination");
assert.equal(result.recent_blocked[0].reason_it, "Combinazione protetta di progetto rilevata");

console.log("364 search guard specificity frontend conformance: PASS");
