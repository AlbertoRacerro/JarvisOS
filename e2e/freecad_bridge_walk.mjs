#!/usr/bin/env node
// Spec 326: FreeCAD bridge UI against a hash-pinned fake peer.
// node e2e/freecad_bridge_walk.mjs <evidence-dir> [<data-root-copy>]
import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { setTimeout as delay } from "node:timers/promises";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = resolve(root, process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python"));
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8306);
const base = `http://127.0.0.1:${port}`;
if (!process.argv[2]) throw new Error("usage: freecad_bridge_walk.mjs <evidence-dir> [<data-root-copy>]");
const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });
const dataRoot = resolve(process.argv[3] ?? await mkdtemp(join(tmpdir(), "jarvisos-326-data-")));
const controlDir = join(dataRoot, "control");
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true });
const peer = join(dataRoot, "fake-python");
await writeFile(peer, '#!/bin/sh\nexec "$JARVISOS_PROOF_PYTHON" "$1"\n', { mode: 0o755 });
const state = join(dataRoot, "freecad-state.json");
await writeFile(state, JSON.stringify({ length: 10 }));
const token = "offline-walk-token";
const tokenFile = join(dataRoot, "freecad-token");
await writeFile(tokenFile, token, { mode: 0o600 });
const mcpCalls = [];
const mcp = createServer(async (req, res) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const request = JSON.parse(Buffer.concat(chunks).toString());
  assert(req.url === "/mcp" && req.method === "POST", "unexpected MCP endpoint");
  assert(req.headers.authorization === `Bearer ${token}`, "missing MCP bearer token");
  assert(req.headers["mcp-protocol-version"] === "2025-11-25", "wrong MCP version");
  assert(request.method === "tools/call" && request.params.name === "execute_python", "unexpected MCP tool");
  const code = request.params.arguments.code;
  const method = code.match(/# Jarvis FreeCAD: (\w+)/)?.[1];
  assert(["ping", "list_documents", "get_objects", "get_object", "edit_object"].includes(method), "unexpected script");
  const literal = code.match(/_params = json.loads\(([^\n]+)\)/)?.[1];
  assert(literal, "missing JSON parameters");
  const params = JSON.parse(JSON.parse(literal));
  mcpCalls.push(method);
  const current = JSON.parse(await readFile(state));
  let result;
  if (method === "ping") result = true;
  else if (method === "list_documents") result = ["Demo"];
  else if (method === "get_objects" || method === "get_object") {
    const object = { Name: "Box", Label: "Box", TypeId: "Part::Box", Properties: { Length: { value: current.length, read_only: false } } };
    result = method === "get_objects" ? [object] : object;
  } else if (params.old_value !== current.length) result = { success: false, code: "FREECAD_STALE_PREVIEW", mutation_outcome: "unchanged" };
  else if (params.value === 99) result = { success: false, mutation_outcome: "rolled_back" };
  else {
    current.length = params.value;
    await writeFile(state, JSON.stringify(current));
    result = { success: true, mutation_outcome: "applied" };
  }
  const page = { has_more: false, page: [{ stream: "stdout", text: `JARVIS_FREECAD_RESULT:${JSON.stringify(result)}\n` }] };
  const envelope = { jsonrpc: "2.0", id: request.id, result: { content: [{ type: "text", text: JSON.stringify(page) }] } };
  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify(envelope));
});
await new Promise((resolve, reject) => mcp.listen(39280, "127.0.0.1", resolve).once("error", reject));
const sha = createHash("sha256").update(await readFile(peer)).digest("hex");
const env = {
  ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: join(controlDir, "continuity"), RELAY_REPO: join(dataRoot, "relay-repo"),
  XDG_STATE_HOME: join(dataRoot, "host"), LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_FREECAD_PYTHON_PATH: peer, JARVISOS_FREECAD_PYTHON_SHA256: sha,
  JARVISOS_FREECAD_MCP_TOKEN_FILE: tokenFile, JARVISOS_PROOF_PYTHON: python, JARVISOS_AGY_BIN: "/nonexistent/agy", JARVISOS_CODEX_BIN: "/nonexistent/codex"
};
delete env.GH_TOKEN; delete env.GITHUB_TOKEN; delete env.JARVISOS_GITHUB_READ_TOKEN;
const seed = "from app.core.bootstrap import initialize_storage\ninitialize_storage()\n";
const init = spawnSync(python, ["-c", seed], { cwd: backend, env, encoding: "utf8" });
if (init.status !== 0) throw new Error(`database initialization failed: ${init.stderr}`);
const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)],
  { cwd: backend, env, stdio: ["ignore", log, log] });
const summary = { spec: "361", browser: null, console_errors: [], page_errors: [], steps: [], screenshots: [] };
const assert = (condition, message) => { if (!condition) throw new Error(message); };
let browser;
try {
  for (let i = 0; i < 120; i++) {
    try { if ((await fetch(`${base}/health`)).ok) break; } catch {}
    await delay(500);
  }
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
  await page.getByText("FreeCAD connesso").waitFor();
  assert(await page.getByLabel("Documento").inputValue() === "Demo", "the open document is not selected");
  await page.getByText("Box", { exact: true }).first().waitFor();
  summary.steps.push({ step: "open_freecad_and_read_document", status: "pass" });
  const value = page.getByLabel("Nuovo valore");
  await value.fill("20");
  await page.getByRole("button", { name: "Anteprima", exact: true }).click();
  await page.getByText("Anteprima: 10 → 20").waitFor();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Applica", exact: true }).click();
  await page.getByText("Valore aggiornato: 20").waitFor();
  await value.fill("99");
  await page.getByRole("button", { name: "Anteprima", exact: true }).click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Applica", exact: true }).click();
  await page.getByText("Il documento non è stato modificato.").waitFor();
  summary.steps.push({ step: "preview_apply_and_report_rollback", status: "pass" });
  await page.screenshot({ path: join(evidence, "freecad-panel.png"), fullPage: true });
  summary.screenshots.push("freecad-panel.png");
  assert(summary.console_errors.length === 0, `console errors: ${JSON.stringify(summary.console_errors)}`);
  assert(summary.page_errors.length === 0, `page errors: ${JSON.stringify(summary.page_errors)}`);
  summary.passed = true;
  assert(mcpCalls.includes("edit_object"), "the MCP edit template did not run");
  await writeFile(join(evidence, "summary.json"), JSON.stringify(summary, null, 2));
  await context.close();
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  mcp.close();
}
