import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const read = (relative) => fs.readFileSync(path.join(root, relative), "utf8");
const assert = (condition, message) => {
  if (!condition) throw new Error(`100g conformance failure: ${message}`);
};

const shellCss = read("src/styles/final-fusion-shell-overrides.css");
const workspaceCss = read("src/styles/final-workspace-header.css");
const repairCss = read("src/styles/100g-ui-repair.css");
const main = read("src/main.tsx");
const routes = read("src/app/routes.ts");
const processStage = read("src/stages/ProcessStage.tsx");
const bluecadStage = read("src/stages/ModelStage.tsx");
const settingsSurface = read("src/components/fusion/FinalSettingsSurface.tsx");
const pkg = JSON.parse(read("package.json"));

// Finding A: final route fragments must stack a compact workspace header above either truthful production surface root.
assert(shellCss.includes("flex-direction: column;"), "final shell main must stack header and work surface vertically");
assert(!workspaceCss.includes("min-height: 104px"), "workspace header must not reserve the legacy 104px dead band");
assert(!workspaceCss.includes("calc(100% - 104px)"), "work surface must not use the legacy fixed-height subtraction");
assert(workspaceCss.includes("grid-template-columns: minmax(0, 1fr) auto;"), "desktop workspace header must place metadata and peer tabs in one horizontal band");
assert(workspaceCss.includes('"eyebrow tabs"') && workspaceCss.includes('"title tabs"') && !workspaceCss.includes('"description tabs"'), "workspace header must bind peer tabs beside compact title metadata without an unused description row");
assert(workspaceCss.includes(".application-shell--final .shell-main > .final-fusion__workspace-head") && workspaceCss.includes("flex: 0 0 auto;"), "workspace header must override the shared shell flex:1 rule");
assert(workspaceCss.includes(".final-fusion__workspace-head + .final-fusion,") && workspaceCss.includes(".final-fusion__workspace-head + .final-fusion__workbench"), "workspace repair must bind both unavailable wrapper and direct READ workbench roots");
assert(workspaceCss.includes("flex: 1 1 auto;") && workspaceCss.includes("height: auto;"), "work surface must consume the space immediately below the compact header");
assert(workspaceCss.includes(".final-fusion__workspace-head + .final-fusion > .final-fusion__surface-head") && workspaceCss.includes(".final-fusion__workspace-head + .final-fusion__workbench > .final-fusion__surface-head"), "duplicate nested headings must remain suppressed on both production roots");

// Finding B: all peer navigation uses neutral inactive and soft-accent active states.
for (const token of ["--color-border-default", "--color-bg-surface-raised", "--color-accent-foreground", "--color-accent-surface", "--color-text-primary"]) {
  assert(workspaceCss.includes(token), `Memory/Development/Coding peer tabs must include shared token ${token}`);
  assert(repairCss.includes(token), `Design/Settings peer tabs must include shared token ${token}`);
}
assert(repairCss.includes(".application-shell--final .design-stage__tabs button"), "Design peer tabs must receive final-route navigation overrides");
assert(repairCss.includes('.design-stage__tabs button[aria-current="page"]'), "Design active peer tab must use route state, not primary-action styling");
assert(repairCss.includes(".application-shell--final .final-settings__tab.is-active"), "Settings active peer tab must share the soft-accent language");
assert(main.includes('import "./styles/100g-ui-repair.css";'), "100g repair stylesheet must be loaded by production entrypoint");

// Preserve exact canonical routes and intentional Design workbench structure.
for (const route of [
  "/memory/project-basis",
  "/memory/models",
  "/memory/literature",
  "/development/roadmap/timeline",
  "/development/roadmap/calendar",
  "/coding/repository",
  "/coding/runtime",
  "/design/process",
  "/design/bluecad",
  "/settings/appearance",
  "/settings/ai",
  "/settings/system",
]) {
  assert(routes.includes(`path: "${route}"`), `canonical route must remain present: ${route}`);
}
assert(processStage.includes("dwsim-palette") && read("src/stages/ProcessStage.css").includes(".dwsim-palette"), "DWSIM operational palette must remain structurally distinct from the workspace header repair");
// Spec 213 kept the Design surfaces in one rail position; spec 229 makes that one collapsed Design tools group.
const rail = read("src/components/shell/Rail.tsx");
assert(rail.includes('aria-label="Design tools"') && rail.includes("DESIGN_TOOL_ITEMS.map"), "older Design surfaces must share one rail group");
assert(!processStage.includes("design-stage__tabs") && !bluecadStage.includes("design-stage__tabs"), "Design headers must not duplicate the rail switcher");
assert(settingsSurface.includes("final-settings__tabs"), "Settings must preserve its peer selector semantics");

// Regression gate must be part of the normal build.
assert(pkg.scripts?.["test:100g"] === "node tests/100g-final-operator-ui-repair.mjs", "package must expose test:100g");
assert(pkg.scripts?.build?.includes("npm run test:100g"), "npm run build must execute test:100g");

console.log("100g final operator UI repair conformance: PASS");
