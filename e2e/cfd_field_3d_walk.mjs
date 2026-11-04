#!/usr/bin/env node
// Spec 233: run only with a temporary copy of a data root retaining a finished C1 tube_run with 3D exports.
// Build with VITE_API_BASE_URL= first. This scaffold records real browser evidence when such a root is supplied.
import { cp, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) { console.log("node e2e/cfd_field_3d_walk.mjs <evidence-dir> <source-data-root>"); process.exit(0); }
if (!process.argv[2] || !process.argv[3]) throw new Error("evidence directory and source data root required");
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8233);
if (!Number.isInteger(port) || port === 8000) throw new Error("use a private proof port");
const base = `http://127.0.0.1:${port}`;
const evidence = resolve(process.argv[2]);
const source = resolve(process.argv[3]);
const copy = await mkdtemp(join(tmpdir(), "jarvisos-233-data-"));
await mkdir(evidence, { recursive: true });
let server, browser;
const record = { spec: 233, started_at: new Date().toISOString(), screenshots: [], page_errors: [], console_errors: [], results: {} };
const check = (ok, reason) => { if (!ok) throw new Error(reason); };
try {
  await cp(source, copy, { recursive: true, filter: (path) => !/jarvisos\.db(-wal|-shm|-journal)?$/.test(path) });
  const backup = spawnSync(python, ["-I", "-c", [
    "import sqlite3,sys",
    "src=sqlite3.connect('file:'+sys.argv[2]+'/jarvisos.db?mode=ro',uri=True); dst=sqlite3.connect(sys.argv[1]+'/jarvisos.db'); src.backup(dst)",
    "dst.execute(\"UPDATE artifacts SET stored_path=? || substr(stored_path, ?) WHERE stored_path LIKE ? || '%'\",(sys.argv[1],len(sys.argv[2])+1,sys.argv[2]))",
    "dst.commit()"].join("\n"), copy, source], { encoding: "utf8" });
  check(backup.status === 0, `data-root copy failed: ${backup.stderr}`);
  await mkdir(join(copy, "host"), { recursive: true });
  await new Promise((ok, fail) => { const probe = createServer().once("error", () => fail(new Error(`proof port ${port} is in use`)))
    .once("listening", () => probe.close(ok)).listen(port, "127.0.0.1"); });
  const log = openSync(join(evidence, "backend.log"), "w");
  const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: copy, XDG_STATE_HOME: join(copy, "host"), LOCALAPPDATA: join(copy, "host") };
  delete env.JARVISOS_BLUECAD_TOOL_REGISTRY;
  server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], { cwd: backend, env, stdio: ["ignore", log, log] });
  for (let i = 0; ; i++) { try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* starting */ }
    check(i < 120 && server.exitCode === null, "proof backend did not become healthy"); await delay(500); }
  browser = await chromium.launch({ headless: true });
  record.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  await context.addInitScript(() => { window.__initialStorage = [localStorage.length, sessionStorage.length]; });
  const page = await context.newPage();
  page.on("pageerror", (error) => record.page_errors.push(String(error)));
  page.on("console", (message) => { if (message.type() === "error") record.console_errors.push(message.text()); });
  const shot = async (name) => { const path = join(evidence, `${name}.png`); await page.screenshot({ path }); record.screenshots.push(name); };
  const start = Date.now();
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  check(JSON.stringify(await page.evaluate(() => window.__initialStorage)) === "[0,0]", "browser profile was not clean");
  const picker = page.getByRole("combobox", { name: "Project version", exact: true });
  const option = picker.locator('option[value^="case:"]').filter({ hasText: /C1 · smooth tube/ });
  await option.waitFor({ state: "attached" });
  await picker.selectOption(await option.getAttribute("value"));
  const stages = page.getByRole("navigation", { name: "Design stages" });
  const tree = page.getByRole("tree", { name: "Component model tree" });
  await stages.getByRole("button", { name: "CFD", exact: true }).click();
  await tree.getByRole("treeitem", { name: /^C1-CFD$/ }).click();
  await tree.getByRole("treeitem", { name: /^Results\b/ }).click();
  const panel = page.getByRole("region", { name: "CFD 3D field" }).first();
  const canvas = panel.getByRole("img", { name: /3D CFD computed surfaces/ });
  await canvas.waitFor({ timeout: 60_000 });
  record.results.first_render_ms = Date.now() - start;
  await shot("computed");
  const probe = panel.locator(".design-project__field-readout");
  const sample = async (pattern) => { const box = await canvas.boundingBox(); check(box, "canvas has no bounds");
    for (const y of [0.3, 0.5, 0.7]) for (const x of [0.3, 0.5, 0.7]) {
      await page.mouse.click(box.x + box.width * x, box.y + box.height * y);
      if (pattern.test(await probe.innerText())) return await probe.innerText();
    }
    throw new Error(`could not probe ${pattern}`); };
  record.results.computed_probe = await sample(/Computed vertex/);
  await panel.getByRole("button", { name: "|U|" }).click();
  check((await panel.getByLabel("3D colour legend").innerText()).includes("m/s"), "|U| legend did not update");
  await panel.getByRole("checkbox", { name: "Show full pipe" }).check();
  await panel.getByText("Mirrored from the computed quarter, not computed").waitFor();
  record.results.mirrored_probe = await sample(/Reconstructed via .*computed source point/);
  await shot("full-pipe-probe");
  check(record.page_errors.length === 0 && record.console_errors.length === 0, "browser errors occurred");
  record.passed = true;
  await context.close();
} catch (error) { record.passed = false; record.error = String(error); throw error; }
finally {
  if (browser) await browser.close();
  if (server) { server.kill("SIGTERM"); await delay(1000); if (server.exitCode === null) server.kill("SIGKILL"); }
  await rm(copy, { recursive: true, force: true });
  record.finished_at = new Date().toISOString();
  await writeFile(join(evidence, "evidence.json"), JSON.stringify(record, null, 2));
}
