#!/usr/bin/env node
// Spec 315: from / on a clean profile, open Runs, run a correlation card, and verify Banco di prova.
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8299);
const base = `http://127.0.0.1:${port}`;
if (!process.argv[2]) throw new Error("usage: correlation_cards_walk.mjs <evidence-dir>");
const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });
const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-315-data-"));
const controlDir = join(dataRoot, "control");
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });
const env = {
  ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: join(controlDir, "continuity"), JARVIS_RELAY_REPO: join(dataRoot, "relay-repo"),
  XDG_STATE_HOME: join(dataRoot, "host"), LOCALAPPDATA: join(dataRoot, "host"),
};
delete env.GH_TOKEN; delete env.GITHUB_TOKEN; delete env.JARVISOS_GITHUB_READ_TOKEN;

await new Promise((ok, fail) => {
  const probe = createServer().once("error", () => fail(new Error(`port ${port} is in use`)))
    .once("listening", () => probe.close(ok)).listen(port, "127.0.0.1");
});
const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const init = spawnSync(python, ["-c",
  "from app.core.database import initialize_database\nfrom app.modules.workspaces.service import create_workspace, default_workspace_payload\ninitialize_database()\ncreate_workspace(default_workspace_payload())",
], { cwd: backend, env, encoding: "utf8" });
if (init.status !== 0) throw new Error(`database initialization failed: ${init.stderr}`);
const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)],
  { cwd: backend, env, stdio: ["ignore", log, log] });
const summary = { spec: "315", head, browser: null, started_at: new Date().toISOString(),
  console_errors: [], page_errors: [], screenshots: [], steps: [] };
let browser;
const assert = (ok, message) => { if (!ok) throw new Error(message); };
try {
  for (let i = 0; ; i++) {
    assert(server.exitCode === null, "backend exited");
    try { if ((await fetch(`${base}/health`)).ok) break; } catch {}
    assert(i < 120, "backend did not become healthy");
    await delay(500);
  }
  browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  page.on("console", (m) => { if (m.type() === "error") summary.console_errors.push(m.text()); });
  page.on("pageerror", (e) => summary.page_errors.push(String(e)));
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  const tools = page.getByRole("navigation", { name: "Design tools" });
  if (!await tools.getByRole("link", { name: "Runs", exact: true }).isVisible()) {
    await tools.getByText("More Design tools").click();
  }
  await tools.getByRole("link", { name: "Runs", exact: true }).click();
  await page.getByRole("heading", { name: "Runs", level: 1 }).waitFor();
  const section = page.getByRole("region", { name: "Banco di prova" });
  await section.waitFor();
  await section.getByRole("heading", { name: "Nuova esecuzione", level: 3 }).waitFor();
  await section.getByText("Formula:", { exact: false }).waitFor();
  await section.getByText("Intervallo di validità:", { exact: false }).waitFor();
  await section.getByText("Fonte:", { exact: false }).waitFor();
  summary.steps.push({ step: "open_runs_card_form", status: "pass" });

  await section.getByRole("button", { name: "Esegui" }).click();
  await section.getByRole("region", { name: "Dettaglio risultato" }).waitFor();
  await section.locator("li.bench-row").first().waitFor();
  const text = await section.innerText();
  assert(!text.includes("/tmp") && !text.includes(dataRoot), "no filesystem path in result view");
  const shot = join(evidence, "01-correlation-card-run.png");
  await page.screenshot({ path: shot, fullPage: true });
  summary.screenshots.push({ file: "01-correlation-card-run.png",
    sha256: createHash("sha256").update(await readFile(shot)).digest("hex") });
  summary.steps.push({ step: "run_card_and_show_new_result", status: "pass" });
  assert(summary.console_errors.length === 0, `console errors: ${JSON.stringify(summary.console_errors)}`);
  assert(summary.page_errors.length === 0, `page errors: ${JSON.stringify(summary.page_errors)}`);
  summary.passed = summary.steps.every((step) => step.status === "pass");
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
