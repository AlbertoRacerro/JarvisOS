// Spec 213 LUMEN-REFINEMENT-1 deterministic acceptance: magnitude-aware steppers, curated per-appearance accents,
// resizable shell panels, the rail Design switcher and the Studio sidecar default.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const require = createRequire(import.meta.url);

function load(path, imports = {}) {
  const module = { exports: {} };
  const code = ts.transpileModule(read(path), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 }
  }).outputText.replace(/import\.meta\.env/g, "({ DEV: false })");
  new Function("require", "module", "exports", code)((name) => imports[name] ?? require(name), module, module.exports);
  return module.exports;
}

// ---- Item 9: magnitude-aware, range-respecting stepping ----
const step = load("src/components/ui/numericStep.ts");
const walk = (start, direction, count, bounds) => {
  const out = [];
  let text = String(start);
  for (let i = 0; i < count; i += 1) {
    const next = step.stepNumeric(text, direction, bounds);
    out.push(next);
    if (next === null) break;
    text = step.formatStepped(next);
  }
  return out;
};
assert.deepEqual(walk(1, 1, 4), [2, 3, 4, 5], "from 1 upward the step is 1");
assert.deepEqual(walk(1, -1, 11), [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.09, 0.08], "below 1 tenths, then hundredths");
assert.deepEqual(walk(0.09, 1, 2), [0.1, 0.2], "upward crosses decades without float noise");
assert.deepEqual(walk(2.5, 1, 1), [3], "snaps up onto the step grid");
assert.deepEqual(walk(2.5, -1, 1), [2], "snaps down onto the step grid");
assert.deepEqual(walk(0.25, -1, 2), [0.2, 0.1]);
const positive = { min: 0, minExclusive: true };
assert.ok(walk(3, -1, 60, positive).every((value) => value === null || value > 0), "positive-only quantities never reach 0 or below");
const towardZero = walk(3, -1, 60);
assert.ok(towardZero.slice(0, towardZero.indexOf(0) + 1).every((value) => value >= 0) && towardZero.includes(0), "stepping toward zero lands on zero instead of crossing it");
assert.equal(step.stepNumeric("0", -1, { min: 0 }), null, "an inclusive minimum stops further stepping");
assert.equal(step.stepNumeric("9.5", 1, { max: 10 }), 10, "an overshoot clamps onto an inclusive maximum");
assert.equal(step.stepNumeric("10", 1, { max: 10 }), null);
assert.equal(step.stepNumeric("", 1, positive), 1, "an empty positive-only field steps to the first valid value");
assert.equal(step.stepNumeric("", -1, { min: 5 }), 5, "an empty field steps onto an inclusive minimum");
assert.deepEqual(walk(3, -1, 4, { min: 0, integer: true }), [2, 1, 0, null], "whole-number fields step by 1");
assert.deepEqual(walk(0, -1, 2), [-1, -2], "unbounded fields may go negative from zero");
assert.deepEqual(walk(-0.2, 1, 2), [-0.1, -0.09], "negative magnitudes shrink toward zero");
assert.equal(step.numericOrUndefined("1e-"), undefined);
assert.equal(step.numericOrUndefined(" 0.5 "), 0.5);

const NumericInput = load("src/components/ui/NumericInput.tsx", { "./numericStep": step }).default;
const atMinimum = renderToStaticMarkup(React.createElement(NumericInput, { value: "0.000001", min: 0, minExclusive: true, onValueChange() {}, "aria-label": "Wall" }));
assert.match(atMinimum, /role="spinbutton"/);
assert.match(atMinimum, /aria-valuemin="0"/);
assert.match(atMinimum, /inputMode="decimal"/);
assert.ok(!atMinimum.includes('type="number"'), "native fixed-step spinners are replaced");
assert.match(atMinimum, /<button type="button" tabindex="-1" disabled="">▾<\/button>/, "the down step is disabled at a positive-only minimum");

// Every Design numeric field uses the shared control.
const sourceFiles = (dir) => readdirSync(dir).flatMap((name) => {
  const full = join(dir, name);
  return statSync(full).isDirectory() ? sourceFiles(full) : full.endsWith(".tsx") ? [full] : [];
});
const rawNumberFields = sourceFiles(resolve(root, "src")).filter((file) => readFileSync(file, "utf8").includes('type="number"'));
assert.deepEqual(rawNumberFields, [], "raw number inputs remain outside the shared numeric control");

// ---- Items 7 and 8: cooler Lumen Light, curated per-appearance accents ----
const theme = load("src/theme.ts");
const channel = (hex, offset) => {
  const value = Number.parseInt(hex.slice(offset, offset + 2), 16) / 255;
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
};
const luminance = (hex) => (0.2126 * channel(hex, 1)) + (0.7152 * channel(hex, 3)) + (0.0722 * channel(hex, 5));
const contrast = (a, b) => (Math.max(luminance(a), luminance(b)) + 0.05) / (Math.min(luminance(a), luminance(b)) + 0.05);
assert.deepEqual([...theme.ACCENT_OPTIONS], ["microalgae", "leaf-chlorophyll", "lagoon", "glacier", "iris", "ember"]);
for (const preset of theme.ACCENT_OPTIONS) {
  const { light, dark } = theme.ACCENT_PRESETS[preset];
  assert.match(light, /^#[0-9A-F]{6}$/);
  assert.match(dark, /^#[0-9A-F]{6}$/);
  assert.ok(contrast(light, theme.accentForegroundHex(light)) >= 4.5, `${preset} Light fill text must meet AA`);
  assert.ok(contrast(dark, theme.accentForegroundHex(dark)) >= 4.5, `${preset} Dark fill text must meet AA`);
  assert.equal(theme.accentHex({ preset }, "light"), light);
  assert.equal(theme.accentHex({ preset }, "dark"), dark);
}
assert.equal(theme.ACCENT_PRESETS.microalgae.dark, "#8DB943", "Lumen Dark keeps the approved chlorophyll seed");
assert.equal(theme.safeAccentPreference({ preset: "custom", customHex: "#8DB943" }).preset, "microalgae", "a stored Custom HEX migrates to its nearest curated preset");
assert.equal(theme.safeAccentPreference({ preset: "custom", customHex: "#3366CC" }).preset, "glacier");
assert.equal(theme.safeAccentPreference({ preset: "custom", customHex: "#E07020" }).preset, "ember");
assert.equal(theme.safeAccentPreference({ preset: "custom", customHex: "nope" }).preset, "microalgae");
assert.equal(theme.safeAccentPreference({ preset: "unknown" }).preset, "microalgae");
assert.equal(theme.safeAccentPreference(null).preset, "microalgae");
const tokens = read("src/styles/tokens.css");
const lightBlock = tokens.slice(tokens.indexOf('[data-theme="light"]'), tokens.indexOf('[data-theme="dark"]'));
assert.ok(!/#8db943|rgb\(141 185 67/i.test(lightBlock), "Lumen Light no longer uses the yellowish chlorophyll green");

// ---- Item 1: one Lumen control family ----
const foundation = read("src/styles/foundation.css");
const defaultButton = foundation.slice(foundation.indexOf("button:not(.nav-button):not(.bluecad-candidate),"));
assert.match(defaultButton, /border-radius: var\(--lumen-radius-button\)/, "default buttons use the Lumen capsule");
assert.match(defaultButton, /background: var\(--color-accent-subtle\)/, "default buttons are a quiet tonal fill, not the full accent");
assert.ok(!/background: var\(--color-accent-primary\);\n\s*color: var\(--color-text-on-accent\)/.test(defaultButton), "the glaring flat accent fill is gone");

// ---- Item 6: resizable panels ----
const store = new Map();
globalThis.window = { localStorage: { getItem: (key) => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) } };
const widths = load("src/components/shell/panelWidths.ts");
assert.equal(widths.clampPanelWidth("navigator", 50), 200);
assert.equal(widths.clampPanelWidth("sidecar", 9999), 640);
assert.equal(widths.clampPanelWidth("sidecar", Number.NaN), 280);
assert.deepEqual(widths.parsePanelWidths('{"navigator": 999, "sidecar": "x", "other": 1}'), { navigator: 480 });
assert.deepEqual(widths.parsePanelWidths("not json"), {});
widths.writePanelWidths({ navigator: 300, sidecar: 420 });
assert.deepEqual(widths.readPanelWidths(), { navigator: 300, sidecar: 420 }, "panel widths persist per browser");
assert.equal(widths.keyboardPanelWidth("navigator", 300, "ArrowRight", false), 316, "the navigator grows to the right");
assert.equal(widths.keyboardPanelWidth("sidecar", 300, "ArrowLeft", true), 364, "the sidecar grows to the left");
assert.equal(widths.keyboardPanelWidth("sidecar", 300, "Home", false), 280);
assert.equal(widths.keyboardPanelWidth("navigator", 300, "End", false), 480);
assert.equal(widths.keyboardPanelWidth("navigator", 300, "Enter", false), null);
const splitter = read("src/components/shell/PanelSplitter.tsx");
for (const token of ['role="separator"', 'aria-orientation="vertical"', "aria-valuenow=", "tabIndex={0}", "setPointerCapture"]) assert.ok(splitter.includes(token), `splitter is missing ${token}`);

// ---- Items 2 and 5: rail Design switcher, Studio sidecar default ----
const AppLink = load("src/app/AppLink.tsx").default;
const routes = load("src/app/routes.ts");
const designTargets = load("src/app/designTargets.ts");
const Rail = load("src/components/shell/Rail.tsx", { "../../app/AppLink": { default: AppLink }, "../../app/routes": routes, "../../app/designTargets": designTargets, "../../app/navHistory": load("src/app/navHistory.ts"),
  // The Back/Forward icons: Phosphor's CommonJS build exports nothing under Node, so the rail gets its ES module.
  "@phosphor-icons/react": await import("@phosphor-icons/react") }).default;
// Spec 229 replaced the always-expanded switcher with one collapsed Design tools group, open on its own pages.
for (const path of ["/design/process", "/design/bluecad", "/design/studio"]) {
  const html = renderToStaticMarkup(React.createElement(Rail, { current: "design", currentPath: path, navigate() {} }));
  assert.match(html, /aria-label="Design tools"/);
  assert.equal((html.match(/aria-current="page"/g) ?? []).length, 1, "exactly one Design surface is current");
  assert.match(html, new RegExp(`href="${path}"[^>]*aria-current="page"`));
  assert.match(html, /<details open="">/, "the group is open on one of its own pages");
}
assert.match(renderToStaticMarkup(React.createElement(Rail, { current: "memory", currentPath: "/memory/models", navigate() {} })), /<details><summary>More Design tools/, "elsewhere the group is collapsed");
assert.ok(!read("src/pages/Studio.tsx").includes("design-stage__tabs"), "Studio no longer carries its own right-aligned switcher");
// Maintainer BUBU-LAUNCHER-1 supersedes the old default-open requirement.
assert.match(read("src/components/Layout.tsx"), /setSidecarOpen\(false\)/, "route entry restores the full work area");
assert.doesNotMatch(read("src/components/Layout.tsx"), /setSidecarOpen\([^)]*route\.id/, "route names do not auto-open Jarvis");

// ---- Items 3, 4 and 10: workspace scope label, tree -> viewport highlight, solver console reveal ----
const workbench = read("src/components/bluecad/BluecadWorkbench.tsx");
// Superseded: BlueCad no longer has a project of its own; the single project is chosen in the rail for every page.
assert.ok(!workbench.includes('aria-label="Workspace"') && read("src/components/shell/ProjectSwitcher.tsx").includes('designTarget("design.project-picker")'), "BlueCad follows the one app project chosen in the rail");
const viewer = read("src/components/BluecadGlbViewer.tsx");
assert.ok(viewer.includes("highlightMesh(meshKey ? meshByKey.get(meshKey) : undefined)") && viewer.includes("clearHighlight();"), "a selected part is highlighted in the viewport and restored on cleanup");
const projectTree = read("src/components/bluecad/BluecadProjectTree.tsx");
assert.ok(projectTree.includes('"Highlight in the viewport and inspect"') && projectTree.includes('"Open in the inspector"'), "tree rows must say what activating them does");
const draftEditor = read("src/stages/ProcessDraftEditor.tsx");
assert.ok(draftEditor.includes("setRevealRunId(started.run_id);") && draftEditor.includes('id="process-solver-console"') && draftEditor.includes('className="draft-console-link"'), "a Run reveals the solver console and the toolbar links to it");

console.log("213 Lumen refinement: ok");
