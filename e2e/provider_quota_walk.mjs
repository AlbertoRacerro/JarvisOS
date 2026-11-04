#!/usr/bin/env node
// Real-Chromium walk of Coding -> Runtime provider usage, including a failed refresh
// that preserves the last good GitHub Copilot reading.
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/provider_quota_walk.mjs <evidence-dir>
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/provider_quota_walk.mjs <evidence-dir>  (needs built frontend, Playwright)");
  process.exit(0);
}
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8241);
const base = `http://127.0.0.1:${port}`;
const evidence = resolve(process.argv[2] ?? "");
if (!process.argv[2]) throw new Error("usage: provider_quota_walk.mjs <evidence-dir>");
const assert = (ok, message) => { if (!ok) throw new Error(message); };
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-provider-quota-data-"));
const controlDir = join(dataRoot, "control");
const continuityDir = join(controlDir, "continuity");
const relayRepo = join(dataRoot, "relay-repo");
const fakeBin = join(dataRoot, "fake-bin");
await mkdir(continuityDir, { recursive: true });
await mkdir(join(relayRepo, ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });
await mkdir(fakeBin, { recursive: true });

await writeFile(join(fakeBin, "agent-relay"), `#!/bin/sh
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
five=$(date -u -d '2 hours' +%Y-%m-%dT%H:%M:%SZ)
week=$(date -u -d '3 days' +%Y-%m-%dT%H:%M:%SZ)
printf '{"agents":[{"agent_key":"claude","status":"ok","as_of":"%s","windows":[{"id":"five_hour","used_percent":40,"resets_at":"%s"},{"id":"seven_day","used_percent":20,"resets_at":"%s"}]}]}\\n' "$now" "$five" "$week"
`);
await writeFile(join(fakeBin, "gh"), `#!/bin/sh
counter="\${JARVISOS_PROVIDER_QUOTA_GH_COUNTER}"
if [ -e "$counter" ]; then
  exit 1
fi
touch "$counter"
reset=$(date -u -d 'next month' +%Y-%m-01)
printf '{"cli_enabled":true,"quota_reset_date":"%s","quota_snapshots":{"premium_interactions":{"unlimited":false,"has_quota":true,"percent_remaining":75,"remaining":225,"entitlement":300}}}\\n' "$reset"
`);
spawnSync("chmod", ["+x", join(fakeBin, "agent-relay"), join(fakeBin, "gh")]);

const env = {
  ...process.env,
  PYTHONPATH: backend,
  JARVISOS_DATA_ROOT: dataRoot,
  JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: continuityDir,
  JARVIS_RELAY_REPO: relayRepo,
  XDG_STATE_HOME: join(dataRoot, "host"),
  LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_AGENT_RELAY_BIN: join(fakeBin, "agent-relay"),
  JARVISOS_GH_BIN: join(fakeBin, "gh"),
  JARVISOS_PROVIDER_QUOTA_TTL_S: "3600",
  JARVISOS_PROVIDER_QUOTA_GH_COUNTER: join(dataRoot, "gh-calls"),
  JARVISOS_AGY_BIN: "/nonexistent/agy",
  JARVISOS_CODEX_BIN: "/nonexistent/codex",
};
delete env.JARVISOS_BLUECAD_TOOL_REGISTRY;
delete env.GH_TOKEN;
delete env.GITHUB_TOKEN;
delete env.JARVISOS_GITHUB_READ_TOKEN;
delete env.JARVISOS_COPILOT_GH_TOKEN;

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
].join("; ")], { cwd: backend, env, encoding: "utf8" });
assert(init.status === 0, `isolated database initialization failed: ${init.stderr}`);

const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], {
  cwd: backend, env, stdio: ["ignore", log, log],
});
const summary = {
  spec: "provider-quota",
  head,
  dirty,
  browser: null,
  started_at: new Date().toISOString(),
  data_root: "isolated temporary JARVISOS_DATA_ROOT + fake provider CLIs",
  console_errors: [],
  page_errors: [],
  screenshots: [],
  checks: [],
};
const check = async (name, action) => {
  try {
    await action();
    summary.checks.push({ name, status: "pass" });
  } catch (error) {
    summary.checks.push({ name, status: "fail", error: String(error) });
    throw error;
  }
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
  page.on("pageerror", (error) => summary.page_errors.push(String(error)));
  const shot = async (name) => {
    const file = join(evidence, `${String(summary.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
    await page.screenshot({ path: file, fullPage: true });
    summary.screenshots.push({
      file: file.slice(evidence.length + 1),
      sha256: createHash("sha256").update(await readFile(file)).digest("hex"),
    });
  };

  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  const createBtn = page.getByRole("button", { name: "Create workspace", exact: true });
  if (await createBtn.isVisible()) {
    await page.getByLabel("Workspace name").fill("Provider Quota Proof Workspace");
    await createBtn.click();
  }
  await page.getByRole("navigation", { name: "Primary navigation" })
    .getByRole("link", { name: "Coding", exact: true }).click();
  await page.getByRole("navigation", { name: "Coding destinations" })
    .getByRole("link", { name: "Runtime", exact: true }).click();

  const panel = page.getByRole("region", { name: "Provider usage" });
  await panel.waitFor();
  await shot("provider-usage-before-assertions");
  await panel.locator(".coding-quota-provider").filter({ hasText: "GitHub Copilot" }).waitFor();
  await check("four provider column headers", async () => {
    const headers = (await panel.locator(".coding-quota-provider").allTextContents()).map((text) => text.replace(/\s+/g, " ").trim());
    for (const label of ["Claude", "Codex (ChatGPT)", "Antigravity", "GitHub Copilot"]) {
      assert(headers.some((header) => header.startsWith(label)), `missing provider header: ${label}`);
    }
  });
  await check("Copilot shows 75% remaining ring", async () => {
    await panel.getByRole("group", { name: "Monthly premium requests" })
      .getByRole("img", { name: "75% left" }).waitFor();
  });
  await check("Claude shows both usage windows", async () => {
    await panel.getByRole("group", { name: "5-hour" })
      .locator('[data-provider="claude"]').getByRole("img", { name: "60% left" }).waitFor();
    await panel.getByRole("group", { name: "Weekly" })
      .locator('[data-provider="claude"]').getByRole("img", { name: "80% left" }).waitFor();
  });
  await shot("provider-usage-initial");

  const refresh = panel.getByRole("button", { name: /Refresh usage/ });
  await refresh.click();
  await panel.getByText(/refresh failed/).waitFor();
  await check("failed refresh preserves Copilot 75% reading", async () => {
    await panel.getByRole("group", { name: "Monthly premium requests" })
      .getByRole("img", { name: "75% left" }).waitFor();
    await panel.getByText(/refresh failed/).waitFor();
  });
  await shot("provider-usage-refresh-failed");
  assert(summary.console_errors.length === 0, `console errors: ${JSON.stringify(summary.console_errors)}`);
  assert(summary.page_errors.length === 0, `page errors: ${JSON.stringify(summary.page_errors)}`);
  summary.passed = summary.checks.length === 4 && summary.checks.every((item) => item.status === "pass");
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
console.log(JSON.stringify({ head, browser: summary.browser, passed: summary.passed, checks: summary.checks }));
