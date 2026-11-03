#!/usr/bin/env node
// Spec 224 acceptance 3: measure the real Process owner surface after operator actions in Chromium.
// Run after building frontend: node e2e/owner_predicate_walk.mjs <evidence-dir> <source-data-root>
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import ts from "../frontend/node_modules/typescript/lib/typescript.js";

if (process.argv.includes("--help")) { console.log('node e2e/owner_predicate_walk.mjs <evidence-dir> <source-data-root>  (needs built frontend, Playwright)'); process.exit(0); }
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8225);
const base = `http://127.0.0.1:${port}`;
const evidence = resolve(process.argv[2] ?? "");
const sourceRoot = resolve(process.argv[3] ?? "");
if (!process.argv[2] || !process.argv[3]) throw new Error("usage: owner_predicate_real_app.mjs <evidence-dir> <source-data-root>");
const assert = (ok, message) => { if (!ok) throw new Error(message); };
await mkdir(evidence, { recursive: true });
const source = await readFile(join(root, "frontend/src/app/ownerPredicates.ts"), "utf8");
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
const module = { exports: {} };
new Function("require", "module", "exports", code)(() => ({}), module, module.exports);
const { processUnitRevised } = module.exports;

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-224-predicate-"));
await cp(sourceRoot, dataRoot, { recursive: true, filter: (path) => !/jarvisos\.db(-wal|-shm|-journal)?$/.test(path) });
const snapshot = spawnSync(python, ["-I", "-c", [
  "import sqlite3,sys",
  "src=sqlite3.connect('file:'+sys.argv[2]+'/jarvisos.db?mode=ro',uri=True); dst=sqlite3.connect(sys.argv[1]+'/jarvisos.db'); src.backup(dst)",
  "dst.execute(\"UPDATE artifacts SET stored_path=? || substr(stored_path, ?) WHERE stored_path LIKE ? || '%'\",(sys.argv[1],len(sys.argv[2])+1,sys.argv[2]))",
  "dst.commit()"].join("\n"), dataRoot, sourceRoot], { encoding: "utf8" });
assert(snapshot.status === 0, `snapshot failed: ${snapshot.stderr}`);
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });
const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, XDG_STATE_HOME: join(dataRoot, "host"), LOCALAPPDATA: join(dataRoot, "host") };
delete env.JARVISOS_BLUECAD_TOOL_REGISTRY;
await new Promise((ok, fail) => {
  const probe = createServer().once("error", () => fail(new Error(`port ${port} is in use`)))
    .once("listening", () => probe.close(ok)).listen(port, "127.0.0.1");
});
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)],
  { cwd: backend, env, stdio: ["ignore", openSync(join(evidence, "backend.log"), "w"), openSync(join(evidence, "backend.log"), "a")] });
const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const dirty = spawnSync("git", ["status", "--porcelain"], { cwd: root, encoding: "utf8" }).stdout.trim();
const result = { head, dirty, source_data_root: sourceRoot, data_root: "isolated SQLite backup and file snapshot", cases: [] };
let browser;
try {
  for (let i = 0; ; i++) {
    assert(server.exitCode === null, "backend exited");
    try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* starting */ }
    assert(i < 120, "backend did not become healthy");
    await delay(500);
  }
  browser = await chromium.launch({ headless: true });
  result.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });
  await context.addInitScript(() => window.addEventListener("jarvis:workspace-surface", (event) => { window.__processSurface = event.detail; }));
  const page = await context.newPage();
  page.setDefaultTimeout(45_000);
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  // Since BUBU-3 the C1 Example is a case study offered by the Project version picker, not a Project picker entry.
  const picker = page.getByRole("combobox", { name: "Project version", exact: true });
  const option = picker.locator('option[value^="case:"]').filter({ hasText: /C1 · smooth tube/ });
  await option.waitFor({ state: "attached" });
  await picker.selectOption(await option.getAttribute("value"));
  await page.getByRole("heading", { level: 1, name: /case study C1 smooth tube/ }).waitFor();
  await page.getByRole("navigation", { name: "Design stages" }).getByRole("button", { name: "Plant", exact: true }).click();
  const units = page.getByRole("img", { name: "Process flowsheet draft" }).locator('g[role="button"]');
  await units.first().waitFor();
  await units.first().focus();
  const surface = () => page.evaluate(() => window.__processSurface ?? null);
  await page.waitForFunction(() => window.__processSurface?.process_selection?.[0]?.kind === "unit");
  const before = await surface();
  const unit = before.process_selection[0];
  const expected = { draft_id: before.draft_id, unit_id: unit.id, observed_revision: before.revision };
  const predicate = async () => processUnitRevised(await surface(), expected);
  assert(!await predicate(), "predicate true before action");
  const tag = page.getByRole("textbox", { name: "Tag", exact: true });
  const rename = page.getByRole("button", { name: "Rename", exact: true });
  assert(await rename.isDisabled(), "unchanged tag should disable Rename");
  await rename.click({ force: true });
  result.cases.push({ action: "no-op", before, after: await surface(), completed: await predicate() });
  assert(!result.cases.at(-1).completed, "no-op completed");

  await tag.fill(`${unit.tag}-proof`);
  const patch = "**/process/drafts/*/patch";
  await page.route(patch, (route) => route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"proof rejection"}' }));
  await rename.click();
  await page.getByText(/proof rejection|server error|500/i).first().waitFor();
  result.cases.push({ action: "failed", after: await surface(), completed: await predicate() });
  assert(!result.cases.at(-1).completed, "failed action completed");
  await page.unroute(patch);

  let release;
  const held = new Promise((resolveHeld) => { release = resolveHeld; });
  let intercepted;
  const entered = new Promise((resolveEntered) => { intercepted = resolveEntered; });
  await page.route(patch, async (route) => { intercepted(); await held; await route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"held proof rejection"}' }); });
  await rename.click();
  await entered;
  await delay(700);
  result.cases.push({ action: "held patch request", after: await surface(), completed: await predicate() });
  assert(!result.cases.at(-1).completed, "held action completed");
  const rejected = page.waitForResponse((response) => response.url().endsWith("/patch") && response.status() === 500);
  release();
  await rejected;
  await delay(300);
  await page.unroute(patch);

  await rename.click();
  await page.waitForFunction((previous) => {
    const now = window.__processSurface;
    return now?.revision !== previous && now?.process_selection?.[0]?.tag?.endsWith("-proof");
  }, before.revision);
  result.cases.push({ action: "success", after: await surface(), completed: await predicate() });
  assert(result.cases.at(-1).completed, "successful operator edit did not complete");
  await context.close();
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  for (let i = 0; i < 20 && server.exitCode === null; i++) await delay(250);
  if (server.exitCode === null) server.kill("SIGKILL");
  await rm(dataRoot, { recursive: true, force: true }).catch(() => {});
  await writeFile(join(evidence, "summary.json"), JSON.stringify(result, null, 2) + "\n");
}
console.log(JSON.stringify({ head, browser: result.browser, cases: result.cases.map(({ action, completed }) => ({ action, completed })) }));
