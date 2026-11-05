#!/usr/bin/env node
// Spec 235 slice 2: clean-profile placement, drag, rebuild, undo, refusal and connection walk.
// Run against a temporary data root with a built frontend; never port 8000.
// JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/design_feature_placement_walk.mjs <evidence-dir>
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) { console.log("node e2e/design_feature_placement_walk.mjs <evidence-dir>  (needs built frontend, Playwright)"); process.exit(0); }
const playwright = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const chromium = playwright.chromium ?? playwright.default.chromium;
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8235);
const assert = (ok, message) => { if (!ok) throw new Error(message); };
assert(port !== 8000, "port 8000 is the live app; refusing");
const base = `http://127.0.0.1:${port}`;
if (!process.argv[2]) throw new Error("usage: design_feature_placement_walk.mjs <evidence-dir>");
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

const summary = { spec: "235", slice: 2, head, worktree_dirty: Boolean(dirty), browser: null, started_at: new Date().toISOString(),
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

  await inspector.getByRole("button", { name: "Add tube", exact: true }).click();
  await inspector.getByRole("button", { name: "Rebuild geometry", exact: true }).click();
  await lineage.getByText(/Feature history revision 1 /).waitFor();
  await viewport.locator("canvas").waitFor({ timeout: 60_000 });
  await inspector.getByRole("button", { name: "Add tube", exact: true }).click();
  await inspector.getByLabel("Placement tube_run_2").selectOption("free");
  const y = inspector.locator("#placement-tube_run_2-1");
  await y.fill("100");
  await inspector.getByRole("button", { name: "Rebuild geometry", exact: true }).click();
  await lineage.getByText(/Feature history revision 2 /).waitFor();
  const placed = await record("free placement");
  assert(placed.sequence.features[1].frame.origin[1] === 100 && !placed.sequence.features[1].connect_to, "free frame missing");
  await shot("free-placement");
  step("free_placement_rebuilt");

  await inspector.getByLabel("Place in viewport").last().check();
  await viewport.getByRole("button", { name: "Top view" }).click();
  const canvas = viewport.locator("canvas");
  const box = await canvas.boundingBox();
  assert(box, "canvas bounds unavailable");
  // Find the visible X handle by trying points near the projected marker. Each attempt is a real mouse drag.
  let changed = false;
  for (let dy = -120; dy <= 120 && !changed; dy += 12) {
    for (let dx = -120; dx <= 120 && !changed; dx += 12) {
      const before = await y.inputValue();
      const x = box.x + box.width / 2 + dx, py = box.y + box.height / 2 + dy;
      await page.mouse.move(x, py); await page.mouse.down(); await page.mouse.move(x + 35, py + 20, { steps: 8 }); await page.mouse.up();
      changed = await y.inputValue() !== before || await inspector.locator("#placement-tube_run_2-0").inputValue() !== "0";
    }
  }
  assert(changed, "could not drag the placement handle");
  await inspector.getByText("Edits not applied yet.").waitFor();
  const dragged = [await inspector.locator("#placement-tube_run_2-0").inputValue(), await y.inputValue(), await inspector.locator("#placement-tube_run_2-2").inputValue()].map(Number);
  await inspector.getByRole("button", { name: "Rebuild geometry", exact: true }).click();
  await lineage.getByText(/Feature history revision 3 /).waitFor();
  const moved = await record("dragged placement");
  assert(JSON.stringify(moved.sequence.features[1].frame.origin) === JSON.stringify(dragged), "dragged frame differs from fields");
  await inspector.getByRole("button", { name: "Undo", exact: true }).click();
  await lineage.getByText(/Feature history revision 4 /).waitFor();
  assert((await history()).sequence.features[1].frame.origin[1] === 100, "undo did not restore placement");
  await shot("undo-placement");
  step("drag_rebuild_undo");

  const writesBefore = summary.write_requests.length;
  await y.fill("Infinity");
  await inspector.getByRole("button", { name: "Rebuild geometry", exact: true }).click();
  await inspector.getByRole("alert").getByText(/finite number/).waitFor();
  assert(summary.write_requests.length === writesBefore, "non-finite value sent");
  await y.fill("100");
  await inspector.getByLabel("Placement tube_run_2").selectOption("connected");
  await inspector.getByRole("button", { name: "Rebuild geometry", exact: true }).click();
  await lineage.getByText(/Feature history revision 5 /).waitFor();
  const connected = await record("connected");
  assert(!connected.sequence.features[1].frame && connected.sequence.features[1].connect_to, "connection did not remove frame");
  await shot("connected");
  step("invalid_refused_and_connection_restored");
  // 5. Server refusal: acceptance 4 (server side). A concurrent edit made out of band (direct HTTP, not the UI) makes the
  // page's expected_revision stale; the page's own rebuild must be refused and must not overwrite the other edit.
  const componentsUrl = `/workspaces/${projectId}/design-versions/${versions.active_version_id}/components`;
  const beforeOob = await history();
  const liveComponent = (await getJson(componentsUrl)).find((item) => item.id === componentId);
  const oobSequence = structuredClone(beforeOob.sequence);
  const oobTube = oobSequence.features.find((f) => f.kind === "tube_run") ?? oobSequence.features[0];
  const lengthKey = Object.keys(oobTube.params ?? {}).find((k) => /length/i.test(k));
  assert(lengthKey, `cannot find a length parameter in the sequence: ${JSON.stringify(oobTube)}`);
  oobTube.params[lengthKey] = 450;
  const oobResponse = await fetch(`${base}${componentsUrl}/${encodeURIComponent(componentId)}/geometry/feature-history`, {
    method: "PUT", headers: { "content-type": "application/json" },
    body: JSON.stringify({ sequence: oobSequence, expected_revision: liveComponent.revision, expected_active_version_id: versions.active_version_id, expected_draft_revision: null })
  });
  const oobRequest = { origin: "out_of_band", request: `PUT ${componentsUrl}/${componentId}/geometry/feature-history`, status: oobResponse.status };
  summary.out_of_band_requests = [oobRequest];
  assert(oobResponse.ok, `out-of-band edit failed: ${oobResponse.status} ${await oobResponse.text()}`);
  const afterOob = await record("after out-of-band edit");
  assert(afterOob.revision === 6 && afterOob.candidate_id !== connected.candidate_id, "out-of-band edit did not create a new revision");
  // In the page, without reloading: change a dimension and rebuild.
  await inspector.locator("#placement-tube_run_1-0").fill("260");
  await inspector.getByRole("status").getByText("Edits not applied yet.").waitFor();
  const putResponse = page.waitForResponse((r) => r.request().method() === "PUT" && r.url().includes("/geometry/feature-history"));
  await inspector.getByRole("button", { name: "Rebuild geometry", exact: true }).click();
  const refusedResponse = await putResponse;
  const refusedStatus = refusedResponse.status();
  const refusedBody = await refusedResponse.json().catch(() => null);
  assert(refusedStatus >= 400, `the stale page edit was not refused (status ${refusedStatus})`);
  const alert = inspector.getByRole("alert");
  await alert.getByText(/^Not applied: .+ The previous geometry is still attached\.$/).waitFor();
  const alertText = (await alert.innerText()).trim();
  const detail = typeof refusedBody?.detail === "string" ? refusedBody.detail : JSON.stringify(refusedBody?.detail ?? refusedBody?.message ?? "");
  assert(detail && alertText.includes(detail.replace(/\.$/, "")), `alert does not show the server message: alert=${alertText} server=${detail}`);
  assert(await inspector.locator("#placement-tube_run_1-0").inputValue() === "260", "edited value was lost after the refusal");
  const afterRefusal = await record("after server refusal");
  assert(afterRefusal.revision === afterOob.revision && afterRefusal.candidate_id === afterOob.candidate_id
    && afterRefusal.attached_candidate_id === afterOob.attached_candidate_id, "the refused edit changed the server geometry");
  await shot("server-refusal-keeps-out-of-band-geometry");
  step("server_refusal_keeps_attached_geometry", { out_of_band: oobRequest, page_put_status: refusedStatus, server_message: detail, alert_text: alertText,
    attached_candidate_id: afterRefusal.attached_candidate_id, revision: afterRefusal.revision });

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
