import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relative) => fs.readFileSync(path.join(root, relative), "utf8");
const routes = read("src/app/routes.ts");
const app = read("src/App.tsx");
const layout = read("src/components/Layout.tsx");
const main = read("src/main.tsx");
const readFusion = read("src/components/fusion/FinalOperatorReadSurface.tsx");
const readApi = read("src/api/client.ts");
const codingWorkbench = read("src/pages/CodingWorkbench.tsx");
const codingApi = read("src/api/coding.ts");
const generatedModeling = read("src/api/generated/modeling.ts");
const fusionCss = read("src/styles/final-fusion.css");
const shellOverlay = read("src/styles/final-fusion-shell-overrides.css");
const canonicalOverlay = read("src/styles/final-fusion-canonical-overrides.css");
const processStage = read("src/stages/ProcessStage.tsx");
const processCss = read("src/stages/ProcessStage.css");
const contextNav = read("src/components/shell/ContextualNavigator.tsx");
const pkg = JSON.parse(read("package.json"));
const failures = [];
const check = (condition, message) => { if (!condition) failures.push(message); };
const includesAll = (text, fragments, message) => { for (const fragment of fragments) check(text.includes(fragment), `${message}: missing ${fragment}`); };

const canonicalRoutes = [
  "/design/process", "/design/bluecad", "/memory/project-basis", "/memory/models", "/memory/literature",
  "/development/roadmap/timeline", "/development/roadmap/calendar",
  "/coding/repository", "/coding/runtime", "/settings/appearance", "/settings/ai", "/settings/system"
];
for (const route of canonicalRoutes) check(routes.includes(`path: "${route}"`), `canonical route missing: ${route}`);

const primaryBlock = routes.match(/export const PRIMARY_NAV_ITEMS = \[([\s\S]*?)\] as const/)?.[1] ?? "";
const primaryLabels = [...primaryBlock.matchAll(/label: "([^"]+)"/g)].map((match) => match[1]);
check(JSON.stringify(primaryLabels) === JSON.stringify(["Design", "Memory", "Development", "Coding", "Settings"]), `primary IA drifted: ${primaryLabels.join(" | ")}`);
check(!primaryBlock.includes("Home") && !primaryBlock.includes("Runs") && !primaryBlock.includes("Engineering Data") && !primaryBlock.includes("Review"), "legacy peer leaked into primary rail");

includesAll(routes, [
  // Spec 229: the ordinary entry is New Design, the single Design entry.
  '"/": "/design/project"',
  '"/home": "/design/project"',
  '"/design/model": "/memory/models"',
  '"/design/results": "/memory/models"',
  '"/design/lineage": "/memory/models"',
  '"/design/flowsheet": "/memory/models"',
  '"/settings": "/settings/appearance"'
], "legacy redirect contract");

includesAll(routes, [
  'design: [', 'label: "Process"', 'label: "BlueCad"',
  'memory: [', 'label: "Project Basis"', 'label: "Models"', 'label: "Literature"',
  'development: [', 'label: "Roadmap"',
  'coding: [', 'label: "Repository"', 'label: "Runtime"',
  'settings: [', 'label: "Appearance"', 'label: "AI"', 'label: "System"',
  'label: "Timeline"', 'label: "Calendar"'
], "peer IA contract");
check(!routes.includes('label: "Board"'), "Board reintroduced as Roadmap peer");
check(contextNav.includes("ROADMAP_STAGE_ITEMS"), "Roadmap Timeline/Calendar secondary navigation missing");

check(app.includes('route.id === "design-process" || route.id === "design-bluecad"'), "Process/BLUECAD do not reuse the existing stage shell");
// BUBU-LAUNCHER-1 makes the existing conversation explicitly reachable from Settings too.
check(app.includes('sidecar: route.primaryNav === "coding" || (IN_PAGE_JARVIS_ROUTES.has(route.id) && workspaceLoadState === "ready") ? undefined : jarvisSidecar'), "Coding and ready in-page Jarvis routes must not duplicate their conversation; other pages offer the shell launcher");
includesAll(app, [
  'FinalOperatorReadSurface kind="project-basis"', 'ModelDossier workspaceId=',
  'kind="literature"',
  '<DevelopmentRoadmap mode="timeline" workspaceId={workspaceId} onWorkspaceChange={setWorkspaceId} jarvis={jarvisSidecar} />',
  '<DevelopmentRoadmap mode="calendar" workspaceId={workspaceId} onWorkspaceChange={setWorkspaceId} jarvis={jarvisSidecar} />',
  '<CodingWorkbench mode="repository" workspaceId={workspaceId} jarvis={jarvisSidecar} />',
  '<CodingWorkbench mode="runtime" workspaceId={workspaceId} jarvis={jarvisSidecar} />'
], "missing final production surface composition");
check(!app.includes("No server-owned roadmap item store") && !app.includes("No server-owned time-allocation calendar"), "Development still presents obsolete missing-authority states after 116 owner landed");
check(codingWorkbench.includes("readSafeGithubUrl(repository, requestSha, path)") && codingWorkbench.includes("href={safeUrl}"), "Repository frontend/GitHub truth boundary is not explicit");
check(!/api\.github\.com|github\.com\/api|Authorization|GITHUB_TOKEN/i.test(codingApi), "Coding browser API client crossed provider/credential boundary");
check(!/iframe|srcDoc/.test(app), "static HTML embedding is forbidden");
check(!/localStorage|sessionStorage|fetch\(|axios|github\.com\/api/i.test(app), "final surface shell gained private truth/API authority");

includesAll(readFusion, [
  "Exact model-version inventory is not exposed by the current read owner.",
  "Specification record; not an exact model version",
  "Not projected from workspace-level records because exact model/version binding cannot be proven.",
  "LOCAL · Unknown SHA",
  "REMOTE · Unknown",
  "does not prove an executed Git SHA",
  "The frontend does not call GitHub directly or infer alignment.",
  "Safe update and terminal EXECUTE authority are not present in 100f."
], "truthful READ/Unknown boundary missing");
check(readFusion.includes("<ProjectKnowledgePanel") && readFusion.includes("aria-expanded={open.has(row.id)}"), "Project Basis must expose real revisions and operable record disclosure");
check(!/cd951bae|86cdedde|working tree clean|remote current|PASS|Aligned/i.test(readFusion), "fixture repository/runtime success identity leaked into truthful read surface");
check(!/localStorage|sessionStorage|github\.com\/api|api\.github|child_process|powershell|cmd\.exe/i.test(readFusion), "read surface crossed frontend authority boundary");
check(readFusion.includes('from "../../api/client"'), "final operator READ surface no longer uses the shared API client");
check(!readFusion.includes("finalOperatorReads"), "stale finalOperatorReads import remains in the final operator READ surface");

includesAll(readApi, [
  'getJson<Workspace[]>("/workspaces")',
  "/model-specs",
  "/requirements",
  "/parameters",
  "/decisions",
  "getSystemInfo",
  "export type Requirement = {",
  'import type { ParameterRead } from "./generated/modeling";',
  "export type Parameter = ParameterRead;"
], "accepted existing backend READ binding missing");
includesAll(generatedModeling, [
  "export type ParameterRead = {",
  'value_status: "candidate" | "literature" | "measured" | "validated" | "accepted";',
  'lifecycle_state: "active" | "inactive" | "superseded" | "archived" | "deleted";'
], "generated Parameter READ contract missing");
check(!/github\.com|api\.github|localStorage|sessionStorage|child_process|powershell|cmd\.exe/i.test(readApi), "shared final read client crossed frontend authority boundary");

includesAll(layout, [
  "const finalOperatorRoute = route.primaryNav !== undefined",
  'application-shell--final',
  '!finalOperatorRoute && <TopBar',
  'setNavigatorOpen(route.id === "design-bluecad")'
], "canonical final routes must use rail-only shared shell and persistent BLUECAD navigator");
check(main.includes('final-fusion-shell-overrides.css'), "final shared-shell canonical overlay is not loaded");
includesAll(shellOverlay, [
  "grid-template-columns: var(--shell-rail-width) minmax(0, 1fr)", "--jarvis-width: clamp(300px, 24vw, 420px)",
  ".application-shell--final .shell-topbar",
  "display: none",
  'font-family: "Inter Display", "Inter"',
  "var(--color-bg-canvas)",
  "var(--color-bg-shell)",
  "height: 100vh",
  ".application-shell--final .shell-main > *"
], "canonical rail/shell geometry or visual language missing");

includesAll(processStage, [
  'aria-label="DWSIM process canvas', 'runDwsimCommand', 'expected_revision', 'Changed elsewhere. The stale edit was discarded',
  'unsupported_commands', 'Read-back evidence', 'Solve', 'Revision history', 'No process topology is loaded.'
], "revisioned Process editor boundary or truthful empty state missing");
check(!/fetch\(|axios|localStorage|sessionStorage/i.test(processStage), "Process editor gained direct network or local persistence authority");
includesAll(processCss, [
  ".dwsim-workbench", ".dwsim-palette", ".dwsim-canvas", ".dwsim-inspector",
  "grid-template-columns: 180px minmax(300px, 1fr) 230px", "background-size: 18px 18px"
], "DWSIM Process workstation composition missing");

includesAll(fusionCss, [
  "final-fusion__workbench--memory", "grid-template-columns: 255px minmax(0, 1fr) var(--jarvis-width, 315px)",
  "final-fusion__workbench--models", "grid-template-columns: 230px minmax(0, 1fr) var(--jarvis-width, 310px)",
  "final-fusion__workbench--development", "grid-template-columns: minmax(0, 1fr) var(--jarvis-width, 350px)",
  "final-fusion__preview-skeleton", "grid-template-columns: minmax(0,1fr) 218px",
  "final-fusion__execution-grid", "final-fusion__week-head", "final-fusion__repo-inspector-body", "final-fusion__compare"
], "canonical reference panel geometry missing");
check(!fusionCss.includes("border-radius: 16px") && !fusionCss.includes("border-radius: 20px"), "large-radius dashboard styling leaked into canonical fusion CSS");

const test100f = pkg.scripts?.["test:100f"] ?? "";
check(test100f.includes("node tests/100f-final-operator-frontend-fusion.mjs") && test100f.includes("node tests/100f-visual-conformance.mjs"), "test:100f is not wired");
check((pkg.scripts?.build ?? "").includes("npm run test:100f"), "build does not execute test:100f");

if (failures.length) {
  console.error("FINAL-OPERATOR-FRONTEND-FUSION-1 deterministic acceptance failed:");
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}
console.log("FINAL-OPERATOR-FRONTEND-FUSION-1 deterministic acceptance: PASS");
