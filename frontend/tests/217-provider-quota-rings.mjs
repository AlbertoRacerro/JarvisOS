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

// Four provider columns, 4 semantic rows in plain Italian
for (const fragment of ["Claude", "OpenAI / Codex", "Google", "GitHub", "aggiornamento fallito", 'aria-label="5 ore"', 'aria-label="Settimanale"', 'aria-label="Modello di frontiera"', 'aria-label="Extra usage"', "letto 3 min fa"]) {
  assert.ok(html.includes(fragment), `missing ${fragment}`);
}
assert.equal((html.match(/class="coding-quota-row"/g) ?? []).length, 4, "four fixed semantic rows");
assert.ok(html.includes('<span class="coding-quota-model">Opus</span>'), "the model name is Opus, not the id prefix");

// The visible percentage is remaining
assert.ok(html.includes(">49%</text>") && html.includes('aria-label="49% rimasto"'));
assert.ok(html.includes(">88%</text>") && html.includes(">36%</text>"));
assert.ok(html.includes("↻ 2h 30m") && html.includes('title="Si azzera tra 2h 30m"'));
const later = render({ data: { ...data, providers: [{ ...data.providers[0], windows: [win("weekly", 10, { resets_at: "2026-10-25T21:59:00Z" })] }] } });
assert.ok(later.includes("↻ 1d 9h") || later.includes("↻ 1g 9h"), "a reset beyond a day is shown in days");
assert.equal((html.match(/class="coding-quota-value"/g) ?? []).length, 5, "an empty (0 %) ring draws no arc");

// Exhausted, stale and missing states in Italian
assert.ok(html.includes('data-state="exhausted"') && html.includes(">Esaurito<"));
assert.ok(html.includes('data-state="stale"') && html.includes("non aggiornato"));
assert.ok(html.includes("Quota agy non leggibile"));
const agy = render({ data: { ...data, providers: [{ provider: "antigravity", label: "Antigravity", source: "agy /quota", status: "ok",
  windows: [win("five_hour", 0), win("weekly", 51), win("frontier", 51, { id: "argon-weekly", label: "Argon · weekly" })] }] } });
assert.ok(agy.includes('aria-label="Modello di frontiera"') && agy.includes('<span class="coding-quota-model">Argon</span>'));
assert.ok(agy.includes(">100%</text>") && agy.includes(">49%</text>") && !agy.includes("Claude &amp; GPT"));
assert.ok(html.includes("Non applicabile") && html.includes("Non riportato"));

const unauthenticated = render({ data: { ...data, providers: [{ provider: "claude", label: "Claude", source: "agent-relay limits",
  status: "unauthenticated", reason: "not_authenticated", windows: [] }] } });
assert.ok(unauthenticated.includes("Accesso non effettuato"));

const expired = render({ data: { ...data, providers: [{ provider: "claude", label: "Claude", source: "claude live", status: "unavailable", reason: "session_expired", windows: [] }] } });
assert.ok(expired.includes("Login scaduto"));
const missingCreds = render({ data: { ...data, providers: [{ provider: "claude", label: "Claude", source: "claude live", status: "unavailable", reason: "credentials_missing", windows: [] }] } });
assert.ok(missingCreds.includes("Credenziali assenti"));
const twoHoursAgo = render({ data: { ...data, read_at: "2026-10-24T10:00:00Z" } });
assert.ok(twoHoursAgo.includes("letto 2 h fa"));
const threeDaysAgo = render({ data: { ...data, read_at: "2026-10-21T12:00:00Z" } });
assert.ok(threeDaysAgo.includes("letto 3 giorni fa"));
const justNow = render({ data: { ...data, read_at: "2026-10-24T12:00:00Z" } });
assert.ok(justNow.includes("letto ora"));

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
assert.ok(withShared.includes('aria-label="Claude via AGY · 5 ore"') && withShared.includes('aria-label="Claude via AGY · Settimanale"'));
assert.ok(withShared.includes('title="Antigravity shared Claude &amp; GPT pool · observed at 2026-10-24T11:58:00Z"'));
assert.ok(withShared.includes('aria-label="73% rimasto"') && withShared.includes('aria-label="0% rimasto, esaurito"'));
assert.equal((withShared.match(/class="coding-quota-row"/g) ?? []).length, 4, "four fixed semantic rows");
const frontierRow = withShared.slice(withShared.indexOf('aria-label="Modello di frontiera"'));
assert.ok(frontierRow.indexOf('data-provider="claude"') < frontierRow.indexOf('aria-label="Claude via AGY · Settimanale"'));
assert.ok(frontierRow.indexOf('aria-label="Claude via AGY · Settimanale"') < frontierRow.indexOf('data-provider="codex"'));
assert.ok(frontierRow.includes('<span class="coding-quota-model">Argon</span>'));

const failed = render({ data: null, error: "server unreachable" });
assert.ok(failed.includes("Utilizzo provider non disponibile: server unreachable") && !failed.includes("<svg"));
assert.ok(!/<input|<textarea|<button/.test(html), "the usage view is read-only");
const refresh = render({ data, onRefresh() {} });
assert.ok(refresh.includes('type="button"') && refresh.includes("Aggiorna"));
assert.ok(refresh.includes('title="Rilevato il 2026-10-24T11:57:00Z"'), "source observation time is separate from the server read");
const loading = render({ data, onRefresh() {}, refreshing: true });
assert.ok(loading.includes('disabled=""') && loading.includes('aria-busy="true"') && loading.includes("Aggiornamento…"));

// Section 2: Modelli gratis ora (empty and populated)
assert.ok(html.includes("<h4>Modelli gratis ora</h4>"));
assert.ok(html.includes("Nessun modello gratis attivo"));

const withFreeAndPrepaid = render({
  data: {
    ...data,
    free_models: [
      { id: "free-1", label: "Hermes 3", source: "Nous", model_id: "nous/hermes-3", intelligence_index: 43,
        status: "active", limit_kind: "ring", remaining_percent: 75.0, exhausted: false },
      { id: "free-2", label: "Zen Free", source: "OpenCode Zen", model_id: "zen:free", intelligence_index: 38,
        status: "active", limit_kind: "live_dot", remaining_percent: null, exhausted: false },
      { id: "free-3", label: "Exhausted Model", source: "OpenRouter", model_id: "deepseek:free", intelligence_index: 30,
        status: "exhausted", limit_kind: "ring", remaining_percent: 0.0, exhausted: true },
    ],
    prepaid_balances: [
      { provider: "deepseek", label: "DeepSeek Prepaid", mode: "read_only", balance_usd: 12.50,
        spent_eur: null, cap_eur: null, status: "ok", reason: null },
      { provider: "zai", label: "Z.ai", mode: "read_only", balance_usd: null,
        spent_eur: 0.0, cap_eur: 5.0, status: "ok", reason: null },
    ],
  },
});

assert.ok(withFreeAndPrepaid.includes("Nous") && withFreeAndPrepaid.includes("AA 43"));
assert.ok(withFreeAndPrepaid.includes("Hermes 3") && withFreeAndPrepaid.includes(">75%</text>"));
assert.ok(withFreeAndPrepaid.includes("OpenCode Zen") && withFreeAndPrepaid.includes("coding-free-live-dot"));
assert.ok(withFreeAndPrepaid.includes("Exhausted Model") && withFreeAndPrepaid.includes("coding-free-badge exhausted"));

// Section 3: Saldi prepagati (empty and populated)
assert.ok(html.includes("<h4>Saldi prepagati (sola lettura)</h4>"));
assert.ok(html.includes("Sola lettura — modelli a pagamento disattivati"));
assert.ok(html.includes("Nessun saldo prepagato configurato"));

assert.ok(withFreeAndPrepaid.includes("DeepSeek Prepaid") && withFreeAndPrepaid.includes("$12.50"));
assert.ok(withFreeAndPrepaid.includes("Z.ai") && withFreeAndPrepaid.includes("€0.00 / €5.00"));
assert.ok(withFreeAndPrepaid.includes("sola lettura"));

// Placement: beside the Relay owner panel in Coding → Runtime, stacking on narrow screens.
const page = read("src/pages/CodingWorkbench.tsx");
assert.ok(page.includes('<div className="coding-relay-row"><RelayMonitorPanel /><ProviderQuotaPanel /></div>'));
const css = read("src/pages/CodingWorkbench.css");
assert.ok(/@media \(max-width: 900px\) \{ \.coding-relay-row \{ grid-template-columns: minmax\(0, 1fr\); \} \}/.test(css));
assert.ok(css.includes("prefers-reduced-motion: reduce) { .coding-quota-value"));
console.log("217 provider quota rings: ok");
