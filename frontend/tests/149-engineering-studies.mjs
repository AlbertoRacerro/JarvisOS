import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relative) => fs.readFileSync(path.join(root, relative), "utf8");
const stage = read("src/stages/EngineeringStudiesStage.tsx");
const api = read("src/api/engineeringStudies.ts");
const app = read("src/App.tsx");
const routes = read("src/app/routes.ts");
const contracts = read("src/api/generated/engineering.ts");
const failures = [];
const check = (condition, message) => { if (!condition) failures.push(message); };

check(routes.includes('id: "design-studies", path: "/design/studies", title: "Studies", primaryNav: "design"'), "149 studies route is not registered under Design");
check(app.includes('route.id === "design-studies"') && stage.includes("Run study") && stage.includes("Run inspection"), "149 studies stage is not mounted or incomplete");
for (const endpoint of ["/evaluators", "/capabilities", "/studies", "/runs", "/envelope", "/escalations"]) check(api.includes(endpoint), `engineering API client is missing ${endpoint}`);
check(api.includes("Stale engineering response") && api.includes("validRun") && api.includes("validEvaluators"), "stale engineering payloads are not rejected defensively");
check(stage.includes('item.state === "available"') && stage.includes('disabled={item.state !== "available"}') && stage.includes("reason_code"), "unavailable evaluators are not disabled with their reason visible");
check(stage.includes('"Unqualified"') && stage.includes('"Qualification unknown"') && stage.includes("QUALIFICATIONS.has"), "unknown or unqualified status can be upgraded during rendering");
check(api.includes("/workspaces/${encodeURIComponent(workspaceId)}/engineering") && !/fetch\([^\n]*(?:ollama|provider|filesystem|runner)/i.test(api), "engineering API client escaped the workspace engineering route owner");
check(!/fetch\([^\n]*(?:ollama|provider|filesystem|runner)|https?:\/\/(?:api\.)?(?:openai|anthropic|ollama)/i.test(stage), "UI contains direct provider, Ollama, filesystem, or runner authority");
check(contracts.includes("export type EvaluatorRead") && contracts.includes("export type CapabilityRead"), "generated read contracts are no longer consumed");
if (failures.length) { console.error(failures.join("\n")); process.exitCode = 1; }
else console.log("149 engineering studies acceptance passed");
