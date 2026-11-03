import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const assert = (condition, message) => { if (!condition) throw new Error(`058d acceptance failed: ${message}`); };
const includesAll = (text, fragments, message) => { for (const fragment of fragments) assert(text.includes(fragment), `${message}: missing ${fragment}`); };

const routes = read("src/app/routes.ts");
const registry = read("src/stages/registry.ts");
const processStage = read("src/stages/ProcessStage.tsx");
const app = read("src/App.tsx");
const editorApi = read("src/api/dwsimEditor.ts");

// 100f supersedes the visible 058d Design peer IA while preserving Process and deterministic historical aliases.
includesAll(routes, [
  'id: "design-process", path: "/design/process"',
  'id: "design-bluecad", path: "/design/bluecad"',
  '"/design/lineage": "/memory/models"',
  '"/design/flowsheet": "/memory/models"',
  'pathOnly.replace(/\\/+$/g, "")'
], "100f route migration contract");
const designPeerBlock = routes.match(/design: \[([\s\S]*?)\n  \],\n  memory:/)?.[1] ?? "";
const designLabels = [...designPeerBlock.matchAll(/label: "([^"]+)"/g)].map((match) => match[1]);
// Spec 229 puts New Design first: it is the single Design entry; the older surfaces stay as peers.
assert(JSON.stringify(designLabels) === JSON.stringify(["Design", "Process", "BlueCad", "Studio"]), `100f Design peers drifted: ${designLabels.join(" -> ")}`);

includesAll(registry, [
  'import ProcessStage from "./ProcessStage"',
  'process: { kind: "process", label: "Process", render: ProcessStage }'
], "stage registry contract");
assert(!/\bflowsheet\s*:/.test(registry), "registry still exposes a flowsheet stage key");

// 100f adds presentation-only Phosphor icons before the type import; keep the authority boundary rather than enforcing import order.
assert(processStage.includes('import type { PrimaryStageProps } from "./registry";'), "ProcessStage lost its type-only stage contract");
includesAll(processStage, ["runDwsimCommand", "expected_revision", "cause.status === 409", "setProjection(result.projection)", "unsupported_commands"], "revisioned DWSIM operator boundary");
includesAll(editorApi, ["/process/dwsim", "EditorCommand", "DwsimEditorError", "restoreDwsimRevision"], "typed DWSIM API boundary");
assert(!/\b(?:localStorage|sessionStorage)\b/.test(processStage), "Process editor persists graph state locally");
assert(!/from\s+["'][^"']*(?:provider|runner|filesystem|ollama)/i.test(processStage + editorApi), "Process editor imports direct execution authority");
assert((processStage.match(/\bdisabled\b/g) ?? []).length >= 8, "Process controls lack availability and single-flight guards");
assert(processStage.includes("Changed elsewhere. The stale edit was discarded"), "Process editor does not discard stale edits visibly");
assert(processStage.includes("projection?.connections") && processStage.includes("geometryIds.has(edge.source_native_id)"), "Canvas does not validate native connection endpoints");

const routeReset = app.match(/useEffect\(\(\) => \{([\s\S]*?)\}, \[route\.id\]\);/)?.[1] ?? "";
includesAll(routeReset, ["setSelection(null)", "setShellRegions({})", "setShellRegionRequest(null)"], "route-id stale-context reset");
includesAll(app, [
  'import AnalyticsDockContent from "./components/analytics/AnalyticsDockContent"',
  'route.id === "design-process"',
  "<AnalyticsDockContent workspaceId={workspaceId} />"
], "Process Analysis Dock reuse");
assert(!/ProcessAnalytics|ProcessKpi|process-specific analytics/i.test(app + processStage), "process-specific analytics authority was introduced");

console.log("058d deterministic frontend acceptance passed");
