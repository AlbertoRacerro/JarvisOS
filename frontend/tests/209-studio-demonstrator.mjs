import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const api = read("src/api/studio.ts");
assert.match(api, /openStudioExample = \(\) => request<StudioExample>\("\/studio\/example-workspace", "POST", \{\}\)/);
const page = read("src/pages/Studio.tsx");
for (const text of ["Open example workspace", "openStudioExample()", "onWorkspaceChange?.(example.workspace.id)", "studio-header__actions", "including your edits, were left unchanged", "sessionStorage.setItem(CARRIED_NOTICE, message)", "useState<string | null>(takeCarriedNotice)"]) assert.ok(page.includes(text), text);
// The control is refused locally while the runtime is known unavailable and while another action runs.
assert.match(page, /disabled=\{busy \|\| \(!!runtime && !runtimeReady\)\}[^>]*onClick=\{openExample\}/);
assert.match(read("src/App.tsx"), /<Studio [^>]*onWorkspaceChange=\{setWorkspaceId\}/);
assert.match(read("src/pages/studio.css"), /\.studio-header__actions\{/);
assert.match(read("package.json"), /npm run test:209/);
console.log("209 Studio demonstrator frontend ok");
