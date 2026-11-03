// Spec 161 source-level and pure-function checks; browser behaviour is proven separately.
import assert from "node:assert/strict";
import fs from "node:fs";
import { modelDisplayName } from "../src/components/ai/modelDisplayName.ts";
import { cloudFailureMessage, formatMoney, sortCreatedChronologically } from "../src/components/ai/sidecarPresentation.ts";

const read = (path) => fs.readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const sidecar = read("src/components/ai/useJarvisSidecar.tsx");
const sidecarPresentation = read("src/components/ai/workspaceActionPresentation.ts");
const shellSidecar = read("src/components/shell/ContextualSidecar.tsx");
const layout = read("src/components/Layout.tsx");
const api = read("src/api/threads.ts");
const shellCss = read("src/styles/final-fusion-shell-overrides.css");
const contextMenu = read("src/components/ui/ContextMenu.tsx");

// Human model names: concise, derived from the recorded id, never invented.
for (const [id, name] of [
  ["gemma-4-12b-it-qat-q4_0", "Gemma 4 12B"],
  ["/models/gemma-4-12b-it-qat-q4_0.gguf", "Gemma 4 12B"],
  ["Qwen3.5-4B-Q6_K.gguf", "Qwen 3.5 4B"],
  ["claude-opus-5-5", "Claude Opus 5.5"],
  ["claude-sonnet-5-5", "Claude Sonnet 5.5"],
  ["gpt-6-luna", "GPT-6 Luna"],
  ["gpt-6.1-sol", "GPT-6.1 Sol"],
  ["deepseek-v4-flash-0731", "DeepSeek V4 Flash"]
]) assert.equal(modelDisplayName(id), name, id);
assert.equal(modelDisplayName(null, "codex"), "Codex");
assert.equal(modelDisplayName(null), "Unknown model");

// API cost metadata uses the record's USD/EUR currencies and human-readable precision.
assert.equal(formatMoney("0.00065225160000000001", "USD"), "less than $0.01");
assert.equal(formatMoney("0E+4", "EUR"), "€0.00");
assert.equal(formatMoney("1.234567", "USD"), "$1.235");
assert.equal(formatMoney(null, "USD"), "unknown");
assert.equal(cloudFailureMessage("provider_gate_blocked"), "Not sent — paid cloud AI is off or over budget. Nothing left your computer.");
assert.doesNotMatch(cloudFailureMessage("provider_gate_blocked"), /provider_gate_blocked/);

const createdOrder = sortCreatedChronologically([
  { id: "api", kind: "cloud", created_at: "2026-10-01T10:02:00Z" },
  { id: "relay", kind: "relay", created_at: "2026-10-01T10:01:00Z" }
]);
assert.deepEqual(createdOrder.map(({ kind }) => kind), ["relay", "cloud"]);

// Chat-first hierarchy: no duplicate headings or legacy form labels.
assert.doesNotMatch(shellSidecar, /Jarvis &amp; Properties|role="tablist"/);
for (const legacy of ["Jarvis agent (Hermes)\" : responderLabel", "Send to Jarvis\"", "<span>Conversation</span>", "<span>Responder</span>", "<span>Send to</span>", "Ask Jarvis about this workspace."]) {
  assert.ok(!sidecar.includes(legacy), `legacy Sidecar chrome still present: ${legacy}`);
}
assert.match(sidecar, /jarvis-turn--user/);
assert.match(sidecar, /jarvis-sidecar__history/);
assert.ok(sidecar.indexOf("jarvis-sidecar__composer") < sidecar.indexOf("jarvis-sidecar__history"), "history must sit below the composer");
assert.match(sidecar, /SidecarChromeActions/);
assert.match(sidecar, /reason_code === "LLAMACPP_AUTH_REQUIRED"/, "a protected llama-server is not presented as a usable responder");
assert.match(sidecar, /function ResponderMenu[\s\S]*useContext\(SidecarChrome\)[\s\S]*id: "show-properties", label: "Show properties"/);
assert.match(sidecar, /jarvis-status[^>]*title=\{state\.detail\} aria-label=\{state\.label\} aria-description=\{state\.detail\}/, "full readiness detail remains available when its visible label truncates");
assert.match(sidecar, /<button[^>]*jarvis-icon-button--new-conversation[^>]*>[\s\S]*aria-label="New conversation"[\s\S]*<span>New conversation<\/span>/, "New conversation keeps an accessible label when its text collapses");
const sidecarCss = read("src/components/ai/JarvisSidecar.css");
assert.match(sidecarCss, /\.jarvis-sidecar__header\s*\{[^}]*min-width:\s*0;[^}]*\}/);
assert.match(sidecarCss, /\.jarvis-status\s*>\s*span\s*\{[^}]*text-overflow:\s*ellipsis;[^}]*\}/);
assert.match(sidecarCss, /@container jarvis-header \(max-width: \d+px\)[\s\S]*?\.jarvis-icon-button--new-conversation span\s*\{\s*display:\s*none;/);
assert.doesNotMatch(sidecarCss, /\.jarvis-sidecar__header\s*\{[^}]*flex-wrap:\s*wrap/);
assert.doesNotMatch(sidecar, /aria-pressed=\{chrome\.propertiesOpen\}/, "Properties stays in the compact secondary controls, outside the required header row");
assert.match(shellSidecar, /closest\('\[role="menu"\]'\)/, "Escape in a menu must not close the whole Sidecar");
assert.match(contextMenu, /if \(!position\) return;[\s\S]*querySelector<HTMLButtonElement>\("button:not\(:disabled\)"\)[\s\S]*\}, \[position\]\)/, "menus move keyboard focus after their final position is measured");
assert.match(shellSidecar, /Back to Jarvis/);

// Relay is the default escalation; the API path stays an explicit choice; no silent fallback.
assert.match(sidecar, /onClick=\{onRelay\}/);
assert.match(sidecar, /"Escalate with Relay"/);
assert.match(sidecar, /"Escalate with API key…"/);
assert.match(sidecar, /Nothing was sent to a paid API\./);
assert.match(sidecar, /subscription, no API charge/);
assert.match(api, /relay-escalation-draft/);
assert.match(api, /relay-escalate/);
assert.match(sidecar, /relayEscalationInFlight\.current/);
assert.match(sidecar, /relayFailureMessage\(run\.reason_code, run\.result_text\)/, "Relay failure notices use the recorded result");
assert.match(sidecarPresentation, /relay_gateway_disabled:[\s\S]*relay_agent_login_missing:[\s\S]*relay_run_failed:/, "Relay availability and run failures use readable messages");
assert.match(sidecar, /cloudFailureMessage\(item\.reason_code\)/, "blocked API turns map reason codes to plain language");
assert.match(sidecar, /item\.reason_code \? ` · \$\{item\.reason_code\}`/, "raw API failure code is kept in Info");
assert.match(sidecar, /formatMoney\(item\.projected_cost_usd, "USD"\)[\s\S]*formatMoney\(item\.accounted_cost_eur, "EUR"\)/, "projected and actual amounts keep their source currencies");
assert.match(sidecar, /sortCreatedChronologically\(\[[\s\S]*cloudResults[\s\S]*relayRuns[\s\S]*\]\)/, "Relay and API turns share one chronological ordering");
assert.match(sidecar, /<textarea id="jarvis-prompt" aria-label=\{/, "composer keeps an accessible label without a visible Message label");
assert.doesNotMatch(sidecar, /<label htmlFor="jarvis-prompt"/, "composer does not render the redundant Message label");

// Closing a shell region leaves an on-screen reopen affordance.
// BUBU-LAUNCHER-1 replaces the width-consuming tab with the persistent launcher.
assert.match(layout, /<BubuLauncher buttonRef=\{sidecarToggleRef\}/);
assert.doesNotMatch(layout, /shell-reopen--sidecar/);
assert.match(layout, /shell-reopen--navigator/);
assert.match(shellCss, /--jarvis-width: clamp\(300px, 24vw, 420px\)/);
console.log("161 Sidecar chat and Relay escalation checks passed");

// Routes that render Jarvis in-page must not also offer a shell Sidecar (and reopen tab).
const appSrc = read("src/App.tsx");
assert.match(appSrc, /IN_PAGE_JARVIS_ROUTES = new Set\(\[[^\]]*"memory-models"[^\]]*"development-roadmap-timeline"[^\]]*\]\)/);
assert.match(appSrc, /IN_PAGE_JARVIS_ROUTES\.has\(route\.id\)[^;]*\? undefined : jarvisSidecar/);
