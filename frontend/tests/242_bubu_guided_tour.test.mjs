// Spec 242: guided tour source-pattern test (stop order, known targets, no mutating controls, wiring, build chain).
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const tour = read("src/components/bubu/guidedTour.ts");
const runner = read("src/components/bubu/useGuidedTour.tsx");
const targets = read("src/app/designTargets.ts");
const pkg = read("package.json");

const stops = [...tour.matchAll(/route: "([^"]+)", target: "([^"]+)", title: "([^"]+)"/g)].map((m) => ({ route: m[1], target: m[2], title: m[3] }));

// ---- order and size: the areas of the spec, in order, 12-18 stops ----
assert.ok(stops.length >= 12 && stops.length <= 18, `roughly 12-18 stops, found ${stops.length}`);
assert.deepEqual(stops.map((s) => s.title), [
  "The left rail", "Project picker", "Design stages", "Components tree", "Component nodes", "Geometry node", "Feature inspector",
  "CFD stage", "Compare runs", "3D symmetry view", "Plant", "Dynamic", "More Design tools", "Memory", "Development", "Coding", "Settings"
]);
assert.deepEqual(stops.map((s) => s.route.split("?")[0]).filter((r, i, all) => r !== all[i - 1]),
  ["/design/project", "/memory/project-basis", "/development/roadmap/timeline", "/coding/repository", "/settings/appearance"]);
assert.match(tour, /title: "Feature inspector"[\s\S]*?\n    text: ".*Placed freely.*Place in viewport/);
assert.equal(tour.split("title: \"").length - 1, stops.length, "every stop declares route, target and title on one line");
for (const part of [/\n    text: "/g, /absent: [A-Z"]/g]) assert.equal([...tour.matchAll(part)].length, stops.length, `every stop has ${part}`);

// ---- every target is known: in the registry or rendered as data-jarvis-target ----
const registry = new Set([...targets.matchAll(/^\s*"((?:rail|design)\.[a-z0-9.-]+)":/gm)].map((m) => m[1]));
const sources = [];
const walk = (dir) => { for (const name of fs.readdirSync(dir)) { const p = path.join(dir, name); if (fs.statSync(p).isDirectory()) walk(p); else if (/\.tsx?$/.test(name)) sources.push(fs.readFileSync(p, "utf8")); } };
walk(path.join(root, "src"));
const rendered = (id) => sources.some((text) => text.includes(`data-jarvis-target="${id}"`));
for (const { target } of stops) assert.ok(registry.has(target) || rendered(target), `${target} is a known target`);
for (const id of ["rail.memory", "rail.development", "rail.coding", "rail.settings", "design.panel.feature-inspector", "design.panel.cfd-field-3d"]) assert.ok(registry.has(id), `${id} is registered`);
assert.match(read("src/pages/DesignFeatureInspector.tsx"), /designTarget\("design\.panel\.feature-inspector"\)/);
assert.match(read("src/pages/CfdField3dPanel.tsx"), /designTarget\("design\.panel\.cfd-field-3d"\)/);

// ---- no mutating control among the stops, and the runner can only select tree rows ----
const MUTATING = /attach|rebuild|save|delete|remove|publish|submit|\.add\b|run-start|\.start\b/;
for (const { target } of stops) assert.ok(!MUTATING.test(target), `${target} is not a mutating control`);
assert.ok(!stops.some((s) => s.target === "design.node.attach-model"), "Attach is never a stop");
assert.match(tour, /TOUR_CLICKABLE = \/\^design\\\.tree\\\.\(component\|node\)\\\./);
assert.match(runner, /TOUR_CLICKABLE\.test\(id\)/);
for (const forbidden of [/fetch\(/, /XMLHttpRequest/, /\bmethod:/, /from "\.\.\/\.\.\/api/, /localStorage/, /sessionStorage/]) assert.ok(!forbidden.test(runner) && !forbidden.test(tour), `tour code has no ${forbidden}`);
assert.equal([...runner.matchAll(/\.click\(\)/g)].length, 3, "the runner clicks only: a tree row, the case-study offer, and the visible Back button");
assert.match(runner, /"design\.example-open"/);
assert.match(runner, /shell-project__back/);

// ---- visible entry, controls, keyboard ----
const bubble = read("src/components/bubu/BubuBubble.tsx");
assert.match(bubble, />Guided tour<\/button>/);
assert.match(bubble, /if \(tour\) tour\.onExit\(\); else close\(\);/);
assert.match(runner, />Back<\/button>/);
assert.match(runner, />Next<\/button>/);
assert.match(runner, />Exit<\/button>/);
assert.match(runner, /tourPosition\(state\.index\)/);
assert.match(runner, /event\.key !== "Escape"/);
assert.match(runner, /Not available here|notAvailableLine/);
assert.match(tour, /`Not available here: \$\{why\}\.`/);
assert.match(tour, /`\$\{index \+ 1\} \/ \$\{TOUR_COUNT\}`/);
assert.match(read("src/components/bubu/useBubuIntegration.tsx"), /useBubuIntegration\(navigate: Navigate\)/);
assert.match(read("src/App.tsx"), /useBubuIntegration\(navigate\)/);
assert.match(read("src/components/Layout.tsx"), /if \(touring\) setBubbleOpen\(true\)/);
assert.match(read("src/components/Layout.tsx"), /tour=\{bubu\?\.tour\} onStartTour=\{bubu\?\.onStartTour\}/);

// ---- build chain ----
assert.match(pkg, /"test:242": "node tests\/242_bubu_guided_tour\.test\.mjs"/);
assert.match(pkg, /npm run test:235 && npm run test:235b && npm run test:242 &&/);
console.log("242 bubu guided tour: ok");
