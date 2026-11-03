import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const editor = read("src/stages/ProcessDraftEditor.tsx");
const api = read("src/api/processDraft.ts");
const proposals = read("src/components/process/ProcessProposals.tsx");
const stage = read("src/stages/ProcessStage.tsx");
const app = read("src/App.tsx");
const fail = (message) => {
  throw new Error(`155 process draft acceptance failed: ${message}`);
};
const has = (text, value, message) => {
  if (!text.includes(value)) fail(message);
};

// Instant Jarvis-side edits: every canvas edit is a draft patch; DWSIM is reached only through validate/run.
has(api, "/patch", "draft edits must go through the draft owner patch route");
has(api, "expected_revision: expectedRevision", "draft patches must carry compare-and-swap revisions");
has(editor, "patchDraft(workspaceId, draftId, revisionRef.current, ops)", "edits must patch the latest revision");
has(editor, "cause.status === 409", "a stale edit must reload the draft");
if (/dwsimEditor|runDwsimCommand/.test(editor)) fail("the draft editor must not call the DWSIM editor for edits");
if ((editor.match(/executeDraft\(/g) ?? []).length !== 1) fail("DWSIM must be reached only through one validate/run path");
has(editor, '"validate" | "run"', "validate and run are the only DWSIM actions");

// No frontend-owned engineering math: quantities travel as value + unit; the server converts.
has(editor, "{ value, unit: entry.unit }", "quantity inputs must send the operator's value and unit");
if (/273\.15|1e5|100000|\* 3600|\/ 3600|psi\s*\*/.test(editor + api + proposals)) fail("frontend performs unit conversion");
has(editor, "draft-composition", "composition must be edited as a table");
if (/JSON\.parse\(/.test(editor)) fail("the draft editor must not take raw JSON input");
if (/\b(?:localStorage|sessionStorage)\b/.test(editor + proposals)) fail("draft state must not persist locally");

// Verified materialization and run binding.
has(editor, "materialization_mismatch", "materialization mismatch must be rendered");
has(editor, "draft-diffs", "mismatch diagnostics must list path, expected and actual");
has(editor, "Retry", "a mismatch must offer a retry");
has(editor, 'results.state === "stale"', "stale results must be marked");
has(editor, "Values below describe that revision, not the current draft.", "stale banner must name the solved revision");
has(editor, "solvedRun.draft_revision", "results must show their exact revision");
has(editor, "materialization_fingerprint", "results must show the materialization fingerprint");
has(editor, "registry.unsupported", "unsupported equipment must be shown as unsupported");

// Structured proposals: highlighted on the canvas, approved or rejected by the operator.
has(editor, "is-proposal-target", "proposal targets must be highlighted on the canvas");
has(editor, "draft-proposal-badge", "proposal badges must show old and new values");
has(editor, "formatQuantity(change.current)} → ${formatQuantity(change.proposed)}", "badges must show current → proposed");
has(proposals, "approveProposal(", "proposals must be approvable");
has(proposals, "rejectProposal(", "proposals must be rejectable");
has(proposals, "The draft is unchanged until you approve.", "the pending state must be explicit");
has(proposals, 'proposal.state === "stale"', "stale proposals cannot be approved");
has(proposals, "announceDraftChanged(", "Sidecar decisions must refresh the canvas");
has(app, "<ProcessProposals workspaceId={workspaceId} />", "the Sidecar must present process proposals");
has(read("src/components/ai/useJarvisSidecar.tsx"), "{pinnedContent}", "proposals must be pinned visibly in the Sidecar");
has(stage, "<ProcessDraftEditor", "the Process stage must open the draft editor");
has(stage, "Native DWSIM cases (advanced)", "the 149 native editor must stay reachable");

console.log("155 process draft deterministic acceptance: PASS");
