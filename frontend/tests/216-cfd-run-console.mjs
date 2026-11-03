// Spec 216 contract 4: CFD run console state folds cursor pages and never words stopped as finished.
import assert from "node:assert/strict";
import fs from "node:fs";
import { applyEvents, canRetry, canStop, emptyConsole, isActiveState, MAX_CONSOLE_LINES, runStateText } from "../src/pages/cfdRunConsole.ts";

const log = (seq, line) => ({ seq, kind: "log", emitted_at: "t", stream: "stdout", line });
const res = (seq, iteration, u) => ({ seq, kind: "residual", emitted_at: "t", iteration, initial: { Ux: u, p: u / 2 }, final: { Ux: u / 10, p: null }, max_normalized_residual: u });
const page = (run_id, events) => ({ run_id, state: "running", live: true, events, cursor: events.at(-1)?.seq ?? 0 });

let s = emptyConsole("a");
s = applyEvents(s, page("a", [log(1, "mesh"), res(2, 1, 0.5)]));
s = applyEvents(s, page("a", [res(3, 2, 0.1), log(4, "end")]));
assert.equal(s.cursor, 4);
assert.deepEqual(s.lines.map((l) => l.line), ["mesh", "end"]);
assert.equal(s.residuals.Ux.iteration, 2);
assert.equal(s.residuals.Ux.initial, 0.1);
assert.equal(s.residuals.p.final, null);

// stale replay, wrong run: ignored
assert.equal(applyEvents(s, page("a", [log(2, "old")])), s);
assert.equal(applyEvents(s, page("b", [log(9, "x")])), s);

// bounded line buffer
let big = emptyConsole("a");
big = applyEvents(big, page("a", Array.from({ length: MAX_CONSOLE_LINES + 10 }, (_, i) => log(i + 1, `l${i}`))));
assert.equal(big.lines.length, MAX_CONSOLE_LINES);
assert.equal(big.droppedLines, 10);

for (const state of ["queued", "running", "stopping"]) assert.ok(isActiveState(state));
for (const state of ["stopped", "finished", "failed", "interrupted"]) { assert.ok(!isActiveState(state)); assert.ok(canRetry(state)); }
assert.ok(canStop("running") && !canStop("stopping") && !canStop("stopped") && !canRetry("stopping"));
assert.ok(!/finished/i.test(runStateText({ state: "stopped" })));
assert.ok(!/finished/i.test(runStateText({ state: "stopping" })));
assert.match(runStateText({ state: "failed", message: "boom" }), /boom/);
assert.match(runStateText({ state: "refused", mesh_reason: "checkMesh failed" }), /Refused — checkMesh failed/);
assert.ok(canRetry("refused") && !isActiveState("refused"));

const panel = fs.readFileSync(new URL("../src/pages/CfdRunsPanel.tsx", import.meta.url), "utf8");
assert.match(panel, /qualifies_publication/);
console.log("216 cfd run console ok");
