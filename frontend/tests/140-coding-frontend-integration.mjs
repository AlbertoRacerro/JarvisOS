import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relative) => fs.readFileSync(path.join(root, relative), "utf8");
const app = read("src/App.tsx");
const workbench = read("src/pages/CodingWorkbench.tsx");
const api = read("src/api/coding.ts");
const failures = [];
const check = (condition, message) => { if (!condition) failures.push(message); };
const includesAll = (text, fragments, message) => {
  for (const fragment of fragments) check(text.includes(fragment), `${message}: missing ${fragment}`);
};

includesAll(app, [
  'import CodingWorkbench from "./pages/CodingWorkbench"',
  '<CodingWorkbench mode="repository" workspaceId={workspaceId} jarvis={jarvisSidecar} />',
  '<CodingWorkbench mode="runtime" workspaceId={workspaceId} jarvis={jarvisSidecar} />'
], "Coding routes are not wired to the accepted 140 surface");

includesAll(api, [
  "/api/coding/repository/ref?",
  "/api/coding/repository/tree?",
  "/api/coding/repository/file?",
  "/api/coding/repository/search?",
  "/api/coding/repository/pull-request?",
  "/api/coding/repository/checks?",
  "/api/coding/repository/reviews?",
  "/api/coding/repository/url?",
  "/api/coding/runtime-truth?",
  "/api/coding/control-room?",
  "/api/coding/pipeline-state?",
  "/api/coding/actions/inspect",
  "/api/coding/actions/context-preview",
  "/api/coding/actions/suggest-modification"
], "140 API client is missing an accepted backend owner projection");
check(!/api\.github\.com|github\.com\/api|Authorization|GITHUB_TOKEN|ghp_/i.test(api), "browser API client gained provider/credential authority");

includesAll(workbench, [
  "resolvedSha",
  "readRepositoryTree",
  "readRepositoryFile",
  "searchRepository",
  "readPullRequest",
  "readChecks",
  "readReviews",
  "readSafeGithubUrl",
  "Open on GitHub",
  "Partial evidence",
  "Truncated evidence is not presented as complete.",
  "treePath",
  "openDirectory",
  "Repository breadcrumb",
  "Parent directory",
  "fileReadGeneration",
  "prEvidenceGeneration",
  "proposalGeneration",
  "contextPreviewGeneration",
  "inspectGeneration",
  "refreshGeneration",
  "treeReadGeneration",
  "searchGeneration",
  "requestGeneration",
  "requestTreeGeneration",
  "requestSha",
  "requestLiteral",
  "fileReadGeneration.current !== requestGeneration",
  "prEvidenceGeneration.current !== requestGeneration",
  "proposalGeneration.current !== requestGeneration",
  "contextPreviewGeneration.current !== requestGeneration",
  "inspectGeneration.current !== requestGeneration",
  "refreshGeneration.current !== requestGeneration",
  "treeReadGeneration.current !== requestGeneration",
  "treeReadGeneration.current !== requestTreeGeneration",
  "searchGeneration.current !== requestGeneration",
  "fileReadGeneration.current += 1",
  "prEvidenceGeneration.current += 1",
  "proposalGeneration.current += 1",
  "contextPreviewGeneration.current += 1",
  "inspectGeneration.current += 1",
  "searchGeneration.current += 1",
  "canonicalPrNumber",
  "canonicalSpecId",
  "repositoryErrors",
  "setRepositoryError",
  "Promise.allSettled",
  "Checks evidence refused / unavailable",
  "Reviews evidence refused / unavailable",
  "File preview refused / unavailable",
  "Repository search refused / unavailable",
  "PR evidence refused / unavailable",
  "Context insertion refused / unavailable",
  "Proposal refused / unavailable",
  "readControlRoom",
  'relation = runtime?.alignment ?? "unknown"',
  "Process startup identity",
  "Root identity",
  "Observed at",
  "Provenance",
  "Failure identity",
  "The browser performs no SHA ancestry or cleanliness inference.",
  "runtimeError",
  "pipelineError",
  "Pipeline projection refused / unavailable",
  "readPipelineState",
  "pipelineRequestGeneration",
  "invalidatePipelineSelection",
  "No synthetic stages are shown.",
  "inspectCodingTarget",
  "previewCodingContext",
  "Add to Jarvis context",
  "context_digest",
  "added_context_refs",
  "suggestCodingModification",
  "Browsing has not entered proposal context",
  "Proposals for review",
  "Suggestions remain proposals for review."
], "140 operator contract is incomplete");
check(
  /if \(next\.state !== "current"[\s\S]*?return;\s*}\s*proposalGeneration\.current \+= 1;\s*setProposal\(null\);\s*setContextBinding\(next\);/.test(workbench),
  "context binding completion does not invalidate in-flight proposals"
);
check(
  /setMatches\(\[\]\); setRepositoryError\("search", null\)/.test(workbench) &&
  /setPrEvidence\(null\);\s*setRepositoryErrors\(\(current\) => \(\{ \.\.\.current, pr: null, checks: null, reviews: null \}\)\)/.test(workbench) &&
  /setContextBinding\(null\); setProposal\(null\);\s*setRepositoryErrors\(\(current\) => \(\{ \.\.\.current, context: null, proposal: null \}\)\)/.test(workbench),
  "repository operations do not preserve independent refusal ownership"
);
check(
  /setPrEvidence\(\{ pr: pr\.payload \}\);[\s\S]*?Promise\.allSettled\([\s\S]*?checks\.status === "fulfilled"[\s\S]*?reviews\.status === "fulfilled"/.test(workbench) &&
  /checks: checks\.value\.partial/.test(workbench) &&
  /reviews: reviews\.value\.partial/.test(workbench) &&
  /setRepositoryError\("checks", errorText\(checks\.reason\)\)/.test(workbench) &&
  /setRepositoryError\("reviews", errorText\(reviews\.reason\)\)/.test(workbench),
  "PR evidence fan-out does not preserve successful sibling evidence and explicit failures"
);
check(
  /const \[runtimeError, setRuntimeError\]/.test(workbench) &&
  /const \[pipelineError, setPipelineError\]/.test(workbench) &&
  /setPipelineError\(errorText\(cause\)\)/.test(workbench) &&
  /setPipelineError\(null\);\s*};/.test(workbench) &&
  /status=\{pipelineError \? "Projection error"/.test(workbench),
  "runtime and pipeline refusal states are not independently owned/rendered"
);
check(
  /function canonicalPrNumber\(value: string\): number \| null \{[\s\S]*?\/\^\[1-9\]\[0-9\]\*\$\/[\s\S]*?Number\.isSafeInteger/.test(workbench) &&
  /const prNumber = canonicalPrNumber\(prInput\);/.test(workbench),
  "PR evidence still coerces non-canonical operator identities"
);
check(
  /function canonicalSpecId\(value: string\): string \| null \{[\s\S]*?\/\^\[0-9\]\{3\}\[a-z\]\?\$\/[\s\S]*?value\.slice\(0, 3\)/.test(workbench) &&
  /const requestSpecId = canonicalSpecId\(specId\);/.test(workbench) &&
  /readPipelineState\(CODING_REPOSITORY, prNumber, requestSpecId\)/.test(workbench),
  "pipeline projection still normalizes a different PR/spec selection"
);
check(!workbench.includes("const prNumber = Number(prInput)"), "raw PR selection is still coerced with Number()");
check(!/readPipelineState\(CODING_REPOSITORY, prNumber, specId\.trim\(\)\)/.test(workbench), "pipeline request still trims the selected spec id");
check(!/const \[error, setError\]/.test(workbench), "shared cross-operation error slot remains in Coding surfaces");
check(!/api\.github\.com|github\.com\/api|localStorage|sessionStorage|child_process|powershell|cmd\.exe/i.test(workbench), "Coding surface crossed the browser authority boundary");
check(!/fetch\(/.test(workbench), "Coding surface bypasses the shared API client");
check(!/merge-base|rev-list|isAncestor|compareCommits|compare_commits/i.test(workbench), "browser appears to derive repository/runtime relation locally");

if (failures.length) {
  console.error("CODING-FRONTEND-INTEGRATION-1 deterministic acceptance failed:");
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}
console.log("CODING-FRONTEND-INTEGRATION-1 deterministic acceptance: PASS");
