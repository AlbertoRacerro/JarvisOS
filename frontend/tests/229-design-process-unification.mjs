// Spec 229 DESIGN-PROCESS-UNIFICATION-1 deterministic acceptance: one Design entry, the collapsed Design tools
// group, the semantic target registry and postconditions, owner-read tree badges and the declared field stretch.
// The operator discovery walk itself is the real-browser proof in e2e/design_discovery_walk.mjs.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, relative, resolve } from "node:path";
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

// ---- Acceptance 2: one Design entry; every older route stays a valid deep link ----
const targets = load("src/app/designTargets.ts");
const routes = load("src/app/routes.ts", { "./designTargets": targets });
for (const path of ["/", "/home"]) {
  const resolved = routes.resolveRoute(path);
  assert.equal(resolved.route.id, "design-project", `${path} opens New Design`);
  assert.equal(resolved.shouldReplace, true);
}
assert.equal(routes.PRIMARY_NAV_ITEMS.find((item) => item.id === "design").href, "/design/project", "the Design rail item opens New Design");
for (const [path, id] of [["/design/process", "design-process"], ["/design/studies", "design-studies"], ["/design/bluecad", "design-bluecad"],
  ["/design/studio", "design-studio"], ["/engineering-data", "engineering-data"], ["/runs", "runs"], ["/design/project", "design-project"]]) {
  const resolved = routes.resolveRoute(path);
  assert.equal(resolved.route.id, id, `${path} still resolves`);
  assert.equal(resolved.shouldReplace, false, `${path} is not redirected`);
}
assert.deepEqual(routes.DESIGN_TOOL_ITEMS.map((item) => item.href),
  ["/design/process", "/design/studies", "/design/bluecad", "/design/studio", "/engineering-data", "/runs"], "every older Design surface is in the tools group");

// ---- Acceptance 6: one registry; each target is placed exactly once in the source ----
const { DESIGN_TARGETS, DESIGN_STAGE_POSTCONDITIONS } = targets;
const ids = Object.keys(DESIGN_TARGETS);
assert.ok(ids.length >= 30);
for (const [id, target] of Object.entries(DESIGN_TARGETS)) {
  assert.match(id, /^(rail|design)\.[a-z0-9.-]+$/, `target id ${id} follows the naming scheme`);
  assert.ok(target.role && target.name, `${id} declares a role and an accessible name`);
  assert.equal(id.split(".")[0], target.surface === "rail" ? "rail" : "design", `${id} prefix matches its surface`);
}
const sources = [];
const walk = (dir) => { for (const name of readdirSync(dir)) { const path = join(dir, name); if (statSync(path).isDirectory()) walk(path); else if (/\.(ts|tsx)$/.test(name)) sources.push(path); } };
walk(resolve(root, "src"));
const placements = new Map(ids.map((id) => [id, []]));
for (const path of sources) {
  if (path.endsWith(join("app", "designTargets.ts"))) continue;
  if (/components[\\/]bubu[\\/](guidedTour\.ts|useGuidedTour\.tsx)$/.test(path)) continue; // Spec 242: the tour refers to targets, it does not place them
  const text = readFileSync(path, "utf8");
  for (const id of ids) {
    const count = text.split(`"${id}"`).length - 1;
    for (let i = 0; i < count; i += 1) placements.get(id).push(relative(root, path));
  }
}
for (const [id, where] of placements) assert.equal(where.length, 1, `${id} is placed exactly once (found in: ${where.join(", ") || "nowhere"})`);
const stages = ["Plant", "Dynamic", "CAD", "Motion/Mechanics", "CFD", "Optics", "Reduced model"];
assert.deepEqual([...load("src/pages/designView.ts", { "../components/design/DesignComponentTree": { DESIGN_NODES: [] } }).STAGES], stages, "postconditions follow the stage bar order");
assert.deepEqual(Object.keys(DESIGN_STAGE_POSTCONDITIONS), stages, "every stage declares postconditions");
for (const stage of stages) {
  assert.ok(DESIGN_STAGE_POSTCONDITIONS[stage].length >= 1, `${stage} has a postcondition`);
  for (const item of DESIGN_STAGE_POSTCONDITIONS[stage]) assert.ok(ids.includes(item.target) && item.holds, `${stage} postcondition targets a registered target`);
}

// ---- Rail targets render exactly once with their role and accessible name ----
const AppLink = load("src/app/AppLink.tsx").default;
const Rail = load("src/components/shell/Rail.tsx", { "../../app/AppLink": { default: AppLink }, "../../app/routes": routes, "../../app/designTargets": targets, "../../app/navHistory": load("src/app/navHistory.ts"),
  // The Back/Forward icons: Phosphor's CommonJS build exports nothing under Node, so the rail gets its ES module.
  "@phosphor-icons/react": await import("@phosphor-icons/react") }).default;
const TAG_ROLE = { a: "link", nav: "navigation", button: "button" };
for (const currentPath of ["/design/project", "/memory/models", "/runs"]) {
  const html = renderToStaticMarkup(React.createElement(Rail, { current: currentPath.startsWith("/design") ? "design" : currentPath === "/runs" ? undefined : "memory", currentPath, navigate() {} }));
  for (const [id, target] of Object.entries(DESIGN_TARGETS).filter(([, item]) => item.surface === "rail")) {
    const matches = [...html.matchAll(new RegExp(`<(a|nav|button)\\b[^>]*data-jarvis-target="${id.replace(/\./g, "\\.")}"[^>]*>`, "g"))];
    assert.equal(matches.length, 1, `${id} renders exactly once on ${currentPath}`);
    const tag = matches[0][1];
    assert.equal(TAG_ROLE[tag], target.role, `${id} exposes ${target.role}`);
    // Links are named by their text; the navigation groups and the icon-only Back/Forward buttons by aria-label.
    if (tag !== "a") assert.match(matches[0][0], new RegExp(`aria-label="${target.name}"`));
    else assert.ok(html.includes(`data-jarvis-target="${id}">${target.name}</a>`), `${id} is named ${target.name}`);
  }
  assert.equal((html.match(/aria-current="page"/g) ?? []).length, 1, "exactly one current page");
}
const onProject = renderToStaticMarkup(React.createElement(Rail, { current: "design", currentPath: "/design/project", navigate() {} }));
assert.match(onProject, /href="\/design\/project"[^>]*aria-current="page"[^>]*data-jarvis-target="rail\.design"/, "Design is the current page on New Design");
assert.match(onProject, /<details><summary>More Design tools/, "the tools group is collapsed on New Design");

// ---- Acceptance 3: the picker changes the workspace only through the App owner ----
const app = read("src/App.tsx");
assert.ok(app.includes("<DesignProject projectId={workspaceId} onProjectChange={setWorkspaceId}"), "New Design switches projects through the App owner");
const design = read("src/pages/DesignProject.tsx");
// The project picker moved from New Design to the rail, where one picker serves every page.
assert.ok(!design.includes('designTarget("design.project-picker")'), "New Design has no project picker of its own");
assert.ok(app.includes("onChange={setWorkspaceId}") && read("src/components/shell/ProjectSwitcher.tsx").includes("onChange={(event) => onChange(event.target.value)}"), "the rail picker calls the owner");
assert.ok(design.includes("onClick={() => switchProject(offerExample.id)}") && design.includes("onProjectChange(id);"), "Open case study C1 calls the owner");
// Case studies (examples) are offered in the version picker and switch the project through the same owner.
assert.ok(design.includes('<optgroup label="Versions">') && design.includes('<optgroup label="Case studies">') && design.includes("if (kind === \"case\") { switchProject(id); return; }"),
  "the version picker lists the home's versions and the case studies");
assert.ok(!/writeStoredWorkspaceId|jarvisos\.active-workspace|sessionStorage/.test(design), "no second selection store");
assert.ok(design.includes("item.slug === C1_EXAMPLE_SLUG") && read("src/app/exampleWorkspace.ts").includes('C1_EXAMPLE_SLUG = "example-c1-smooth-tube"'));

// ---- Acceptance 4: truthful tree badges ----
const { cfdNodeStatus } = load("src/components/design/designNodeStatus.ts");
const ready = { state: "ready", templates: 2, runs: [{ state: "finished" }, { state: "finished" }, { state: "failed" }], reduced: [{ status: "current", process_unit_pinned: true }] };
// One status word per badge (empty / ready / running / checking / stale / failed / unknown); the counts the owner read
// are kept in the badge detail (its tooltip).
// Studies merged into Results: the templates and runs counts are kept in the Results badge detail.
assert.equal(cfdNodeStatus(ready, "Studies"), undefined, "Studies is no longer a node");
assert.deepEqual(cfdNodeStatus(ready, "Mesh"), { state: "ready", detail: "3 run meshes" });
assert.deepEqual(cfdNodeStatus(ready, "Results"), { state: "ready", detail: "2 finished · 3 runs · 2 templates" });
assert.deepEqual(cfdNodeStatus({ ...ready, runs: [] }, "Results"), { state: "empty", detail: "2 templates" }, "templates without a run read empty");
assert.deepEqual(cfdNodeStatus(ready, "Reduced model"), { state: "ready", detail: "Current · pinned on Process unit" });
assert.deepEqual(cfdNodeStatus({ ...ready, reduced: [{ status: "obsolete", process_unit_pinned: true }] }, "Reduced model"), { state: "stale", detail: "1 published · not current" });
assert.equal(cfdNodeStatus({ ...ready, runs: [{ state: "failed" }] }, "Results").state, "failed", "a component whose runs all failed says so");
assert.equal(cfdNodeStatus({ ...ready, runs: [{ state: "failed" }, { state: "running" }] }, "Results").state, "running");
const empty = { state: "ready", templates: 0, runs: [], reduced: [] };
for (const node of ["Mesh", "Results", "Reduced model"]) {
  assert.deepEqual(cfdNodeStatus(empty, node), { state: "empty" }, `${node} says empty only after reading its owner`);
  assert.equal(cfdNodeStatus({ state: "unavailable" }, node).state, "unknown", `${node} never says empty when the read failed`);
  assert.deepEqual(cfdNodeStatus({ state: "loading" }, node), { state: "checking" });
}
for (const node of ["Parameters", "Geometry", "Materials", "Physics"]) assert.equal(cfdNodeStatus(ready, node), undefined, `${node} keeps its own badge`);
const processEditor = read("src/stages/ProcessDraftEditor.tsx");
assert.ok(processEditor.includes("Steady-state solve · {results.state === \"none\" ? \"No results\""), "the Process result chip names its subsystem");
assert.ok(read("src/components/process/PbrInspector.tsx").includes("<dt>Steady-state results</dt>"));
assert.ok(read("src/pages/DesignOpticsPanel.tsx").includes("<h3>Prediction · Dynamic Studio</h3>"), "the prediction names its subsystem");

// ---- Acceptance 5: legible field slice with the stretch declared; data unchanged ----
const slice = load("src/pages/cfdFieldSlice.ts");
const tube = [[0, -0.023], [2.6, -0.023], [2.6, 0.023], [0, 0.023]];
const plain = slice.fitTransform(tube, 640, 320);
assert.ok(0.046 * plain.scaleY < 15, "unstretched, the C1 tube section is a strip under 15 px");
const fit = slice.fitTransform(tube, 640, 320, 8, 0.35);
assert.equal(fit.stretchAxis, "y");
assert.ok(Math.abs(0.046 * fit.scaleY - 0.35 * 304) < 1e-9, "the thin axis fills 35 % of the viewport");
assert.ok(fit.stretch > 5 && Math.abs(fit.stretch - fit.scaleY / fit.scale) < 1e-12, "the stretch factor is reported");
for (const point of tube) { const [x, y] = slice.toScreen(fit, point); assert.ok(x >= 8 - 1e-9 && x <= 632 + 1e-9 && y >= 8 - 1e-9 && y <= 312 + 1e-9); }
const square = slice.fitTransform([[0, 0], [1, 1]], 640, 320, 8, 0.35);
assert.equal(square.stretchAxis, null, "a well-proportioned section is not stretched");
const payload = { status: "available", time: "1", association: "cell", plane: { normal: [0, 0, 1] }, points: [[0, -0.023, 0], [2.6, -0.023, 0], [2.6, 0.023, 0], [0, 0.023, 0]],
  polygons: [[0, 1, 2, 3]], fields: { p: { values: [3], min: 0, max: 6, units: "m2/s2" }, U: { values: [[1, 0, 0]], magnitude_min: 0, magnitude_max: 2, units: "m/s" } } };
const parsed = slice.parseFieldPayload(payload);
assert.deepEqual(slice.buildPolygons(parsed.slice, "p", 640, 320, 0.35).polygons.map((item) => item.fill), slice.buildPolygons(parsed.slice, "p", 640, 320).polygons.map((item) => item.fill), "colours are unchanged by the stretch");
const panel = read("src/pages/CfdFieldSlicePanel.tsx");
assert.ok(panel.includes("Display only: the {drawn.fit.stretchAxis") && panel.includes("Field values and geometry data are unchanged."), "the stretch is declared on screen");

// ---- Acceptance 6: read-only Design state through the existing surface owner, no egress ----
const surface = read("src/app/workspaceActionSurface.ts");
assert.ok(surface.includes("export function publishDesignSurface") && surface.includes("export function listenForDesignSurface"));
assert.ok(!/request|fetch\(/.test(surface), "the Design state is never sent anywhere");
assert.ok(design.includes("publishDesignSurface(projectId ? {") && design.includes("useEffect(() => () => publishDesignSurface(null), []);"));
assert.ok(!/<main\b/.test(design), "Design panels are regions inside the shell's single main landmark");

assert.match(read("package.json"), /"test:229": "node tests\/229-design-process-unification\.mjs"/);
assert.match(read("package.json"), /npm run test:229/);
console.log("229 design process unification ok");
