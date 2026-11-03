import assert from "node:assert/strict";
import fs from "node:fs";
import { actionStatePresentation, buildSurfaceRef, isToolCallShaped, relayFailureMessage } from "../src/components/ai/workspaceActionPresentation.ts";

const process = buildSurfaceRef("design-process", null, {
  draft_id: "draft-1",
  process_selection: [{ kind: "stream", id: "stream-1", tag: "feed" }]
});
assert.deepEqual(process, {
  route_id: "design-process", draft_id: "draft-1",
  process_selection: [{ kind: "stream", id: "stream-1", tag: "feed" }]
});

const bluecadPart = {
  kind: "bluecad-part", workspaceId: "ws", candidateId: "candidate-1", artifactId: "a",
  viewerSessionId: "v", ephemeralObjectId: "mesh", meshKey: "m", semanticKey: "s", partId: "part-1"
};
assert.deepEqual(buildSurfaceRef("design-bluecad", bluecadPart, { draft_id: null, process_selection: [] }), {
  route_id: "design-bluecad", candidate_id: "candidate-1", bluecad_part_ids: ["part-1"]
});
assert.deepEqual(buildSurfaceRef("design-bluecad", { kind: "geometry-hit", viewerSessionId: "v", ephemeralObjectId: "x" }, { draft_id: null, process_selection: [] }), {
  route_id: "design-bluecad", candidate_id: null, bluecad_part_ids: []
});
assert.deepEqual(buildSurfaceRef("dashboard", null, { draft_id: "draft-1", process_selection: [] }), { route_id: "dashboard" });

for (const text of ['{"tool_calls":[{"name":"jarvis_process_act"}]}', '<function=jarvis_process_act>{}', '<|tool_call|>jarvis_bluecad_act', '<|im_start|>tool_call', '<start_function_call>jarvis_bluecad_act', 'jarvis_process_act({"op":"set_value"})', 'mcp__jarvis__jarvis_process_act({})']) {
  assert.equal(isToolCallShaped(text), true, text);
}
for (const text of [
  '```json\n{"name":"mcp__jarvis__jarvis_process_act","arguments":{"grant_id":"hidden"}}\n```',
  'A tool result: {"name":"jarvis_bluecad_act","arguments":{}} received.',
  'Embedded generic call: {"name":"other_tool","arguments":{}} received.',
  '~~~\n{"tool_calls":[{"name":"jarvis_process_act"}]}\n~~~',
]) assert.equal(isToolCallShaped(text), true, text);
for (const text of [
  '{"op":"set_value","target":"Feed","property":"pressure","value":{"value":3,"unit":"bar"}}',
  'Suggested change: [{"op":"move_part","part":"tube","dx":1}]',
  '```jarvis-actions\n{"surface":"process","actions":[]}\n```',
  'Suggested change: {"op":"set_value","target":"Feed"} done.',
]) assert.equal(isToolCallShaped(text), true, text);
for (const text of ["The selected stream is feed.", "{\"answer\":\"done\"}", null, ""]) assert.equal(isToolCallShaped(text), false, String(text));
for (const text of ["The JSON field `grant_id` identifies a grant.", "Use the jarvis_process_act tool to change pressure.", "JSON can describe a tool call.", "The JSON contains an `operation` property.", "The letters op appear in prose."]) assert.equal(isToolCallShaped(text), false, text);

assert.deepEqual(actionStatePresentation("applied"), { label: "Applied", tone: "success" });
assert.deepEqual(actionStatePresentation("proposed"), { label: "Proposed", tone: "pending" });
assert.deepEqual(actionStatePresentation("refused"), { label: "Refused", tone: "plain" });
assert.deepEqual(actionStatePresentation("stale"), { label: "Stale", tone: "plain" });
assert.deepEqual(actionStatePresentation("dismissed"), { label: "Dismissed", tone: "muted" });
assert.deepEqual(actionStatePresentation("undone"), { label: "Undone", tone: "muted" });
assert.equal(
  relayFailureMessage("relay_agent_error", "You've hit your session limit · resets 4:50am (Europe/San_Marino)"),
  "Relay agent unavailable: session limit reached (resets 4:50am)"
);
assert.equal(relayFailureMessage("relay_agent_error", "Error at C:\\private\\secret.log token=abc"), "the agent reported an error");

const sidecar = fs.readFileSync(new URL("../src/components/ai/useJarvisSidecar.tsx", import.meta.url), "utf8");
const cards = fs.readFileSync(new URL("../src/components/ai/WorkspaceActionCards.tsx", import.meta.url), "utf8");
const threadApi = fs.readFileSync(new URL("../src/api/threads.ts", import.meta.url), "utf8");
const actionApi = fs.readFileSync(new URL("../src/api/workspaceActions.ts", import.meta.url), "utf8");
const bluecadViewer = fs.readFileSync(new URL("../src/components/BluecadGlbViewer.tsx", import.meta.url), "utf8");
assert.match(threadApi, /surface_context: options\.surfaceContext/);
assert.match(threadApi, /surface_context: surfaceContext/);
assert.match(sidecar, /getSurfaceBrief\(workspaceId, surfaceContext\)/);
assert.match(sidecar, /addEventListener\("jarvis:workspace-action", refreshBrief\)/);
assert.match(sidecar, /surfaceBriefRefresh/);
assert.match(sidecar, /<WorkspaceActionCards actions=\{interaction\.actions \?\? \[\]\}/);
assert.match(sidecar, /surfaceBrief\?\.summary/);
assert.match(sidecar, /technicalDetails=\{interaction\.technical_details\}/);
assert.match(sidecar, /caught\.status === 409[\s\S]*getWorkspaceAction\(workspaceId, actionId\)/);
assert.match(cards, /change\.before \? `\$\{change\.before\} → ` : ""/);
assert.match(cards, /Revision \{action\.result_revision\.split\(":", 1\)\[0\]\}/);
assert.match(cards, /result_revision \}\) => \(\{ request, request_digest, origin, result_revision \}\)/);
const processEditor = fs.readFileSync(new URL("../src/stages/ProcessDraftEditor.tsx", import.meta.url), "utf8");
assert.match(processEditor, /refreshed draft revision \$\{next\.revision\.split\(":", 1\)\[0\]\}/);
assert.match(actionApi, /`\$\{basePath\(workspaceId\)\}\/brief`/);
assert.match(actionApi, /\/\$\{encodeURIComponent\(actionId\)\}\/apply/);
assert.match(bluecadViewer, /camera\.updateMatrixWorld\(true\);\s*raycaster\.setFromCamera\(pointer, camera\)/);

console.log("166 surface context and action presentation checks passed");
