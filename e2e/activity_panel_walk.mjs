#!/usr/bin/env node
// Spec 298: real-Chromium walk of the Attività panel. Starts at `/` in a clean profile, opens the visible
// "Attività" control, opens a row detail, closes the panel. Supervisor state is a fixture under a temporary
// RELAY_CONTINUITY_DIR; the live control root is never read.
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/activity_panel_walk.mjs <evidence-dir>
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/activity_panel_walk.mjs <evidence-dir>  (needs built frontend, Playwright)");
  process.exit(0);
}
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8298);
const base = `http://127.0.0.1:${port}`;
if (!process.argv[2]) throw new Error("usage: activity_panel_walk.mjs <evidence-dir>");
const evidence = resolve(process.argv[2]);
const assert = (ok, message) => { if (!ok) throw new Error(message); };
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-298-data-"));
const controlDir = join(dataRoot, "control");
const continuityDir = join(controlDir, "continuity");
const jobsDir = join(continuityDir, "jobs");
await mkdir(jobsDir, { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });

// One finished registered job (fixture), created a few minutes ago.
const created = new Date(Date.now() - 10 * 60_000);
const stamp = (d) => d.toISOString().replace(/\.\d+Z$/, "Z");
const jobId = `j-${stamp(created).replace(/[-:]/g, "")}-abc123`;
const logPath = join(jobsDir, `${jobId}.log`);
await writeFile(logPath, "avvio del calcolo\ncalcolo completato\n");
await writeFile(join(jobsDir, `${jobId}.json`), JSON.stringify({
  schema: "relay-jobs.v1", id: jobId, label: "fixture sweep", spec: null, status: "succeeded",
  created_at: stamp(created), started_at: stamp(created), ended_at: stamp(new Date(created.getTime() + 5 * 60_000)),
  exit_code: 0, log: logPath,
}));

const env = {
  ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: continuityDir, JARVIS_RELAY_REPO: join(dataRoot, "relay-repo"),
  XDG_STATE_HOME: join(dataRoot, "host"), LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_AGY_BIN: "/nonexistent/agy", JARVISOS_CODEX_BIN: "/nonexistent/codex",
  JARVISOS_AGENT_RELAY_BIN: "/nonexistent/agent-relay",
};
delete env.GH_TOKEN; delete env.GITHUB_TOKEN; delete env.JARVISOS_GITHUB_READ_TOKEN;

await new Promise((ok, fail) => {
  const probe = createServer().once("error", () => fail(new Error(`port ${port} is in use`)))
    .once("listening", () => probe.close(ok)).listen(port, "127.0.0.1");
});
const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const init = spawnSync(python, ["-c", [
  "from app.core.database import initialize_database",
  "from app.modules.workspaces.service import create_workspace, default_workspace_payload",
  "initialize_database()", "create_workspace(default_workspace_payload())",
].join("; ")], { cwd: backend, env, encoding: "utf8" });
assert(init.status === 0, `isolated database initialization failed: ${init.stderr}`);
const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)],
  { cwd: backend, env, stdio: ["ignore", log, log] });

const summary = { spec: "298", head, browser: null, started_at: new Date().toISOString(),
  console_errors: [], page_errors: [], screenshots: [], steps: [] };
let browser;
try {
  for (let i = 0; ; i++) {
    assert(server.exitCode === null, "backend exited");
    try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* starting */ }
    assert(i < 120, "backend did not become healthy");
    await delay(500);
  }
  browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });
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
  const createBtn = page.getByRole("button", { name: "Create workspace", exact: true });
  if (await createBtn.isVisible()) {
    await page.getByLabel("Workspace name").fill("Activity Proof Workspace");
    await createBtn.click();
  }
  const control = page.getByRole("button", { name: /^Attività/ });
  await control.waitFor();
  summary.steps.push({ step: "control_visible_at_root", status: "pass" });

  await control.click();
  const panel = page.getByRole("region", { name: "Pannello attività" });
  await panel.waitFor();
  await panel.getByText("Un lavoro di calcolo registrato").waitFor();
  await panel.getByText("nessun download registrato").waitFor();
  const text = await panel.innerText();
  assert(!text.includes(jobId) && !text.includes("fixture sweep"), "panel must not show the job id or label");
  await shot("panel-open");
  summary.steps.push({ step: "open_panel_and_list_rows", status: "pass" });

  await panel.getByRole("button", { name: /Un lavoro di calcolo registrato/ }).click();
  const detail = panel.getByRole("region", { name: "Dettaglio attività" });
  await detail.getByText(/calcolo completato/).waitFor();
  await shot("row-detail");
  summary.steps.push({ step: "open_row_detail", status: "pass" });

  await panel.getByRole("button", { name: "Chiudi" }).click();
  await panel.waitFor({ state: "detached" });
  summary.steps.push({ step: "close_panel", status: "pass" });

  assert(summary.console_errors.length === 0, `console errors: ${JSON.stringify(summary.console_errors)}`);
  assert(summary.page_errors.length === 0, `page errors: ${JSON.stringify(summary.page_errors)}`);
  summary.passed = summary.steps.length === 4 && summary.steps.every((s) => s.status === "pass");
  await context.close();
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  for (let i = 0; i < 20 && server.exitCode === null; i++) await delay(250);
  if (server.exitCode === null) server.kill("SIGKILL");
  await rm(dataRoot, { recursive: true, force: true }).catch(() => {});
  summary.finished_at = new Date().toISOString();
  summary.passed = Boolean(summary.passed) && summary.console_errors.length === 0 && summary.page_errors.length === 0;
  await writeFile(join(evidence, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  process.exitCode = summary.passed ? 0 : 1;
}
console.log(JSON.stringify({ head, browser: summary.browser, passed: summary.passed, steps: summary.steps }));
