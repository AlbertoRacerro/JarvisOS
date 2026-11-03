import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Spec 182: the flowsheet canvas owns the central width; Setup and the inspector are temporary panels.
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const editor = read("src/stages/ProcessDraftEditor.tsx");
const editorCss = read("src/stages/ProcessDraftEditor.css");
const resultsCss = read("src/components/process/RunResults.css");

const toolbar = editor.slice(editor.indexOf('<div className="draft-toolbar"'), editor.indexOf("{biologyOpen && <BiologyModelLibrary"));
// Toolbar: Biology models, Setup, Run and the run status chip, in that order, plus the results state.
const order = ["Biology models…", "draft-setup-toggle", "draft-run-button", "<RunStateIndicator", 'data-testid="results-state"'];
order.reduce((at, needle) => {
  const next = toolbar.indexOf(needle);
  assert.ok(next > at, `toolbar must hold ${needle} after the previous control`);
  return next;
}, -1);
// The toolbar name must not collide with the "Process draft" select inside More controls.
assert.match(editor, /role="toolbar" aria-label="Process actions"/);
assert.match(toolbar, /aria-expanded=\{setupOpen\} aria-controls="draft-setup-drawer"/);
assert.match(toolbar, /setupOpen \? closeSetup\(\) : openSetup\(\)/, "Setup toggles open and closed from the toolbar");

// Canvas full width when nothing is selected: two columns only; the inspector column exists only with a selection.
assert.match(editorCss, /\.draft-body \{\n  display: grid;\n  grid-template-columns: 140px minmax\(0, 1fr\);\n/);
assert.match(editorCss, /\.draft-body--inspector \{ grid-template-columns: 140px minmax\(0, 1fr\) 300px; \}/);
assert.match(editorCss, /draft-body--wide-inspector[\s\S]*380px/);
assert.match(editor, /draft-body\$\{selected \? " draft-body--inspector" : ""\}/);
assert.match(editor, /\{selected \? \(\n\s*<aside className="draft-inspector" aria-label="Inspector"/);
assert.doesNotMatch(editor, /draft-inspector__empty/, "no permanent empty inspector");
assert.doesNotMatch(editor, /Open setup \(species and thermodynamics\)/);

// Inspector closes and restores width: close button, Escape, background click; focus returns to the canvas.
assert.match(editor, /aria-label="Close inspector"[^>]*onClick=\{\(\) => setSelectedId\(null\)\}/);
assert.match(editor, /if \(click\) setSelectedId\(null\)/, "a click on the empty canvas deselects");
assert.match(editor, /event\.key !== "Escape"/);
assert.match(editor, /<div className="draft-editor" onKeyDown=\{onEditorKeyDown\}>/);
assert.match(editor, /wasInspectorOpen\.current && !inspectorOpen[\s\S]*svgRef\.current\?\.focus/);
assert.match(editor, /ref=\{viewport\.attach\}\n\s*tabIndex=\{-1\}/);

// Setup is an overlay drawer inside the canvas frame (zero width when closed), focus in on open, back on close.
const frame = editor.slice(editor.indexOf('<div className="draft-canvas-frame">'), editor.indexOf("<ContextMenu label=\"Unit orientation\""));
assert.match(frame, /\{setupOpen && \(\n\s*<section id="draft-setup-drawer"/);
assert.match(resultsCss, /\.draft-setup-drawer \{ position: absolute;/);
assert.match(editor, /const closeSetup = \(\) => \{ setSetupOpen\(false\); setupToggleRef\.current\?\.focus/);
assert.match(editor, /setupRef\.current\?\.focus/);
// Proposals stay visible without a selection: they sit below the flowsheet box, outside the inspector.
assert.ok(editor.indexOf("<ProcessProposals workspaceId") > editor.indexOf('<div className="draft-below">'));
assert.ok(editor.indexOf('<div className="draft-below">') > editor.indexOf('<aside className="draft-inspector"'));
console.log("182 process layout: ok");
