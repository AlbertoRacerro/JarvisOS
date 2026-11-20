#!/usr/bin/env node
// Spec 335: real-Chromium walk of Coding → Runtime "Recent work" listing squash-merged pull requests.
// Starts at `/` in a clean profile on an isolated data root and reads the repository's local target branch.
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/recent_work_walk.mjs <evidence-dir>
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/recent_work_walk.mjs <evidence-dir>  (needs built frontend, Playwright)");
  process.exit(0);
}
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8335);
const base = `http://127.0.0.1:${port}`;
const evidence = resolve(process.argv[2] ?? "");
if (!process.argv[2]) throw new Error("usage: recent_work_walk.mjs <evidence-dir>");
const assert = (ok, message) => { if (!ok) throw new Error(message); };
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-335-data-"));
const controlDir = join(dataRoot, "control");
const continuityDir = join(controlDir, "continuity");
const relayRepo = join(dataRoot, "relay-repo");
await mkdir(continuityDir, { recursive: true });
await mkdir(join(relayRepo, ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });

const env = {
  ...process.env,
  PYTHONPATH: backend,
  JARVISOS_DATA_ROOT: dataRoot,
  JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: continuityDir,
  JARVIS_RELAY_REPO: relayRepo,
  XDG_STATE_HOME: join(dataRoot, "host"),
  LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_AGY_BIN: "/nonexistent/agy",
  JARVISOS_CODEX_BIN: "/nonexistent/codex",
  JARVISOS_AGENT_RELAY_BIN: "/nonexistent/agent-relay",
};
delete env.JARVISOS_BLUECAD_TOOL_REGISTRY;
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
const dirty = spawnSync("git", ["status", "--porcelain"], { cwd: root, encoding: "utf8" }).stdout.trim();
const init = spawnSync(python, ["-c", [
  "from app.core.database import initialize_database",
  "from app.modules.workspaces.service import create_workspace, default_workspace_payload",
  "initialize_database()",
  "create_workspace(default_workspace_payload())",
].join("; ")], {
  cwd: backend,
  env,
  encoding: "utf8",
});
assert(init.status === 0, `isolated database initialization failed: ${init.stderr}`);

const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], {
  cwd: backend,
  env,
  stdio: ["ignore", log, log],
});

const summary = {
  spec: "335",
  head,
  dirty,
  browser: null,
  started_at: new Date().toISOString(),
  data_root: "isolated temporary JARVISOS_DATA_ROOT + JARVIS_CONTROL_DIR + JARVIS_RELAY_REPO",
  console_errors: [],
  page_errors: [],
  screenshots: [],
  steps: [],
};

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
  page.on("console", (msg) => { if (msg.type() === "error") summary.console_errors.push(msg.text()); });
  page.on("pageerror", (err) => summary.page_errors.push(String(err)));

  const shot = async (name) => {
    const file = join(evidence, `${String(summary.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
    await page.screenshot({ path: file, fullPage: true });
    summary.screenshots.push({
      file: file.slice(evidence.length + 1),
      sha256: createHash("sha256").update(await readFile(file)).digest("hex"),
    });
  };

  // 1. Start at `/` in a clean profile and reach Coding -> Runtime through visible controls only.
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  const createBtn = page.getByRole("button", { name: "Create workspace", exact: true });
  if (await createBtn.isVisible()) {
    await page.getByLabel("Workspace name").fill("Recent Work Proof Workspace");
    await createBtn.click();
  }
  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Coding", exact: true }).click();
  await page.getByRole("navigation", { name: "Coding destinations" }).getByRole("link", { name: "Runtime", exact: true }).click();
  const recent = page.getByRole("region", { name: "Recent work" });
  await recent.locator(".coding-work-row").first().waitFor();
  summary.steps.push({ step: "reach_runtime_from_root", status: "pass" });

  // 2. Squash-merged pull requests are listed: spec ids from 285 on, merged after 16 Nov.
  const ids = await recent.locator(".coding-work-id").allTextContents();
  const dates = await recent.locator(".coding-work-row time").evaluateAll((nodes) => nodes.map((n) => n.getAttribute("datetime")));
  const newer = dates.filter((d) => d && d > "2026-11-17");
  const specs = ids.flatMap((id) => id.split(/[,–\s]+/)).filter((t) => /^[0-9]{3}/.test(t)).map((t) => Number.parseInt(t, 10));
  assert(newer.length > 0, `no item merged after 16 Nov: ${JSON.stringify(dates)}`);
  assert(specs.some((n) => n >= 285), `no spec from 285 on: ${JSON.stringify(ids)}`);
  await recent.locator(".coding-work-row summary").first().click();
  await shot("runtime-recent-work-squash-merges");
  summary.steps.push({ step: "recent_work_lists_squash_merges", status: "pass", ids, newest: dates[0], items: ids.length });

  assert(summary.console_errors.length === 0, `console errors: ${JSON.stringify(summary.console_errors)}`);
  assert(summary.page_errors.length === 0, `page errors: ${JSON.stringify(summary.page_errors)}`);
  summary.passed = summary.steps.length === 2 && summary.steps.every((s) => s.status === "pass");
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
