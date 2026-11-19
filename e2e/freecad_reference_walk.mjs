#!/usr/bin/env node
// Spec 327: FreeCAD FCStd upload and visual reference walk.
// node e2e/freecad_reference_walk.mjs <evidence-dir> [<data-root-copy>]
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, open, readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { setTimeout as delay } from "node:timers/promises";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = resolve(root, process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python"));
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8307);
const base = `http://127.0.0.1:${port}`;
if (!process.argv[2]) throw new Error("usage: freecad_reference_walk.mjs <evidence-dir> [<data-root-copy>]");
const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });
const dataRoot = resolve(process.argv[3] ?? await mkdtemp(join(tmpdir(), "jarvisos-327-data-")));
const controlDir = join(dataRoot, "control");
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true });

const peer = join(dataRoot, "fake-python");
await writeFile(peer, `#!${python}
import os, sys
if len(sys.argv) > 2:
    mode = sys.argv[2]
    out_dir = sys.argv[4]
    os.makedirs(out_dir, exist_ok=True)
    if mode == "list":
        print('[{"name":"Box","label":"Boîte","type_id":"Part::Box"}]')
    else:
        import build123d as bd
        bd.export_step(bd.Solid.make_box(10, 20, 30), os.path.join(out_dir, "model.step"))
    sys.exit(0)
import json
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"id": request["id"], "ok": True, "result": True}), flush=True)
`, { mode: 0o755 });
const sha = createHash("sha256").update(await readFile(peer)).digest("hex");
const env = {
  ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: join(controlDir, "continuity"), RELAY_REPO: join(dataRoot, "relay-repo"),
  XDG_STATE_HOME: join(dataRoot, "host"), LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_FREECAD_PYTHON_PATH: peer, JARVISOS_FREECAD_PYTHON_SHA256: sha,
  JARVISOS_AGY_BIN: "/nonexistent/agy", JARVISOS_CODEX_BIN: "/nonexistent/codex"
};
delete env.GH_TOKEN; delete env.GITHUB_TOKEN; delete env.JARVISOS_GITHUB_READ_TOKEN;
const init = spawnSync(python, ["-c", "from app.core.bootstrap import initialize_storage\ninitialize_storage(seed_default=True)\n"], { cwd: backend, env, encoding: "utf8" });
if (init.status !== 0) throw new Error(`database initialization failed: ${init.stderr}`);

// Tiny uncompressed ZIP containing Document.xml.
function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let i = 0; i < 8; i++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}
const name = Buffer.from("Document.xml");
const xml = Buffer.from("<document/>");
const crc = crc32(xml);
const local = Buffer.alloc(30);
local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4); local.writeUInt16LE(0, 6);
local.writeUInt16LE(0, 8); local.writeUInt16LE(0, 10); local.writeUInt32LE(crc, 14);
local.writeUInt32LE(xml.length, 18); local.writeUInt32LE(xml.length, 22);
local.writeUInt16LE(name.length, 26); local.writeUInt16LE(0, 28);
const central = Buffer.alloc(46);
central.writeUInt32LE(0x02014b50, 0); central.writeUInt16LE(20, 4); central.writeUInt16LE(20, 6);
central.writeUInt16LE(0, 8); central.writeUInt16LE(0, 10); central.writeUInt16LE(0, 12);
central.writeUInt16LE(0, 14); central.writeUInt32LE(crc, 16); central.writeUInt32LE(xml.length, 20);
central.writeUInt32LE(xml.length, 24); central.writeUInt16LE(name.length, 28); central.writeUInt16LE(0, 30);
central.writeUInt16LE(0, 32); central.writeUInt16LE(0, 34); central.writeUInt16LE(0, 36);
central.writeUInt32LE(0, 38); central.writeUInt32LE(30 + name.length, 42);
const end = Buffer.alloc(22);
end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(0, 4); end.writeUInt16LE(0, 6);
end.writeUInt16LE(1, 8); end.writeUInt16LE(1, 10); end.writeUInt32LE(central.length, 12);
end.writeUInt32LE(local.length + name.length + xml.length, 16); end.writeUInt16LE(0, 20);
const fcstd = Buffer.concat([local, name, xml, central, name, end]);
const fcstdPath = join(dataRoot, "Demo.FCStd");
await writeFile(fcstdPath, fcstd);

const log = await open(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)],
  { cwd: backend, env, stdio: ["ignore", log.fd, log.fd] });
const summary = { spec: "327", browser: null, console_errors: [], page_errors: [], steps: [], screenshots: [] };
const assert = (condition, message) => { if (!condition) throw new Error(message); };
let browser;
try {
  for (let i = 0; i < 120; i++) {
    try { if ((await fetch(`${base}/health`)).ok) break; } catch {}
    await delay(500);
  }
  const seedCandidate = await fetch(`${base}/workspaces/main/bluecad/candidates`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ brief_text: "FreeCAD proof candidate" })
  });
  assert(seedCandidate.ok, "candidate seed failed");
  browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);
  page.on("console", (message) => { if (message.type() === "error") summary.console_errors.push(message.text()); });
  page.on("pageerror", (error) => summary.page_errors.push(String(error)));
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  const tools = page.getByRole("navigation", { name: "Design tools" });
  if (!await tools.getByRole("link", { name: "BlueCad workbench", exact: true }).isVisible()) await tools.getByText("More Design tools").click();
  await tools.getByRole("link", { name: "BlueCad workbench", exact: true }).click();
  await page.getByRole("heading", { name: "Model workbench" }).waitFor();
  await page.getByRole("button", { name: "FreeCAD", exact: true }).click();
  await page.getByRole("heading", { name: "Parte FreeCAD nella vista" }).waitFor();
  await page.getByLabel("Scegli file .FCStd").setInputFiles(fcstdPath);
  await page.getByLabel("Oggetto FreeCAD").waitFor();
  await page.getByRole("button", { name: "Aggiungi alla vista", exact: true }).click();
  await page.getByText("Boîte", { exact: true }).waitFor();
  await page.getByText("Valida", { exact: true }).waitFor();
  for (const label of ["STEP", "STL", "3MF"]) assert(await page.getByRole("link", { name: label, exact: true }).isVisible(), `missing ${label}`);
  summary.steps.push({ step: "upload_choose_add_and_validate", status: "pass" });
  await page.getByRole("button", { name: "Rimuovi", exact: true }).click();
  await page.getByText("Nessuna parte FreeCAD nella vista.", { exact: true }).waitFor();
  summary.steps.push({ step: "remove_freecad_reference", status: "pass" });
  await page.screenshot({ path: join(evidence, "freecad-reference-panel.png"), fullPage: true });
  summary.screenshots.push("freecad-reference-panel.png");
  assert(summary.console_errors.length === 0, `console errors: ${JSON.stringify(summary.console_errors)}`);
  assert(summary.page_errors.length === 0, `page errors: ${JSON.stringify(summary.page_errors)}`);
  summary.passed = true;
  await writeFile(join(evidence, "summary.json"), JSON.stringify(summary, null, 2));
  await context.close();
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  await log.close();
}
