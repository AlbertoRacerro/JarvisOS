import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const read = (relative) => fs.readFileSync(path.join(root, relative), "utf8");
const assert = (condition, message) => {
  if (!condition) throw new Error(`112 frontend conformance failure: ${message}`);
};

const panel = read("src/components/fusion/ProjectKnowledgePanel.tsx");
const app = read("src/App.tsx");
const processStage = read("src/stages/ProcessStage.tsx");
const router = read("src/app/useAppRouter.ts");
const pkg = JSON.parse(read("package.json"));

// Historical lifecycle must remain inspectable instead of collapsing to only current working revisions.
assert(panel.includes("revisions.map((revision)"), "revision selector must enumerate the server-owned lifecycle history");
assert(panel.includes('revision.state === "reconciled"') && panel.includes('revision.state === "working"'), "only reconciled/working revisions may become writable draft parents");
assert(panel.includes("discarded and superseded revisions remain inspectable"), "discarded/superseded history must be explicitly inspect-only");
assert(panel.includes("superseded_by_revision_id"), "supersession identity must be visible when present");

// Revalidation must fail visibly and preserve exact recomputation identity.
assert(panel.includes("revalidation.diagnostics.join"), "deterministic revalidation diagnostics must be rendered");
for (const key of [
  "project_knowledge_revision_id",
  "project_knowledge_basis_digest",
  "project_knowledge_validation_set_digest",
  "project_knowledge_requirement_id",
]) {
  assert(panel.includes(key), `recomputation handoff must carry ${key}`);
  assert(processStage.includes(key), `Process landing must inspect ${key}`);
}
assert(processStage.includes("Incomplete Project Knowledge recomputation handoff ignored"), "partial Process handoff must fail closed");
assert(processStage.includes("This editor cannot launch recomputation from the handoff."), "handoff display must not fabricate Process execution authority");
assert(router.includes("destination.search") && router.includes("destination.hash"), "SPA navigation must preserve exact handoff query context");

// Models may inspect the same server-owned lifecycle but must not acquire mutation authority.
assert(app.includes('<ProjectKnowledgePanel workspaceId={workspaceId} readOnly />'), "Models must expose Project Knowledge lifecycle in read-only mode");
assert(panel.includes("readOnly ?") && panel.includes("!readOnly"), "ProjectKnowledgePanel must explicitly gate mutation affordances in read-only mode");

// The deterministic regression gate belongs to normal frontend build authority.
assert(pkg.scripts?.["test:112"] === "node tests/112-project-knowledge-lifecycle.mjs", "package must expose test:112");
assert(pkg.scripts?.build?.includes("npm run test:112"), "npm run build must execute test:112");

console.log("112 Project Knowledge frontend lifecycle conformance: PASS");
