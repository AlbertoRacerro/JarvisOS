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
const code = ts.transpileModule(read("src/components/coding/RelayMonitorPanel.tsx"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
}).outputText;
new Function("require", "module", "exports", code)(
  name => name === "../../api/coding" ? { CodingRequestError: class extends Error {} } : require(name), module, module.exports
);
const { RelayMonitorView } = module.exports;
const render = props => renderToStaticMarkup(React.createElement(RelayMonitorView, { items: [], error: null, connected: true, ...props }));

// Semantic header: the spec 205 state, reason, mission, task, turn and liveness.
const running = render({ snapshot: {
  state: "held", reason: "task_check_failed", owner: true,
  mission: { id: "m-20261022T125110Z-1fae56", task: "1/1", label: "ciclo 7 della roadmap autonoma", agent: "claude" },
  turn: { turn: "turn-001", status: "continue", next_step: "wait for CI on #875" },
  problems: ["hostops_down"], incidents: [],
} });
for (const fragment of ['data-state="held"', ">Held<", "task check failed", "owner process alive", "m-20261022T125110Z-1fae56",
  "task 1/1", "claude", "ciclo 7 della roadmap autonoma", "turn-001 · continue", "wait for CI on #875", "hostops down"]) {
  assert.ok(running.includes(fragment), `header is missing ${fragment}`);
}

// Timeline rendering and details disclosure on demand.
const at = { session: "20261022-125822-1260f3", turn: "turn-001" };
const timeline = render({ snapshot: { state: "running", owner: true, mission: { id: "m-1" } }, items: [
  { kind: "turn", mark: "──", text: "20261022-125822-1260f3/turn-001", ...at, offset: 0 },
  { kind: "message", mark: "●", text: "Reading the spec", ...at, offset: 10 },
  { kind: "tool", mark: "⏺", text: "Bash(git status)", ...at, offset: 20 },
  { kind: "output", mark: "✗", text: "boom one…", detail: "boom one\nboom two", failed: true, ...at, offset: 30 },
  { kind: "done", mark: "✓", text: "success", ...at, offset: 40 },
] });
assert.ok(timeline.includes('aria-label="Owner timeline"'));
assert.equal((timeline.match(/<li /g) ?? []).length, 5);
assert.ok(timeline.includes('data-kind="tool"') && timeline.includes("Bash(git status)"));
assert.ok(timeline.includes("Turn finished: success"));
assert.ok(timeline.includes('data-failed="true"'));
assert.equal((timeline.match(/<details>/g) ?? []).length, 1, "only shortened lines open details");
assert.ok(timeline.includes("<pre>boom one\nboom two</pre>"));
assert.ok(!/<input|<textarea|<button/.test(timeline), "the monitor has no input or control in this slice");

// Empty and unknown states are truthful, never an error page or a fake running state.
const loading = render({ snapshot: null });
assert.ok(loading.includes("Reading owner state…") && loading.includes('data-state="unknown"'));
const idle = render({ snapshot: { state: "idle", owner: true, roadmap: { enabled: true, cycle: 7, paused: null } } });
assert.ok(idle.includes("No mission is active · roadmap mode on") && idle.includes("No timeline events yet."));
const unknown = render({ snapshot: { state: "unknown", reason: "owner_state_missing" } });
assert.ok(unknown.includes("cannot be determined (owner state missing)") && unknown.includes("owner liveness unknown"));
assert.ok(!unknown.includes("No mission is active"));
const failed = render({ snapshot: null, error: "http_503", connected: false });
assert.ok(failed.includes("Owner state unavailable: http 503") && failed.includes("reconnecting"));

// Wiring: the panel is in Coding → Runtime and the client only reads the two read-only endpoints.
assert.ok(read("src/pages/CodingWorkbench.tsx").includes("<RelayMonitorPanel />"));
const api = read("src/api/coding.ts");
assert.ok(api.includes("/api/coding/relay-monitor?") && api.includes("/api/coding/relay-monitor/timeline?"));
const panel = read("src/components/coding/RelayMonitorPanel.tsx");
assert.ok(!/method:\s*"(POST|PUT|DELETE|PATCH)"/.test(panel) && !/#[a-f\d]{3,8}\b/i.test(read("src/pages/CodingWorkbench.css").split("Spec 206")[1]));
console.log("206 coding relay monitor: PASS");
