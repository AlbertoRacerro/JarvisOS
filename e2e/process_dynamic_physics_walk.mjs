#!/usr/bin/env node

import { createHash } from "node:crypto";
import { openSync } from "node:fs";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/process_dynamic_physics_walk.mjs <evidence-dir> [<data-root-copy>]");
  process.exit(0);
}
if (!process.argv[2]) throw new Error("usage: node e2e/process_dynamic_physics_walk.mjs <evidence-dir> [<data-root-copy>]");

const pw = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const chromium = pw.chromium ?? pw.default?.chromium;
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8325);
const base = `http://127.0.0.1:${port}`;
if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) throw new Error("Invalid proof port");

await new Promise((ok, fail) => {
  const probe = createServer().once("error", (error) => fail(new Error(`port ${port} is in use: ${error.message}`)))
    .once("listening", () => probe.close(ok)).listen(port, "127.0.0.1");
});

const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });
const sourceRoot = process.argv[3] ? resolve(process.argv[3]) : null;
// Spec 377: a source data root inside or containing the private vault is refused before it is read.
if (process.argv[3] && spawnSync("python3", [resolve(root, "scripts/vault_guard.py"), "check", resolve(process.argv[3])], { stdio: "inherit" }).status !== 0) {
  throw new Error("source data root refused by the vault guard");
}
const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-325-data-"));
const controlDir = join(dataRoot, "control");
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });
const env = {
  ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: join(controlDir, "continuity"), JARVIS_RELAY_REPO: join(dataRoot, "relay-repo"),
  XDG_STATE_HOME: join(dataRoot, "host"), LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_AGY_BIN: "/nonexistent/agy", JARVISOS_CODEX_BIN: "/nonexistent/codex",
  JARVISOS_AGENT_RELAY_BIN: "/nonexistent/agent-relay",
};
delete env.GH_TOKEN; delete env.GITHUB_TOKEN; delete env.JARVISOS_GITHUB_READ_TOKEN;

if (sourceRoot) {
  await cp(sourceRoot, dataRoot, { recursive: true, filter: (p) => !/jarvisos\.db(-wal|-shm|-journal)?$/.test(p) });
  const snapshot = spawnSync(python, ["-I", "-c", [
    "import sqlite3,sys",
    "src=sqlite3.connect('file:'+sys.argv[2]+'/jarvisos.db?mode=ro',uri=True); dst=sqlite3.connect(sys.argv[1]+'/jarvisos.db'); src.backup(dst)",
    "n=dst.execute(\"UPDATE artifacts SET stored_path=? || substr(stored_path, ?) WHERE stored_path LIKE ? || '%'\",(sys.argv[1],len(sys.argv[2])+1,sys.argv[2])).rowcount",
    "dst.commit(); print(n)",
  ].join("\n"), dataRoot, sourceRoot], { encoding: "utf8" });
  if (snapshot.status !== 0) throw new Error(`snapshot failed: ${snapshot.stderr}`);
} else {
  const init = spawnSync(python, ["-c", "from app.core.bootstrap import initialize_storage\ninitialize_storage(seed_default=True)"],
    { cwd: backend, env, encoding: "utf8" });
  if (init.status !== 0) throw new Error(`storage initialization failed: ${init.stderr}`);
}

const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)],
  { cwd: backend, env, stdio: ["ignore", log, log] });
const summary = { spec: "325", head, browser: null, started_at: new Date().toISOString(), console_errors: [], page_errors: [], screenshots: [], steps: [] };
let browser;
try {
  for (let i = 0; ; i++) {
    if (server.exitCode !== null) throw new Error("backend exited prematurely");
    try { if ((await fetch(`${base}/health`)).ok) break; } catch {}
    if (i > 120) throw new Error("backend did not become healthy in time");
    await delay(500);
  }
  browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, colorScheme: "light" });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);
  page.on("console", (m) => { if (m.type() === "error") summary.console_errors.push(m.text()); });
  page.on("pageerror", (e) => summary.page_errors.push(String(e)));
  const shot = async (name) => {
    const file = join(evidence, `${String(summary.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
    await page.screenshot({ path: file, fullPage: true });
    summary.screenshots.push({ file: file.slice(evidence.length + 1), sha256: createHash("sha256").update(await readFile(file)).digest("hex") });
  };

  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  await shot("root");
  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Design", exact: true }).click();
  await page.locator(".guided-flow-bar").waitFor();

  await page.locator(".guided-flow-bar__new-btn").click();
  await page.locator('.guided-flow-bar__create-form input[type="text"]').first().fill("Physics UI proof");
  const created = page.waitForResponse((r) => r.request().method() === "POST" && /\/engineering\/guided-questions$/.test(r.url()));
  await page.getByRole("button", { name: "Crea domanda" }).click();
  const workspaceId = (await (await created).json()).workspace_id;
  if (!workspaceId) throw new Error("question has no workspace_id");

  // Test setup, not part of the reachability claim: a requirement and a source make "model" the next step (as in the 319 walk).
  const seed = (code) => {
    const result = spawnSync(python, ["-c", code, workspaceId], { cwd: backend, env, encoding: "utf8" });
    if (result.status !== 0) throw new Error(`seed failed: ${result.stderr}`);
  };
  seed([
    "import sys",
    "from uuid import uuid4",
    "from app.core.database import open_sqlite_connection",
    "from app.modules.events.service import utc_now",
    "ws, now = sys.argv[1], utc_now()",
    "with open_sqlite_connection() as c:",
    "    c.execute(\"INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) VALUES (?, ?, 'Produttivita minima', 'active', ?, ?)\", (str(uuid4()), ws, now, now))",
    "    c.execute(\"INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, created_at, updated_at) VALUES (?, ?, 'Manuale', 'book', 'accepted', ?, ?)\", (str(uuid4()), ws, now, now))",
    "    c.commit()",
  ].join("\n"));
  await page.reload({ waitUntil: "networkidle" });
  await page.locator(".guided-flow-bar__question-card", { hasText: "Physics UI proof" }).click();
  await page.locator(".guided-flow-bar__next-title", { hasText: "Apri il diagramma di processo" }).waitFor();
  await page.locator(".guided-flow-bar__next-btn").click();
  await page.waitForURL("**/design/process");
  seed([
    "import sys",
    "from app.modules.process_stack import draft",
    "from app.modules.process_stack.draft_models import AddUnit",
    "ws=sys.argv[1]",
    "s=draft.create_draft(ws,'Physics proof')",
    "draft.patch(ws,s['draft_id'],s['revision'],[AddUnit(op='add_unit',id='pbr',type='PhotobioreactorT1',tag='PBR',x=100,y=100)])",
    "from datetime import UTC, datetime, timedelta",
    "from app.modules.environment import profiles",
    "t0=datetime(2026,1,1,tzinfo=UTC); stamps=[(t0+timedelta(hours=h)).isoformat().replace('+00:00','Z') for h in range(169)]",
    "profiles.create_profile(ws,name='Walk profile',timestamps=stamps,channels={'par':[500.0]*len(stamps)},resolution_minutes=60,provenance={'kind':'deterministic_test'})",
  ].join("\n"));
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("link", { name: "Design › Dynamic", exact: true }).waitFor();
  await shot("process-editor-with-pbr");
  summary.steps.push({ step: "reach_process_editor_with_photobioreactor", status: "pass" });

  await page.getByRole("link", { name: "Design › Dynamic", exact: true }).click();
  await page.getByRole("region", { name: "Dynamic Studio" }).waitFor();
  await page.getByRole("navigation", { name: "Dynamic simulation steps" }).getByRole("button", { name: /Scenario/ }).click();
  await page.locator('[aria-label="Scenario editor"]').waitFor();
  await shot("dynamic-scenario-editor");
  summary.steps.push({ step: "reach_dynamic_scenario_editor", status: "pass" });

  const profile = page.getByLabel("Environment profile");
  await profile.selectOption(await profile.locator("option", { hasText: "Walk profile" }).getAttribute("value"));

  const unitBox = page.getByLabel("Unit PBR");
  if (!(await unitBox.isChecked())) await unitBox.check();
  await page.getByLabel("Thermal mode").selectOption("state");
  await page.getByLabel("Carbon mode").selectOption("state");
  await page.getByLabel("Water density [kg/m3]").fill("1001");
  await page.getByLabel("Biomass carbon fraction").fill("0.55");
  await page.getByLabel("Back-wall albedo of PBR").fill("0.25");
  await shot("physics-fields-filled");

  await page.getByRole("button", { name: "Save scenario" }).click();
  await page.getByRole("status").filter({ hasText: "Saved scenario" }).waitFor();
  summary.steps.push({ step: "save_physics_scenario", status: "pass" });

  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("navigation", { name: "Dynamic simulation steps" }).getByRole("button", { name: /Scenario/ }).click();
  await page.locator('[aria-label="Scenario editor"]').waitFor();
  await page.getByLabel("Thermal mode").waitFor();
  const thermal = await page.getByLabel("Thermal mode").inputValue();
  const carbon = await page.getByLabel("Carbon mode").inputValue();
  const density = await page.getByLabel("Water density [kg/m3]").inputValue();
  const fraction = await page.getByLabel("Biomass carbon fraction").inputValue();
  const albedo = await page.getByLabel("Back-wall albedo of PBR").inputValue();
  if (thermal !== "state" || carbon !== "state" || density !== "1001" || fraction !== "0.55" || albedo !== "0.25") {
    throw new Error(`persisted values mismatch: ${JSON.stringify({ thermal, carbon, density, fraction, albedo })}`);
  }
  await shot("physics-fields-reloaded");
  summary.steps.push({ step: "reload_persists_physics_values", status: "pass" });

  if (summary.console_errors.length) throw new Error(`Console errors during walk: ${JSON.stringify(summary.console_errors)}`);
  if (summary.page_errors.length) throw new Error(`Page errors during walk: ${JSON.stringify(summary.page_errors)}`);
  summary.completed_at = new Date().toISOString();
  await writeFile(join(evidence, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
  console.log(`WALK PASS: ${evidence}`);
} finally {
  if (browser) await browser.close();
  server.kill("SIGTERM");
  await delay(500);
  try { await rm(dataRoot, { recursive: true, force: true }); } catch {}
}
