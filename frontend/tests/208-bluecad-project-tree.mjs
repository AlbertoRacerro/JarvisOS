// Spec 208 BLUECAD-PROJECT-TREE-1 deterministic acceptance: compact, keyboard-inspectable BlueCad project tree.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relative) => fs.readFileSync(path.join(root, relative), "utf8");
const failures = [];
const check = (condition, message) => { if (!condition) failures.push(message); };

const tree = read("src/components/bluecad/BluecadProjectTree.tsx");
const workbench = read("src/components/bluecad/BluecadWorkbench.tsx");
const css = read("src/styles/bluecad-final.css");
const client = read("src/api/client.ts");

// WAI-ARIA tree pattern: roles, state attributes, a single roving tab stop and the full keyboard model.
for (const token of ['role="tree"', 'role="treeitem"', 'role="group"', "aria-level=", "aria-expanded=", "aria-selected=", "aria-disabled="]) {
  check(tree.includes(token), `project tree is missing ${token}`);
}
check(/tabIndex=\{node\.id === tabStop \? 0 : -1\}/.test(tree), "project tree must expose exactly one roving tab stop");
for (const key of ["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End", "Enter", '" "']) {
  check(tree.includes(`case ${key.startsWith('"') ? key : `"${key}"`}`), `project tree keyboard model is missing ${key}`);
}

// Inspectable contents of the selected candidate, from data the workbench already loads.
for (const group of ['"Parts"', '"Studio references"', '"Artifacts"', '"Validation"']) check(tree.includes(group), `project tree is missing the ${group} node`);
check(tree.includes("onSelectMesh(") && workbench.includes("onSelectMesh={requestInspection}"), "part nodes must drive the existing geometry inspection");
check(workbench.includes('onInspect={() => requestShellRegionOpen("sidecar")}'), "content nodes must open the existing inspector");
// Status is text, not colour only; a parked reason stays available to assistive technology.
check(tree.includes("bluecad-tree__status") && tree.includes("{node.status}</span>"), "lifecycle status must render as text");
check(tree.includes("aria-describedby={descriptionId}") && tree.includes("Parked: "), "parked reason must stay available on the candidate node");

// The verbose navigator is gone: one compact toolbar, the composer collapsed behind one control.
check(workbench.includes("<BluecadProjectTree"), "workbench navigator must render the project tree");
check(!workbench.includes('className="bluecad-candidate-list"'), "the stacked candidate-card list must not return");
check(workbench.includes("bluecad-navigator__toolbar"), "navigator controls must form one compact toolbar");
check(/aria-expanded=\{composerOpen\}/.test(workbench) && /\{composerOpen && <form id="bluecad-brief-composer"/.test(workbench), "the brief composer must be collapsed behind one disclosure control");
check(/const openBriefComposer = \(\) => \{\s*setComposerOpen\(true\);/.test(workbench), "opening the brief composer from elsewhere must expand it");
check(/setBriefText\(duplicateBrief\(selected\.brief_text\)\.briefText\);\s*setComposerOpen\(true\);/.test(workbench), "Duplicate brief must open the composer");
for (const label of ["Workspace", "Filter candidates", "Refresh", "New candidate brief"]) check(workbench.includes(label), `navigator lost the accessible name ${label}`);

// Focus restoration keeps targeting the candidate node.
check(workbench.includes("registerCandidate={registerCandidate}") && workbench.includes("candidateRefs.current[candidateId] = node"), "candidate nodes must stay registered for focus restoration");

// Styles: visible focus on the tree item, tokenised colours only, no API change.
check(css.includes(".bluecad-tree__item:focus-visible > .bluecad-tree__row"), "tree items need a visible focus indicator");
const treeCss = css.slice(css.indexOf("/* Spec 208"));
check(!/#[0-9a-fA-F]{3,8}\b|rgba?\(/.test(treeCss), "project tree styles must use Lumen tokens, not literal colours");
check(!/BLUECAD/.test(tree), "product text must spell BlueCad");
check(!client.includes("project-tree"), "spec 208 must not add an API route");

if (failures.length) {
  console.error(`208 project tree checks failed:\n- ${failures.join("\n- ")}`);
  process.exit(1);
}
console.log("208 BlueCad project tree checks passed");
