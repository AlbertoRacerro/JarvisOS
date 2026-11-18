#!/usr/bin/env node
// Spec 300: real-Chromium walk of the "Ricerca" memory search and document folder registration. Starts at `/` in a
// clean profile, opens Memory through visible navigation, opens Ricerca, registers a fixture folder outside the data root,
// refreshes it, searches for a word from one of its files, and sees the passage with its relative path and heading.
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/memory_search_walk.mjs <evidence-dir>
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/memory_search_walk.mjs <evidence-dir>  (needs built frontend, Playwright)");
  process.exit(0);
}
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8300);
const base = `http://127.0.0.1:${port}`;
if (!process.argv[2]) throw new Error("usage: memory_search_walk.mjs <evidence-dir>");
const evidence = resolve(process.argv[2]);
const assert = (ok, message) => { if (!ok) throw new Error(message); };
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-300-data-"));
const controlDir = join(dataRoot, "control");
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });

// Fixture folder outside the data root with two Markdown files
const fixtureDocsDir = await mkdtemp(join(tmpdir(), "jarvisos-300-fixture-docs-"));
await writeFile(
  join(fixtureDocsDir, "guida.md"),
  "# Fermentazione Biologica\n\nIl processo di fermentazione aerobica produce metaboliti ad alta resa.\n",
  "utf8"
);
await writeFile(
  join(fixtureDocsDir, "parametri.md"),
  "# Parametri Operativi\n\nLa temperatura di processo e il controllo del pH garantiscono stabilita.\n",
  "utf8"
);

const env = {
  ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: join(controlDir, "continuity"), JARVIS_RELAY_REPO: join(dataRoot, "relay-repo"),
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

// Seed database with one workspace
const seed = `
from app.core.database import initialize_database
from app.modules.workspaces.service import create_workspace, default_workspace_payload
initialize_database()
ws = create_workspace(default_workspace_payload())
`;
const init = spawnSync(python, ["-c", seed], { cwd: backend, env, encoding: "utf8" });
assert(init.status === 0, `isolated database initialization failed: ${init.stderr}`);
const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)],
  { cwd: backend, env, stdio: ["ignore", log, log] });

const summary = { spec: "300", head, browser: null, started_at: new Date().toISOString(),
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
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: "light" });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);
  page.on("console", (m) => { if (m.type() === "error") summary.console_errors.push(m.text()); });
  page.on("pageerror", (e) => summary.page_errors.push(String(e)));
  const shot = async (name) => {
    const file = join(evidence, `${String(summary.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
    await page.screenshot({ path: file, fullPage: true });
    summary.screenshots.push({ file: file.slice(evidence.length + 1), sha256: createHash("sha256").update(await readFile(file)).digest("hex") });
  };

  // 1. From `/` open Memory, then Ricerca
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  await page.getByRole("link", { name: "Memory", exact: true }).click();
  await page.getByRole("link", { name: "Ricerca", exact: true }).click();
  await page.getByRole("heading", { name: "Ricerca", level: 1 }).waitFor();
  await shot("ricerca-initial");
  summary.steps.push({ step: "open_ricerca_from_root", status: "pass" });

  // 2. Type folder path into "Percorso della cartella" and click "Registra cartella"
  const folderInput = page.getByLabel("Percorso della cartella");
  await folderInput.waitFor();
  await folderInput.fill(fixtureDocsDir);
  await page.getByRole("button", { name: "Registra cartella" }).click();
  await page.getByText("Cartella registrata:").waitFor();
  await shot("folder-registered");
  summary.steps.push({ step: "register_folder", status: "pass" });

  // 3. Click "Aggiorna cartella"
  await page.getByRole("button", { name: "Aggiorna cartella" }).click();
  await page.getByText("Inclusi: 2").waitFor();
  await shot("folder-refreshed");
  summary.steps.push({ step: "refresh_folder", status: "pass" });

  // 4. Search a word from a fixture file and see the passage with its relative path and heading
  const searchInput = page.getByLabel("Cerca nella memoria");
  await searchInput.fill("fermentazione");
  await page.getByRole("button", { name: "Cerca" }).click();

  await page.getByText("guida.md").waitFor();
  await page.getByText("Fermentazione Biologica").waitFor();
  await page.getByText("fermentazione aerobica").waitFor();
  await shot("search-results");
  summary.steps.push({ step: "search_passage_found_with_path_and_heading", status: "pass" });

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
  await rm(fixtureDocsDir, { recursive: true, force: true }).catch(() => {});
  summary.finished_at = new Date().toISOString();
  summary.passed = Boolean(summary.passed) && summary.console_errors.length === 0 && summary.page_errors.length === 0;
  await writeFile(join(evidence, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  process.exitCode = summary.passed ? 0 : 1;
}
console.log(JSON.stringify({ head, browser: summary.browser, passed: summary.passed, steps: summary.steps }));
