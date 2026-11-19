#!/usr/bin/env node
// Spec 316: verify retrieval quality from the Memory / Ricerca screen.
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8300);
const base = `http://127.0.0.1:${port}`;
if (!process.argv[2]) throw new Error("usage: retrieval_eval_ui_walk.mjs <evidence-dir>");
const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });
const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-316-data-"));
const controlDir = join(dataRoot, "control");
const fixture = await mkdtemp(join(tmpdir(), "jarvisos-316-docs-"));
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });
await writeFile(join(fixture, "guida.md"), "# Fermentazione\n\nProcesso aerobico.\n", "utf8");
const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: join(controlDir, "continuity"), JARVIS_RELAY_REPO: join(dataRoot, "relay-repo"),
  XDG_STATE_HOME: join(dataRoot, "host"), LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_AGY_BIN: "/nonexistent/agy", JARVISOS_CODEX_BIN: "/nonexistent/codex", JARVISOS_AGENT_RELAY_BIN: "/nonexistent/agent-relay" };
delete env.GH_TOKEN; delete env.GITHUB_TOKEN; delete env.JARVISOS_GITHUB_READ_TOKEN;
await new Promise((ok, fail) => {
  const probe = createServer().once("error", () => fail(new Error(`port ${port} is in use`)))
    .once("listening", () => probe.close(ok)).listen(port, "127.0.0.1");
});
const init = spawnSync(python, ["-c", "from app.core.database import initialize_database; from app.modules.workspaces.service import create_workspace, default_workspace_payload; initialize_database(); create_workspace(default_workspace_payload())"], { cwd: backend, env, encoding: "utf8" });
if (init.status !== 0) throw new Error(init.stderr);
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], { cwd: backend, env, stdio: "ignore" });
let browser;
try {
  for (let i = 0; i < 120; i++) { try { if ((await fetch(`${base}/health`)).ok) break; } catch {} await delay(500); }
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  page.setDefaultTimeout(30_000);
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  await page.getByRole("link", { name: "Memory", exact: true }).click();
  await page.getByRole("link", { name: "Ricerca", exact: true }).click();
  const folder = page.getByLabel("Percorso della cartella");
  await folder.fill(fixture);
  await page.getByRole("button", { name: "Registra cartella" }).click();
  await page.getByText("Cartella registrata:", { exact: false }).waitFor();
  await page.getByRole("button", { name: "Aggiorna cartella" }).click();
  await page.getByText("Inclusi: 1", { exact: false }).waitFor();
  await page.getByLabel("Domanda").fill("fermentazione");
  await page.getByLabel("Titolo o ID atteso").fill("Fermentazione");
  await page.getByRole("button", { name: "Verifica recupero" }).click();
  await page.getByText("Utilizzabile", { exact: true }).waitFor();
  for (const metric of ["top1_hit_rate", "top3_hit_rate", "mrr", "zero_hit_count"]) {
    await page.getByText(new RegExp(metric)).waitFor();
  }
  await page.screenshot({ path: join(evidence, "retrieval-eval.png"), fullPage: true });
  await writeFile(join(evidence, "summary.json"), JSON.stringify({ passed: true, spec: "316" }) + "\n");
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  await delay(250);
  if (server.exitCode === null) server.kill("SIGKILL");
  await rm(dataRoot, { recursive: true, force: true });
  await rm(fixture, { recursive: true, force: true });
}
