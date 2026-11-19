#!/usr/bin/env node
// Spec 320: Browser walk of a case comparison carried by a converged Process-editor run.
// 1. From `/` in a clean browser profile, Development -> Prontezza opens the board using visible controls only.
// 2. With a converged process run whose KPI `water_input` is the case quantity, Calcolo and Confronto are green.
// 3. The Confronto detail names the run by its flowsheet label (Diagramma "Linea 1"), not by an internal id.
// 4. "Registra verdetto" records a verdict on the process run and the board shows it.

import { createHash } from "node:crypto";
import { openSync } from "node:fs";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/readiness_process_compare_walk.mjs <evidence-dir>");
  process.exit(0);
}

const pw = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const chromium = pw.chromium ?? pw.default?.chromium;
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8314);
const base = `http://127.0.0.1:${port}`;

if (!process.argv[2]) throw new Error("usage: readiness_process_compare_walk.mjs <evidence-dir>");
const evidence = resolve(process.argv[2]);
const assert = (ok, message) => {
  if (!ok) throw new Error(message);
};
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-320-data-"));
const controlDir = join(dataRoot, "control");
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });

// A temporary copy of the fake pack whose case compares a process KPI (1 kg/s feed = 3600 kg/h).
const zephyrPack = join(dataRoot, "pack");
await cp(join(backend, "tests/fixtures/packs/zephyr"), zephyrPack, { recursive: true });
const cases = await readFile(join(zephyrPack, "readiness_cases.toml"), "utf8");
await writeFile(
  join(zephyrPack, "readiness_cases.toml"),
  cases.replace('quantity = "density"', 'quantity = "water_input"').replace("value = 1000.0", "value = 3600.0").replace('unit = "kg/m^3"', 'unit = "kg/h"')
);

const env = {
  ...process.env,
  PYTHONPATH: backend,
  JARVISOS_DATA_ROOT: dataRoot,
  JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: join(controlDir, "continuity"),
  JARVIS_RELAY_REPO: join(dataRoot, "relay-repo"),
  XDG_STATE_HOME: join(dataRoot, "host"),
  LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_PACK_PATH: zephyrPack,
  JARVISOS_AGY_BIN: "/nonexistent/agy",
  JARVISOS_CODEX_BIN: "/nonexistent/codex",
  JARVISOS_AGENT_RELAY_BIN: "/nonexistent/agent-relay"
};
delete env.GH_TOKEN;
delete env.GITHUB_TOKEN;
delete env.JARVISOS_GITHUB_READ_TOKEN;

await new Promise((ok, fail) => {
  const probe = createServer()
    .once("error", () => fail(new Error(`port ${port} is in use`)))
    .once("listening", () => probe.close(ok))
    .listen(port, "127.0.0.1");
});

const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();

// Initialize isolated database with default workspace (zephyr case workspace not created yet)
const init = spawnSync(
  python,
  ["-c", "from app.core.bootstrap import initialize_storage\ninitialize_storage(seed_default=True)"],
  { cwd: backend, env, encoding: "utf8" }
);
assert(init.status === 0, `isolated database initialization failed: ${init.stderr}`);

const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], {
  cwd: backend,
  env,
  stdio: ["ignore", log, log]
});

const summary = {
  spec: "320",
  head,
  browser: null,
  started_at: new Date().toISOString(),
  console_errors: [],
  page_errors: [],
  screenshots: [],
  steps: []
};

let browser;
try {
  for (let i = 0; ; i++) {
    assert(server.exitCode === null, "backend exited prematurely");
    try {
      if ((await fetch(`${base}/health`)).ok) break;
    } catch {
      /* wait */
    }
    assert(i < 120, "backend did not become healthy in time");
    await delay(500);
  }

  browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: "light" });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);
  page.on("console", (m) => {
    if (m.type() === "error") summary.console_errors.push(m.text());
  });
  page.on("pageerror", (e) => summary.page_errors.push(String(e)));

  const shot = async (name) => {
    const file = join(evidence, `${String(summary.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
    await page.screenshot({ path: file, fullPage: true });
    summary.screenshots.push({
      file: file.slice(evidence.length + 1),
      sha256: createHash("sha256").update(await readFile(file)).digest("hex")
    });
  };

  // 1. Criterion 2: From `/` in a clean browser profile, Development -> Prontezza opens the board using visible controls only.
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  await page.getByRole("link", { name: "Development", exact: true }).click();
  await page.getByRole("link", { name: "Prontezza", exact: true }).click();
  await page.locator(".readiness-board").waitFor();
  await shot("prontezza-initial-from-root");
  summary.steps.push({ step: "open_prontezza_from_root_via_visible_controls", status: "pass" });

  // 2. A workspace and a converged process run, created through the product's own draft functions.
  const res = await fetch(`${base}/workspaces`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name: "Zephyr", slug: "zephyr" })
  });
  assert(res.ok, `workspace create failed: ${res.status}`);
  const ws = await res.json();
  const seed = spawnSync(
    python,
    [
      "-c",
      `import sys
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import AddStream, AddUnit, SetThermo
ws = sys.argv[1]
s = draft.create_draft(ws, "Linea 1")
s = draft.patch(ws, s["draft_id"], s["revision"], [
    SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
    AddUnit(op="add_unit", id="pump", type="Pump", tag="Pump1", x=100, y=100),
    AddStream(op="add_stream", id="feed", tag="Feed", x=0, y=100)])
d = draft.draft_dir(ws, s["draft_id"])
run = {"run_id": "e" * 32, "draft_id": s["draft_id"], "draft_revision": s["revision"], "action": "run",
       "status": "completed", "started_at": "2026-11-19T01:00:00+00:00",
       "streams": {"Feed": {"mass_flow_kg_s": 1.0, "mass_fractions": {"Water": 1.0}}}, "units": {}}
run["outcome"] = draft.run_outcome(run, draft.load_revision(d, s["revision"])["document"])
assert run["outcome"]["state"] == "converged", run["outcome"]
draft.record_run(d, run)
`,
      ws.id
    ],
    { cwd: backend, env, encoding: "utf8" }
  );
  assert(seed.status === 0, `process run seed failed: ${seed.stderr}`);

  await page.getByRole("link", { name: "Development", exact: true }).click();
  await page.getByRole("link", { name: "Prontezza", exact: true }).click();
  await page.locator(".readiness-board").waitFor();
  await page.reload({ waitUntil: "networkidle" });
  await page.locator('.readiness-board__cell-button[data-status="green"]').first().waitFor();
  const runCell = page.locator('.readiness-board__cell-button[aria-label*="Calcolo"]').first();
  assert((await runCell.getAttribute("data-status")) === "green", "Calcolo cell is green");
  const compareCell = page.locator('.readiness-board__cell-button[aria-label*="Confronto"]').first();
  assert((await compareCell.getAttribute("data-status")) === "green", "Confronto cell is green");
  await shot("process-run-makes-calcolo-and-confronto-green");
  summary.steps.push({ step: "converged_process_run_makes_calcolo_and_confronto_green", status: "pass" });

  // 3. The comparison names the flowsheet, not an internal reference.
  await compareCell.click();
  const detail = page.locator(".readiness-board__detail");
  await detail.waitFor();
  await detail.getByText("Calcolato").waitFor();
  await detail.getByText('Diagramma "Linea 1"').waitFor();
  const detailText = await detail.innerText();
  assert(!detailText.includes("process:"), "comparison detail shows no internal run reference");
  assert(!detailText.includes(dataRoot), "comparison detail shows no data-root path");
  await shot("confronto-detail-names-the-flowsheet");
  summary.steps.push({ step: "confronto_detail_names_flowsheet_label", status: "pass" });

  // 4. Record a verdict on the process run from the board.
  await detail.getByRole("button", { name: "Registra verdetto" }).click();
  await detail.getByRole("button", { name: "Accetta verdetto" }).waitFor();
  await shot("verdict-recorded-on-process-run");
  summary.steps.push({ step: "verdict_recorded_on_process_run", status: "pass" });

  assert(summary.console_errors.length === 0, `console errors: ${JSON.stringify(summary.console_errors)}`);
  assert(summary.page_errors.length === 0, `page errors: ${JSON.stringify(summary.page_errors)}`);
  summary.passed = summary.steps.length === 4 && summary.steps.every((s) => s.status === "pass");
  await context.close();
} catch (error) {
  summary.failure = String(error.stack ?? error);
  summary.passed = false;
  throw error;
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  for (let i = 0; i < 20 && server.exitCode === null; i++) await delay(250);
  if (server.exitCode === null) server.kill("SIGKILL");
  await rm(dataRoot, { recursive: true, force: true }).catch(() => {});
  summary.finished_at = new Date().toISOString();
  await writeFile(join(evidence, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
}

console.log(JSON.stringify({ head, browser: summary.browser, passed: summary.passed, steps: summary.steps }));
