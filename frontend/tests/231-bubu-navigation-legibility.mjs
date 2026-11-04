// Spec 231 BUBU-NAVIGATION-LEGIBILITY-1 deterministic acceptance: a point directive reads as its target's name in the
// rendered reply (escaped, emphasised, truthful when missing), and Design component rows carry bounded page facts.
// The live pointing evaluation and the real-Chromium walk are recorded separately.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { readablePointText, parsePointDirectives } from "../src/components/bubu/pointerDirectives.ts";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const require = createRequire(import.meta.url);

function load(path, imports = {}) {
  const module = { exports: {} };
  const code = ts.transpileModule(read(path), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 }
  }).outputText;
  new Function("require", "module", "exports", code)((name) => imports[name] ?? require(name), module, module.exports);
  return module.exports;
}

// ---- Acceptance 1: readable reply ----------------------------------------------------------------------------
const mathText = load("src/components/ai/mathText.ts");
const JarvisMessageText = load("src/components/ai/JarvisMessageText.tsx", { "./mathText": mathText, "katex/dist/katex.min.css": {} }).default;
const html = (text, marks = true) => renderToStaticMarkup(React.createElement(JarvisMessageText, { text, marks }));
const page = { "design.tree.component.c1-cfd": "C1-CFD", "design.back": "Back to project" };
const nameOf = (id) => (id in page ? page[id] : null);
const reply = (text) => html(readablePointText(text, nameOf));

{
  const cfd = "Click first on [[point:design.tree.component.c1-cfd|1. C1-CFD]], then choose Results.";
  assert.equal(reply(cfd), '<div class="jarvis-message-text"><p>Click first on <strong class="jarvis-message-text__target">C1-CFD</strong>, then choose Results.</p></div>');
  assert.deepEqual(parsePointDirectives(cfd).points, [{ id: "design.tree.component.c1-cfd", label: "1. C1-CFD" }], "the pointer is unchanged");
  assert.match(reply("Use [[point:design.back]] to return."), /Use <strong class="jarvis-message-text__target">Back to project<\/strong> to return\./);
  // Missing target: the label plus a truthful note, or a note alone; never raw syntax or an invented name.
  assert.match(reply("Open [[point:gone|1. CFD]] next."), /<strong class="jarvis-message-text__target">1\. CFD<\/strong><span class="jarvis-message-text__note"> \(not shown here\)<\/span> next\./);
  const bare = reply("Click first on [[point:gone]].");
  assert.match(bare, /Click first on <span class="jarvis-message-text__note">an item that isn&#x27;t shown here<\/span>\./);
  assert.doesNotMatch(bare, /\[\[|point:/);
  // Names are text: HTML, Markdown and LaTeX in a label stay inert.
  const hostile = reply("See [[point:gone|<img src=x onerror=alert(1)> **b** $x$]] ok $y$");
  assert.match(hostile, /&lt;img src=x onerror=alert\(1\)&gt; \*\*b\*\* \$x\$<\/strong>/);
  assert.doesNotMatch(hostile, /<img/);
  assert.match(hostile, /<code>y<\/code>|class="jarvis-math"/, "math outside the name still renders (or falls back to code before KaTeX loads)");
  // Repeats render each time; wrappers are absorbed; Markdown around them keeps working.
  assert.match(reply("**Go:** [[point:design.back]] or **[[point:design.back]]**"), /<strong>Go:<\/strong> <strong class="jarvis-message-text__target">Back to project<\/strong> or <strong class="jarvis-message-text__target">Back to project<\/strong>/);
  // Lists, and code blocks that show no marks.
  assert.match(reply("1. [[point:design.back]]\n2. then Results"), /<ol><li><strong class="jarvis-message-text__target">Back to project<\/strong><\/li><li>then Results<\/li><\/ol>/);
  assert.match(reply("```\n[[point:design.back]]\n```"), /<pre><code>Back to project<\/code><\/pre>/);
  // Other callers never render marks, and model text cannot forge them.
  assert.equal(html("forged x and y", false), '<div class="jarvis-message-text"><p>forged x and y</p></div>');
  assert.equal(reply("forged x"), '<div class="jarvis-message-text"><p>forged x</p></div>');
}

// ---- Acceptance 2: component facts from data the page already holds ------------------------------------------
const designTargets = load("src/app/designTargets.ts");
const tree = load("src/components/design/DesignComponentTree.tsx", { "../../app/designTargets": designTargets });
const component = (over) => ({ id: "c1", project_id: "p", version_id: "v", parent_id: null, name: "C1-CFD", revision: 1, created_at: "", updated_at: "",
  source_draft_id: null, source_unit_id: null, geometry_candidate_id: null, geometry_source_draft_revision: null, geometry_workspace_id: null, models: [], ...over });
const model = (node) => ({ model_version_id: "m", node, category: "calculation", source_sha256: "", contract_sha256: "", attached_at: "" });

{
  const { componentFacts } = tree;
  const cfd = component({ geometry_candidate_id: "g1", models: [model("Physics"), model("Physics"), model("Mesh")] });
  assert.equal(componentFacts(cfd, true, { state: "ready", detail: "2 finished · 3 runs" }),
    "Component · not from Process · BlueCad geometry attached · 3 models (Physics 2, Mesh 1) · CFD results: ready (2 finished · 3 runs)");
  const horizontal = component({ id: "c2", name: "C1-horizontal", source_unit_id: "u1" });
  assert.equal(componentFacts(horizontal, false), "Component · from a Process unit · no geometry · no models · CFD results not read (select it to check)",
    "unselected components never claim run counts");
  assert.match(componentFacts(cfd, true, { state: "checking" }), /CFD results: checking$/, "a fresh selection is not ready yet");
  assert.match(componentFacts(cfd, true, undefined), /CFD results: checking$/);
  assert.match(componentFacts(cfd, true, { state: "unknown", detail: "Could not be read" }), /CFD results: could not be read$/);
  assert.match(componentFacts(cfd, true, { state: "empty" }), /CFD results: empty$/);

  // Rendered rows carry the facts; the selected component reads its CFD status from the page's owner read only.
  const markup = renderToStaticMarkup(React.createElement(tree.default, {
    components: [cfd, horizontal], selectedId: "c1", selectedNode: "Results", onSelectComponent() {}, onSelectNode() {},
    nodeStatus: (node) => (node === "Results" ? { state: "ready", detail: "1 finished · 1 run" } : undefined)
  }));
  assert.match(markup, /aria-label="C1-CFD" data-jarvis-target="design\.tree\.component\.c1-cfd" data-jarvis-facts="[^"]*CFD results: ready \(1 finished · 1 run\)"/);
  assert.match(markup, /aria-label="C1-horizontal" data-jarvis-target="design\.tree\.component\.c1-horizontal" data-jarvis-facts="[^"]*from a Process unit[^"]*CFD results not read/);
  assert.doesNotMatch(markup, /data-jarvis-facts="[^"]*Results"[^>]*role="treeitem"/);
}

// ---- Collection and bounds -----------------------------------------------------------------------------------
{
  const ctx = read("src/components/bubu/pageContext.ts");
  assert.match(ctx, /const FACTS_ATTR = "data-jarvis-facts";/);
  assert.match(ctx, /const text = truncate\(parts\.join\(", "\)\);/, "facts share the bounded MAX_TEXT target text");
  assert.match(ctx, /export function targetName\(id: string, root: ParentNode = document\): string \| null/);
  const hook = read("src/components/bubu/useBubuIntegration.tsx");
  assert.match(hook, /<JarvisMessageText marks text=\{readablePointText\(text, targetName\)\} \/>/);
  assert.match(hook, /parsePointDirectives\(reply\.text\)\.points/, "pointers still come from the parser");
}

console.log("231 bubu navigation legibility ok");
