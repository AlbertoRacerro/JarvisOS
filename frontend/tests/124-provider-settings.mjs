import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const apiSource = fs.readFileSync(path.join(root, "src/api/settings.ts"), "utf8");
const pageSource = fs.readFileSync(path.join(root, "src/pages/Settings.tsx"), "utf8");
const semanticsSource = fs.readFileSync(path.join(root, "src/operatorSemantics.ts"), "utf8");
const settingsCss = fs.readFileSync(path.join(root, "src/styles/final-settings.css"), "utf8");
const shellCss = fs.readFileSync(path.join(root, "src/styles/final-fusion-shell-overrides.css"), "utf8");

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
  return new Response(JSON.stringify({ providers: [] }), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  });
};

await api.loadProviderSettings();
assert.equal(calls.at(-1).url, "/ai/provider-settings");
assert.equal(calls.at(-1).init.method, undefined);

const sentinel = "sk-live-DO-NOT-LEAK-124";
await api.replaceScalewayCredential(sentinel);
assert.equal(calls.at(-1).url, "/secrets/scaleway/api-key");
assert.equal(calls.at(-1).init.method, "POST");
assert.deepEqual(JSON.parse(calls.at(-1).init.body), { api_key: sentinel });

await api.removeScalewayCredential();
assert.equal(calls.at(-1).url, "/secrets/scaleway/api-key");
assert.equal(calls.at(-1).init.method, "DELETE");
assert.equal(calls.some(({ url }) => url.includes("provider-smoke")), false);

globalThis.fetch = async () => new Response(JSON.stringify({
  detail: { code: "safe_code", message: "Safe public message" },
  private_debug: sentinel
}), {
  status: 409,
  headers: { "Content-Type": "application/json" }
});
await assert.rejects(
  () => api.loadProviderSettings(),
  (error) => {
    assert.equal(error.name, "SettingsApiError");
    assert.equal(error.code, "safe_code");
    assert.equal(error.message, "Safe public message");
    assert.equal(String(error).includes(sentinel), false);
    return true;
  }
);

function extractedGuard(pattern, variables) {
  const match = pageSource.match(pattern);
  assert.ok(match, `expected guard ${pattern}`);
  return new Function(...variables, `return (${match[1]});`);
}

const staleCanonical = extractedGuard(
  /if \((!mounted\.current \|\| owner !== generation\.current)\) return false;/,
  ["mounted", "owner", "generation"]
);
assert.equal(staleCanonical({ current: true }, 3, { current: 3 }), false);
assert.equal(staleCanonical({ current: true }, 2, { current: 3 }), true);
assert.equal(staleCanonical({ current: false }, 3, { current: 3 }), true);

const replaceBlocked = extractedGuard(
  /if \((credentialBusy \|\| uncertain \|\| !apiKey\.trim\(\) \|\| !scalewayCapability\?\.replace_persisted)\) return;/,
  ["credentialBusy", "uncertain", "apiKey", "scalewayCapability"]
);
assert.equal(replaceBlocked(false, false, "key", { replace_persisted: true }), false);
assert.equal(replaceBlocked(false, true, "key", { replace_persisted: true }), true);
assert.equal(replaceBlocked(false, false, "key", { replace_persisted: false }), true);
assert.equal(replaceBlocked(false, false, "   ", { replace_persisted: true }), true);

const deleteBlocked = extractedGuard(
  /if \((credentialBusy \|\| uncertain \|\| !scalewayCapability\?\.delete_persisted)\) return;/,
  ["credentialBusy", "uncertain", "scalewayCapability"]
);
assert.equal(deleteBlocked(false, false, { delete_persisted: true }), false);
assert.equal(deleteBlocked(true, false, { delete_persisted: true }), true);
assert.equal(deleteBlocked(false, false, { delete_persisted: false }), true);

const summaryMatch = pageSource.match(/function credentialSummary\([\s\S]*?\n}\n\nfunction Settings/);
assert.ok(summaryMatch, "credentialSummary must remain executable in the Settings projection");
const summarySource = `${semanticsSource}\n${summaryMatch[0].replace(/\n\nfunction Settings$/, "\nexport { credentialSummary };\n")}`;
const summaryModule = await importModuleSource(ts.transpileModule(summarySource, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 }
}).outputText);
const notRequired = summaryModule.credentialSummary({
  credential: { effective_source: "not_required", persisted_state: "not_supported", key_present: false }
});
assert.equal(notRequired, "No credential required · Stored credentials not supported.");
const persisted = summaryModule.credentialSummary({
  credential: { effective_source: "secure_persisted", persisted_state: "usable", key_present: true }
});
assert.equal(persisted, "Securely stored credential active · Stored credential ready.");
const invalid = summaryModule.credentialSummary({
  credential: { effective_source: "invalid", persisted_state: "usable", key_present: false }
});
assert.match(invalid, /Environment credential invalid.*Stored credential ready/);
const environmentCorrupted = summaryModule.credentialSummary({
  credential: { effective_source: "environment", persisted_state: "corrupted", key_present: true }
});
assert.match(environmentCorrupted, /Environment credential active.*Stored credential damaged/);

const invalidatorMatch = pageSource.match(/const invalidateCanonicalSnapshot = useCallback\(\(\) => \{([\s\S]*?)\n  \}, \[\]\);/);
assert.ok(invalidatorMatch, "failed canonical snapshots must have one complete invalidation owner");
for (const setter of ["setSettings", "setStatus", "setProviders", "setSecret", "setSystem", "setDraft"]) {
  assert.match(invalidatorMatch[1], new RegExp(`${setter}\\(null\\)`), `${setter} must be invalidated`);
}
const loadFailureMatch = pageSource.match(/catch \(caught\) \{([\s\S]*?)\n      return false;/);
assert.ok(loadFailureMatch, "loadCanonical failure path must remain inspectable");
assert.match(loadFailureMatch[1], /mounted\.current && owner === generation\.current/);
assert.match(loadFailureMatch[1], /invalidateCanonicalSnapshot\(\)/);

assert.match(apiSource, /external_calls_allowed:\s*boolean/);
assert.match(apiSource, /blocking_reason\?:\s*string\s*\|\s*null/);
assert.match(pageSource, /data-provider-egress-state=/);
assert.match(pageSource, /provider\.external_calls_allowed/);
assert.match(pageSource, /provider\.blocking_reason/);
assert.match(pageSource, /setProviders\(null\)/);
assert.match(pageSource, /Scaleway token usage/);
assert.match(settingsCss, /\.final-settings--ai \.settings-grid > \.settings-card:nth-child\(5\)/);
assert.match(settingsCss, /\.final-settings--system \.settings-grid > \.settings-card:nth-child\(6\)/);
assert.match(shellCss, /\.application-shell--final \.shell-main \{[\s\S]*?overflow:\s*auto;/);
assert.doesNotMatch(apiSource, /localStorage[^\n]*(api|key|secret)/i);
assert.doesNotMatch(pageSource, /DEEPSEEK_API_KEY|GLM_API_KEY|KIMI_API_KEY|SCALEWAY_API_KEY/);

console.log("124 provider settings behavioral frontend gate passed");
