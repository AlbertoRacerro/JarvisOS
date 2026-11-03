import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relative) => fs.readFileSync(path.join(root, relative), "utf8");
const app = read("src/App.tsx");
const settings = read("src/components/fusion/FinalSettingsSurface.tsx");
const workspaceHeader = read("src/components/fusion/FinalWorkspaceHeader.tsx");
const workspaceHeaderCss = read("src/styles/final-workspace-header.css");
const settingsCss = read("src/styles/final-settings.css");
const designCss = read("src/styles/final-fusion-canonical-overrides.css");
const bluecadCss = read("src/styles/bluecad-final.css");
const modelStage = read("src/stages/ModelStage.tsx");
const bluecadWorkbench = read("src/components/bluecad/BluecadWorkbench.tsx");
const bluecadViewer = read("src/components/BluecadGlbViewer.tsx");
const apiClient = read("src/api/client.ts");
const failures = [];
const check = (condition, message) => { if (!condition) failures.push(message); };
const includesAll = (text, fragments, message) => { for (const fragment of fragments) check(text.includes(fragment), `${message}: missing ${fragment}`); };

includesAll(app, [
  '<FinalSettingsSurface section="appearance"',
  '<FinalSettingsSurface section="ai"',
  '<FinalSettingsSurface section="system"'
], "Settings canonical routes are not section-specific");
includesAll(app, [
  '<FinalWorkspaceHeader group="memory" active="project-basis"',
  '<FinalWorkspaceHeader group="memory" active="models"',
  '<FinalWorkspaceHeader group="memory" active="literature"',
  '<FinalWorkspaceHeader group="development" active="roadmap"',
  '<FinalWorkspaceHeader group="coding" active="repository"',
  '<FinalWorkspaceHeader group="coding" active="runtime"'
], "Canonical Memory/Development/Coding workspace headings are not routed");
includesAll(workspaceHeader, [
  'title: "Project knowledge"', '"Project Basis", "/memory/project-basis"', '"Models", "/memory/models"', '"Literature", "/memory/literature"',
  'title: "Development"', '"Roadmap", "/development/roadmap/timeline"',
  'title: "Coding"', '"Repository", "/coding/repository"', '"Runtime", "/coding/runtime"',
  'aria-current={active === id ? "page" : undefined}'
], "Canonical workspace peer-tab language drifted");
includesAll(workspaceHeaderCss, [
  ".final-fusion__workspace-head",
  ".final-fusion__peer-tabs",
  ".final-fusion__peer-tabs a.is-active",
  ".final-fusion__workspace-head + .final-fusion__workbench > .final-fusion__surface-head"
], "Workspace heading composition or stale nested-heading suppression missing");
includesAll(settings, [
  'label: "Appearance"', 'href: "/settings/appearance"',
  'label: "AI"', 'href: "/settings/ai"',
  'label: "System"', 'href: "/settings/system"',
  'aria-label="Settings sections"', 'aria-current={tab.id === section ? "page" : undefined}'
], "Settings visible peer IA drifted");
includesAll(settingsCss, [
  ".final-settings__tabs", ".final-settings__tab.is-active",
  ".final-settings--appearance .settings-grid > .settings-card--visual",
  ".final-settings--ai .settings-grid > .settings-card:nth-child(2)",
  ".final-settings--ai .settings-grid > .settings-card:nth-child(3)",
  ".final-settings--ai .settings-grid > .settings-card:nth-child(4)",
  ".final-settings--ai .settings-grid > .settings-card:nth-child(5)",
  ".final-settings--system .settings-grid > .settings-card:nth-child(6)"
], "Settings section filtering drifted");
includesAll(designCss, [
  ".process-stage__palette-grid",
  "grid-template-columns: repeat(2, minmax(0, 1fr))",
  ".application-shell--final .shell-navigator",
  ".application-shell--final .shell-sidecar",
  "width: 240px",
  "width: var(--jarvis-width, 300px)"
], "Canonical Process/BLUECAD workstation composition missing");
includesAll(bluecadCss, [
  ".application-shell--final .bluecad-workbench__viewport",
  ".application-shell--final .bluecad-workbench__empty-viewer",
  ".bluecad-viewer__views button",
  "background: transparent !important",
  ".bluecad-export__menu--flipped",
  "left: 0;"
], "BLUECAD-owned final composition or export-menu alignment missing");
includesAll(modelStage, ["<BluecadWorkbench"], "BLUECAD stage is disconnected from its workbench");
check(!modelStage.includes("bluecad-final-stage__toolbar"), "BLUECAD retains the disabled future-authoring toolbar");
includesAll(bluecadWorkbench, [
  'Create geometry', 'No geometry yet', 'Template part · no AI', 'AI brief',
  'Write a brief', 'export.stl', 'export.step', 'Export ▾',
  'Paid AI is off'
], "BLUECAD creation and print-handoff routes drifted");
includesAll(bluecadViewer, [
  'new OrbitControls', 'controls.enableDamping = true', 'VIEW_BUTTONS', '"fit"', '"front"', '"top"', '"side"'
], "BLUECAD viewer lost camera interaction or standard views");
includesAll(apiClient, ["createBluecadTemplateCandidate", "getBluecadGenerationAvailability", "from-template"], "BLUECAD API routes are missing");
check(!settingsCss.includes("linear-gradient(rgba(75, 91, 78") && !settingsCss.includes("background-size: 32px 32px"), "BLUECAD empty state retains the fake graph-paper viewport");
check(!settings.includes("fetch(") && !settings.includes("localStorage") && !settings.includes("sessionStorage"), "Settings wrapper gained data/authority state");
check(!workspaceHeader.includes("fetch(") && !workspaceHeader.includes("localStorage") && !workspaceHeader.includes("sessionStorage"), "Workspace header gained data/authority state");

if (failures.length) {
  console.error("100f visual conformance acceptance failed:");
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}
console.log("100f visual conformance acceptance: PASS");
