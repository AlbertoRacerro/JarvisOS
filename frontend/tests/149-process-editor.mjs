import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const stage = read("src/stages/ProcessStage.tsx");
const api = read("src/api/dwsimEditor.ts");
const contracts = read("src/api/generated/dwsimEditor.ts");
const fail = (message) => {
  throw new Error(`149 process editor acceptance failed: ${message}`);
};
const has = (text, value, message) => {
  if (!text.includes(value)) fail(message);
};

for (const name of [
  "EditorCommand",
  "EditorProjectionRead",
  "CommandResult",
  "RevisionRead",
])
  has(api, name, `API must use generated ${name}`);
for (const path of [
  "/process/dwsim",
  "/cases/import",
  "/commands",
  "/restore",
  "/revisions/",
])
  has(api, path, `typed API lacks ${path}`);
has(api, "expected_revision", "command revision contract missing");
has(
  stage,
  "setProjection(result.projection)",
  "server command projection must replace the rendered state",
);
has(stage, "cause.status === 409", "stale revision path must be detected");
has(
  stage,
  "Changed elsewhere. The stale edit was discarded",
  "stale command must be discarded and explained",
);
has(
  stage,
  "geometryIds.has(edge.source_native_id)",
  "connection sources must resolve against projected object IDs",
);
has(
  stage,
  "geometryIds.has(edge.target_native_id)",
  "connection targets must resolve against projected object IDs",
);
has(
  stage,
  "invalidCount > 0",
  "invalid connection records must produce a visible warning",
);
has(stage, "Math.hypot(", "a click must not be treated as a drag gesture");
has(
  stage,
  "setSelectedId(id);\n                        setRename(object.tag ?? \"\");\n                        setDrag",
  "pointer selection must refresh the inspector rename draft",
);
has(
  stage,
  "runtimeUnavailable",
  "runtime failure must disable mutating controls",
);
has(
  stage,
  "Object.entries(projection?.unsupported_commands",
  "unsupported command reasons must be rendered from projection",
);
has(
  stage,
  "projection?.editable_commands",
  "new server-supported command kinds must appear from projection",
);
has(
  stage,
  "No process topology is loaded",
  "unavailable state must not invent a graph",
);
has(
  stage,
  "const input = e.currentTarget;",
  "async import cleanup must retain the file input element",
);
if (/\b(?:localStorage|sessionStorage)\b/.test(stage))
  fail("graph state must not be persisted locally");
if (/fetch\s*\(/.test(stage))
  fail("ProcessStage must use the typed process API client");
if (!contracts.includes('availability: "available"'))
  fail("generated projection contract drifted");
console.log("149 process editor deterministic acceptance: PASS");
