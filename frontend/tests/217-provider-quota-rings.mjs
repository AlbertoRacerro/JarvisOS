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

// Render the real panel module; only the API client is replaced (it reads import.meta.env).
const module = { exports: {} };
const code = ts.transpileModule(read("src/components/coding/ProviderQuotaPanel.tsx"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
}).outputText;
new Function("require", "module", "exports", code)(name => name === "../../api/coding" ? {} : require(name), module, module.exports);
const { ProviderQuotaView } = module.exports;
const now = new Date("2026-10-24T12:00:00Z");
const render = props => renderToStaticMarkup(React.createElement(ProviderQuotaView, { error: null, now, ...props }));
const win = (kind, used, extra = {}) => ({ id: kind, label: null, kind, used_percent: used, remaining_percent: 100 - used,
  resets_at: "2026-10-24T14:30:00Z", exhausted: false, stale: false, ...extra });

const data = { state: "ok", read_at: "2026-10-24T11:57:00Z", providers: [
  { provider: "claude", label: "Claude", source: "agent-relay limits", status: "ok", as_of: "2026-10-24T11:57:00Z",
    windows: [win("five_hour", 51), win("weekly", 12), win("frontier", 64, { id: "seven_day_opus", label: null })] },
  { provider: "codex", label: "Codex (ChatGPT)", source: "agent-relay limits", status: "ok",
    windows: [win("five_hour", 100, { exhausted: true }), win("weekly", 40, { stale: true })] },
  { provider: "antigravity", label: "Antigravity", source: "agy /quota", status: "unavailable", reason: "agy_quota_unreadable", windows: [] },
  { provider: "copilot", label: "GitHub Copilot", source: "gh copilot_internal/user", status: "ok", as_of: "2026-10-24T11:57:00Z", last_error: "timeout", windows: [win("pool", 20, { id: "premium_interactions", label: "Monthly premium requests" })] },
] };
const html = render({ data });

// Four provider columns, 5-hour and weekly rows, and a third row only for the pool that exists.
for (const fragment of ["Claude", "Codex (ChatGPT)", "Antigravity", "GitHub Copilot", "refresh failed", 'aria-label="5-hour"', 'aria-label="Weekly"', 'aria-label="Frontier model"', "read 3 min ago"]) {
  assert.ok(html.includes(fragment), `missing ${fragment}`);
}
assert.equal((html.match(/role="group"/g) ?? []).length, 6, "four rows plus the two AGY Claude source groups");
assert.ok(html.includes('<span class="coding-quota-model">Opus</span>'), "the model name is Opus, not the id prefix");

// The visible percentage is the same metric as the ring: remaining.
assert.ok(html.includes(">49%</text>") && html.includes('aria-label="49% left"'));
assert.ok(html.includes(">88%</text>") && html.includes(">36%</text>"));
assert.ok(html.includes("↻ 2h 30m") && html.includes('title="Resets in 2h 30m"'));
const later = render({ data: { ...data, providers: [{ ...data.providers[0], windows: [win("weekly", 10, { resets_at: "2026-10-25T21:59:00Z" })] }] } });
assert.ok(later.includes("↻ 1d 9h"), "a reset beyond a day is shown in days");
assert.equal((html.match(/class="coding-quota-value"/g) ?? []).length, 5, "an empty (0 %) ring draws no arc");

// Exhausted, stale and missing are distinct and never shown as 0 %.
assert.ok(html.includes('data-state="exhausted"') && html.includes(">Exhausted<"));
assert.ok(html.includes('data-state="stale"') && html.includes("out of date"));
assert.ok(html.includes("agy /quota unreadable"));
const agy = render({ data: { ...data, providers: [{ provider: "antigravity", label: "Antigravity", source: "agy /quota", status: "ok",
  windows: [win("five_hour", 0), win("weekly", 51), win("frontier", 51, { id: "argon-weekly", label: "Argon · weekly" })] }] } });
assert.ok(agy.includes('aria-label="Frontier model"') && agy.includes('<span class="coding-quota-model">Argon</span>'));
assert.ok(agy.includes(">100%</text>") && agy.includes(">49%</text>") && !agy.includes("Claude &amp; GPT"));
assert.equal((html.match(/>N\/A</g) ?? []).length, 6, "missing common windows and unavailable AGY show N/A");
assert.equal((html.match(/>0%</g) ?? []).length, 1, "only the really exhausted window shows 0 %; missing values never do");

const unauthenticated = render({ data: { ...data, providers: [{ provider: "claude", label: "Claude", source: "agent-relay limits",
  status: "unauthenticated", reason: "not_authenticated", windows: [] }] } });
assert.ok(unauthenticated.includes("Not signed in"));

// The two AGY windows are visually in Claude's column but keep AGY source/freshness,
// even when the separate Claude Code subscription is not signed in.
const shared = { ...data.providers[2], status: "ok", as_of: "2026-10-24T11:58:00Z", windows: [
  win("frontier", 51, { id: "argon-weekly" }),
  win("pool", 27, { id: "3p-5h", label: "Claude & GPT · 5-hour" }),
  win("pool", 100, { id: "3p-weekly", label: "Claude & GPT · weekly", exhausted: true }),
] };
const withShared = render({ data: { ...data, providers: [
  { ...data.providers[0], status: "unauthenticated", reason: "not_authenticated", windows: [] },
  data.providers[1], shared,
] } });
assert.equal((withShared.match(/>Claude via AGY</g) ?? []).length, 2);
assert.ok(withShared.includes('aria-label="Claude via AGY · 5-hour"') && withShared.includes('aria-label="Claude via AGY · Weekly"'));
assert.ok(withShared.includes('title="Antigravity shared Claude &amp; GPT pool · observed at 2026-10-24T11:58:00Z"'));
assert.ok(withShared.includes('aria-label="73% left"') && withShared.includes('aria-label="0% left, exhausted"'));
assert.equal((withShared.match(/class="coding-quota-row"/g) ?? []).length, 3, "shared windows do not become extra Antigravity pool rows");
const frontierRow = withShared.slice(withShared.indexOf('aria-label="Frontier model"'));
assert.ok(frontierRow.indexOf('data-provider="claude"') < frontierRow.indexOf('aria-label="Claude via AGY · Weekly"'));
assert.ok(frontierRow.indexOf('aria-label="Claude via AGY · Weekly"') < frontierRow.indexOf('data-provider="codex"'));
assert.ok(frontierRow.includes('<span class="coding-quota-model">Argon</span>'));

const failed = render({ data: null, error: "server unreachable" });
assert.ok(failed.includes("Provider usage unavailable: server unreachable") && !failed.includes("<svg"));
assert.ok(!/<input|<textarea|<button/.test(html), "the usage view is read-only");
const refresh = render({ data, onRefresh() {} });
assert.ok(refresh.includes('type="button"') && refresh.includes("Refresh usage"));
assert.ok(refresh.includes('title="Usage observed at 2026-10-24T11:57:00Z"'), "source observation time is separate from the server read");
const loading = render({ data, onRefresh() {}, refreshing: true });
assert.ok(loading.includes('disabled=""') && loading.includes('aria-busy="true"') && loading.includes("Refreshing…"));

// Placement: beside the Relay owner panel in Coding → Runtime, stacking on narrow screens.
const page = read("src/pages/CodingWorkbench.tsx");
assert.ok(page.includes('<div className="coding-relay-row"><RelayMonitorPanel /><ProviderQuotaPanel /></div>'));
const css = read("src/pages/CodingWorkbench.css");
assert.ok(/@media \(max-width: 900px\) \{ \.coding-relay-row \{ grid-template-columns: minmax\(0, 1fr\); \} \}/.test(css));
assert.ok(css.includes("prefers-reduced-motion: reduce) { .coding-quota-value"));
console.log("217 provider quota rings: ok");
