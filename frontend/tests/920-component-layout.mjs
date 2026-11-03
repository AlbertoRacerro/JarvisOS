import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const fail = (message) => { console.error(`920 component layout failed: ${message}`); process.exit(1); };
const page = read("src/pages/DesignProject.tsx");
const tree = read("src/components/design/DesignComponentTree.tsx");
const splitter = read("src/components/design/DesignSplitter.tsx");
const css = read("src/pages/designProject.css");

const header = page.split("<header")[1]?.split("</header>")[0] ?? fail("header missing");
if (!header.includes('id="design-version"')) fail("version selector must be in the header");
if (!header.includes('aria-label="Design stages"') || !header.includes("STAGES.map")) fail("stage bar must be inside the header");
if (page.split("</header>")[1].includes('aria-label="Design stages"')) fail("stage bar must not remain in the body");
if (!header.includes("(placeholder)")) fail("Motion/Mechanics must be marked as placeholder");

for (const token of ['role="tree"', 'role="treeitem"', "aria-level=", "aria-expanded=", "aria-selected=", "tabIndex={row.id === stop ? 0 : -1}"]) {
  if (!tree.includes(token)) fail(`tree missing ${token}`);
}
for (const key of ["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End", "Enter"]) if (!tree.includes(`case "${key}"`)) fail(`tree keyboard model missing ${key}`);
for (const node of ["Parameters", "Geometry", "Materials", "Physics", "Mesh", "Results", "Reduced model"]) if (!tree.includes(`"${node}"`)) fail(`tree missing node ${node}`);
if (tree.includes('"Studies"')) fail("Studies is merged into Results, not a node of its own");
if (!page.includes("<DesignComponentTree")) fail("page must render the tree");
if (page.includes("design-project__nodes") || css.includes("design-project__nodes")) fail("row-of-buttons node bar must be gone");

const viewport = page.split('aria-label="Component viewport"')[1]?.split("</section>")[0] ?? fail("viewport region missing");
if (!viewport.includes("<BluecadGlbViewer")) fail("viewport must render BluecadGlbViewer");
if (!viewport.includes("key={glbUrl}")) fail("viewer key must be stable on the artifact URL");
if (!viewport.includes("No geometry attached to")) fail("viewport empty state missing");
const inspector = page.split('aria-label="Inspector"')[1] ?? fail("inspector region missing");
if (inspector.includes("<BluecadGlbViewer")) fail("inspector must not render the viewer");
for (const id of ["design-edit-name", "design-edit-parent", "design-node-model", "design-geometry-candidate", "design-geometry-wall"]) if (!inspector.includes(`"${id}"`)) fail(`inspector missing ${id}`);

if ((page.match(/<DesignSplitter/g) ?? []).length !== 2) fail("two splitters required");
for (const token of ['role="separator"', 'aria-orientation="vertical"', "aria-valuenow=", "aria-valuemin=", "aria-valuemax=", "ArrowLeft", "ArrowRight", "onDoubleClick"]) if (!splitter.includes(token)) fail(`splitter missing ${token}`);
if (!/@media \(max-width: 850px\)[\s\S]*\.design-splitter \{ display: none/.test(css)) fail("narrow layout must hide splitters");
if (/#[0-9a-fA-F]{3,8}\b/.test(css)) fail("designProject.css must not hardcode colours");
console.log("920 component layout: PASS");
