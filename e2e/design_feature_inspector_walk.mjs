#!/usr/bin/env node
// Spec 235 slice 1: real-Chromium walk of the Design feature inspector. A clean profile opens "/", reaches Design with
// visible controls only, adds a component, adds a tube in the Geometry node, rebuilds (3D preview appears), changes the
// length and rebuilds (revision increments), refuses an invalid wall before any request, then undoes (new revision,
// earlier geometry). Revision and candidate lineage are recorded from read-only GETs. Runs on a fresh temporary data
// root; never the live app.
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/design_feature_inspector_walk.mjs <evidence-dir>
// Env: JARVISOS_PROOF_PORT (8235), PLAYWRIGHT_MODULE.
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) { console.log("node e2e/design_feature_inspector_walk.mjs <evidence-dir>  (needs built frontend, Playwright)"); process.exit(0); }
const playwright = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const chromium = playwright.chromium ?? playwright.default.chromium;
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8235);
const assert = (ok, message) => { if (!ok) throw new Error(message); };
assert(port !== 8000, "port 8000 is the live app; refusing");
const base = `http://127.0.0.1:${port}`;
if (!process.argv[2]) throw new Error("usage: design_feature_inspector_walk.mjs <evidence-dir>");
const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-235-data-"));
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });
const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, XDG_STATE_HOME: join(dataRoot, "host"), LOCALAPPDATA: join(dataRoot, "host") };
delete env.JARVISOS_BLUECAD_TOOL_REGISTRY;
await new Promise((ok, fail) => {
  const probe = createServer().once("error", () => fail(new Error(`port ${port} is in use; refusing to use a foreign backend`)))
    .once("listening", () => probe.close(ok)).listen(port, "127.0.0.1");
});
const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const dirty = spawnSync("git", ["status", "--porcelain"], { cwd: root, encoding: "utf8" }).stdout.trim();
const init = spawnSync(python, ["-c", [
  "from app.core.database import initialize_database",
  "from app.modules.workspaces.service import create_workspace, default_workspace_payload",
  "initialize_database()", "create_workspace(default_workspace_payload())"].join("; ")], { cwd: backend, env, encoding: "utf8" });
assert(init.status === 0, `isolated database initialization failed: ${init.stderr}`);
const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], { cwd: backend, env, stdio: ["ignore", log, log] });

const summary = { spec: "235", slice: 1, head, worktree_dirty: Boolean(dirty), browser: null, started_at: new Date().toISOString(),
  data_root: "fresh temporary JARVISOS_DATA_ROOT", steps: [], lineage: [], screenshots: [], write_requests: [], error_responses: [],
  console_errors: [], page_errors: [] };
let browser;
let failure = null;
try {
  for (let i = 0; ; i++) {
    assert(server.exitCode === null, "backend exited; see backend.log");
    try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* starting */ }
    assert(i < 120, "backend did not become healthy");
    await delay(500);
  }
  browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, colorScheme: "light" });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);
  page.on("console", (m) => { if (m.type() === "error") summary.console_errors.push(m.text()); });
  page.on("pageerror", (e) => summary.page_errors.push(String(e)));
  page.on("request", (r) => { if (!["GET", "HEAD", "OPTIONS"].includes(r.method())) summary.write_requests.push(`${r.method()} ${r.url().replace(base, "").split("?")[0]}`); });
  page.on("response", (r) => { if (r.status() >= 400) summary.error_responses.push(`${r.status()} ${r.request().method()} ${r.url().replace(base, "")}`); });
  const shot = async (name) => {
    const file = join(evidence, `${String(summary.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
    await page.screenshot({ path: file });
    summary.screenshots.push({ file: file.slice(evidence.length + 1), sha256: createHash("sha256").update(await readFile(file)).digest("hex") });
  };
  summary.failureShot = async () => page.screenshot({ path: join(evidence, "failure.png") }).catch(() => {});
  const step = (name, detail = {}) => summary.steps.push({ step: name, status: "pass", ...detail });
  const getJson = async (path) => (await fetch(`${base}${path}`)).json();

  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  const create = page.getByRole("button", { name: "Create workspace", exact: true });
  if (await create.isVisible()) { await page.getByLabel("Workspace name").fill("Feature Inspector Proof"); await create.click(); }
  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Design", exact: true }).click();
  await page.getByRole("heading", { level: 1 }).waitFor();
  step("reach_design_from_root");

  // A component, then its Geometry node in the CAD stage.
  await page.getByRole("navigation", { name: "Design stages" }).getByRole("button", { name: "CAD", exact: true }).click();
  await page.getByLabel("Add a component").fill("Proof tube");
  await page.getByRole("complementary", { name: "Model tree" }).getByRole("button", { name: "Add", exact: true }).click();
  const tree = page.getByRole("tree", { name: "Component model tree" });
  await tree.getByRole("treeitem", { name: /^Proof tube\b/ }).click();
  const geometryNode = tree.getByRole("treeitem", { name: /^Geometry\b/ });
  if (await geometryNode.count()) await geometryNode.first().click();
  const inspector = page.getByRole("region", { name: "Feature inspector" });
  const lineage = inspector.getByRole("status").first();
  await inspector.waitFor();
  await lineage.getByText("No geometry yet").waitFor();
  const viewport = page.getByRole("region", { name: "Component viewport" });
  assert(await viewport.locator("canvas").count() === 0, "a 3D preview exists before any geometry");
  await shot("no-geometry-inspector");
  step("open_geometry_node_without_geometry");

  const ids = await getJson("/workspaces");
  const projectId = ids.find((item) => item.name === "Feature Inspector Proof")?.id ?? ids[0].id;
  const versions = await getJson(`/workspaces/${projectId}/design-versions`);
  const comps = await getJson(`/workspaces/${projectId}/design-versions/${versions.active_version_id}/components`);
  const componentId = comps.find((item) => item.name === "Proof tube").id;
  const history = () => getJson(`/workspaces/${projectId}/design-versions/${versions.active_version_id}/components/${componentId}/geometry/feature-history`);
  const record = async (label) => {
    const h = await history();
    const dataWorkspace = versions.versions.find((item) => item.id === versions.active_version_id).data_workspace_id;
    const candidates = await getJson(`/workspaces/${dataWorkspace}/bluecad/candidates`).catch(() => null);
    summary.lineage.push({ label, revision: h.revision, candidate_id: h.candidate_id, attached_candidate_id: h.attached_candidate_id,
      compiled_spec_id: h.compiled_spec_id, stale: h.stale_against_attached_candidate,
      parent_candidate_id: Array.isArray(candidates) ? candidates.find((c) => c.id === h.candidate_id)?.parent_candidate_id ?? null : "unavailable" });
    return h;
  };

  // 1. Add a tube and rebuild: acceptance 1.
  await inspector.getByRole("button", { name: "Add tube", exact: true }).click();
  const length = inspector.getByRole("spinbutton", { name: /^Length/ });
  assert(await length.inputValue() === "200", "tube default length");
  await inspector.getByRole("button", { name: "Rebuild geometry", exact: true }).click();
  await lineage.getByText(/Feature history revision 1 · matches the attached geometry/).waitFor();
  await viewport.locator("canvas").waitFor({ timeout: 60_000 });
  const first = await record("after first rebuild");
  assert(first.revision === 1 && first.attached_candidate_id === first.candidate_id, "first rebuild did not attach the candidate");
  await shot("tube-rebuilt-3d-preview");
  step("add_tube_rebuild_3d_preview", { spec_id: first.compiled_spec_id });

  // 2. Change the length and rebuild: acceptance 2.
  await length.fill("300");
  await inspector.getByRole("status").getByText("Edits not applied yet.").waitFor();
  await inspector.getByRole("button", { name: "Rebuild geometry", exact: true }).click();
  await lineage.getByText(/Feature history revision 2 /).waitFor();
  const second = await record("after length change");
  assert(second.revision === 2 && second.candidate_id !== first.candidate_id && second.compiled_spec_id !== first.compiled_spec_id, "length change did not create a new candidate");
  assert(await length.inputValue() === "300", "length not shown after rebuild");
  await shot("length-changed-revision-2");
  step("change_length_rebuild_revision_increments", { revision: second.revision });

  // 3. Invalid wall is refused before any request: acceptance 4 (client side).
  const writesBefore = summary.write_requests.length;
  await inspector.getByRole("spinbutton", { name: /^Wall/ }).fill("20");
  await inspector.getByRole("button", { name: "Rebuild geometry", exact: true }).click();
  await inspector.getByRole("alert").getByText(/Wall must be less than half the outer Ø/).waitFor();
  assert(summary.write_requests.length === writesBefore, "an invalid dimension reached the server");
  assert((await history()).revision === 2, "revision changed after a refused edit");
  await shot("invalid-wall-refused-client-side");
  step("invalid_dimension_refused_before_request");
  await inspector.getByRole("spinbutton", { name: /^Wall/ }).fill("2");

  // 4. Undo: a new revision rebuilt from the earlier sequence: acceptance 3.
  await inspector.getByRole("button", { name: "Undo", exact: true }).click();
  await lineage.getByText(/Feature history revision 3 /).waitFor();
  const third = await record("after undo");
  assert(third.revision === 3 && third.compiled_spec_id === first.compiled_spec_id && third.candidate_id !== first.candidate_id, "undo did not restore the earlier spec as a new revision");
  assert(await length.inputValue() === "200", "length not restored by undo");
  await viewport.locator("canvas").waitFor();
  await shot("undo-revision-3");
  step("undo_restores_earlier_spec_as_new_revision", { revision: third.revision, spec_id: third.compiled_spec_id });

  assert(summary.page_errors.length === 0, `page errors: ${summary.page_errors.join("; ")}`);
} catch (error) {
  failure = error;
  await summary.failureShot?.();
  summary.failure = String(error?.stack ?? error);
} finally {
  delete summary.failureShot;
  summary.finished_at = new Date().toISOString();
  summary.result = failure ? "fail" : "pass";
  await writeFile(join(evidence, "evidence.json"), JSON.stringify(summary, null, 2) + "\n");
  await browser?.close().catch(() => {});
  server.kill("SIGTERM");
  await delay(500);
  await rm(dataRoot, { recursive: true, force: true });
}
if (failure) { console.error(failure); process.exit(1); }
console.log(`design feature inspector walk: pass (${evidence})`);
