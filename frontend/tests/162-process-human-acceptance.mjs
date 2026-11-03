import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const editor = read("src/stages/ProcessDraftEditor.tsx");
const stage = read("src/stages/ProcessStage.tsx");
const api = read("src/api/processDraft.ts");
const menu = read("src/components/ui/ContextMenu.tsx");
const editorCss = read("src/stages/ProcessDraftEditor.css");
// Spec 181 moved the results tables into the run results component.
const runResults = read("src/components/process/RunResults.tsx");

assert.match(editor, /set_orientation/);
assert.match(editor, /onContextMenu=\{\(event\).*unitMenu\.targetProps\.onContextMenu/);
assert.match(editor, /unitMenu\.targetProps\.onKeyDown/);
assert.match(editor, /<MenuButton label=\{`Orientation for/);
assert.match(editor, /unit\.flip_x/);
assert.match(editor, /unit\.flip_y/);
assert.match(editor, /set_orientation.*id: contextUnit\.id/);
assert.match(editor, /data-testid="readiness-chip"/);
assert.match(editor, /blockers.*warning/);
assert.match(editor, /Readiness guidance/);
assert.match(editor, /const shownFindings = useMemo\(\(\) => visibleFindings\(draft\?\.findings \?\? \[\]\)/);
assert.match(editor, /shownWarningCount = shownFindings\.length - shownBlockerCount/);
assert.match(editor, /\{shownFindings\.map\(\(finding, index\)/);
assert.doesNotMatch(editor, /draft\.findings\.length - blockers\.length/);
// Solve failure messages are still rendered, minus the one the run outcome already states.
assert.match(editor, /solveFailureMessages\(run\)\.filter\(fresh\)/);
assert.match(editor, /solveMessages\.map\(/);
assert.match(editor, /error_detail\.dwsim_message/);
assert.match(editor, /molar_flow/);
assert.match(runResults, /volumetric_flow/);
assert.match(editor, /Composition basis/);
assert.match(api, /molar_flow\?: StoredQuantity/);
assert.match(api, /vapor_fraction\?: StoredQuantity/);
assert.match(api, /solve_errors\?: string\[\]/);
assert.match(menu, /event\.key !== "ContextMenu"/);
assert.match(menu, /event\.key === "F10" && event\.shiftKey/);
assert.match(editor, /className="draft-run-button"/);
assert.match(editor, /<details className="draft-secondary">/);
assert.match(stage, /<details className="process-advanced">/);
assert.match(editor, /values below describe that revision/i);
assert.match(editor, /unitWidth = \(unit: DraftObject\) => Math\.max\(UNIT_W/);
assert.match(editor, /function solveFailureMessages\(run: DraftRun\)/);
assert.match(editor, /const failedIds = new Set\(noTextFailures\)/);
assert.match(editor, /Not calculated because \$\{cause\}/);
assert.match(editor, /visibleFindings\(draft\?\.findings \?\? \[\]\)/);
assert.match(editorCss, /--color-status-warning-bg/);
assert.match(editorCss, /--color-status-warning-text/);
assert.match(editorCss, /--color-status-danger-bg/);
assert.match(editorCss, /\.draft-findings li button:not\(\.nav-button\):not\(\.bluecad-candidate\)/);

console.log("162 process human acceptance source contract: PASS");
