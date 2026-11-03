#!/usr/bin/env node
// Spec 240: real-Chromium walk of Coding → Runtime supervisor controls and open maintainer question answers.
// Starts at `/` in a clean profile, navigates via visible controls only, and exercises pause, resume,
// graceful restart, and answering an open maintainer question against an isolated continuity/repo store.
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/coding_supervisor_walk.mjs <evidence-dir>
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/coding_supervisor_walk.mjs <evidence-dir>  (needs built frontend, Playwright)");
  process.exit(0);
}
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8240);
const base = `http://127.0.0.1:${port}`;
const evidence = resolve(process.argv[2] ?? "");
if (!process.argv[2]) throw new Error("usage: coding_supervisor_walk.mjs <evidence-dir>");
const assert = (ok, message) => { if (!ok) throw new Error(message); };
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-240-data-"));
const controlDir = join(dataRoot, "control");
const continuityDir = join(controlDir, "continuity");
const relayRepo = join(dataRoot, "relay-repo");
await mkdir(continuityDir, { recursive: true });
await mkdir(join(relayRepo, ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });

const questionId = "L20261103160000-240a";
const seedQuestion = {
  schema: "devctx.v1",
  id: questionId,
  ts: "2026-11-03T16:00:00Z",
  kind: "question",
  status: null,
  author: "argon",
  refs: {},
  conflicts: [],
  supersedes: [],
  text: "Quale modalità preferisci per il controllo supervisor?\n1. (consigliata) Controlli diretti in UI — Conseguenza: Pausa, ripresa e risposta immediata da Coding → Runtime.\n2. Solo CLI — Conseguenza: Richiede accesso al terminale.",
};
await writeFile(join(controlDir, "ledger.jsonl"), `${JSON.stringify(seedQuestion)}\n`);
await writeFile(
  join(continuityDir, "roadmap.json"),
  JSON.stringify({
    schema: 1,
    enabled: true,
    authority: "L20261103141101-7c11",
    started_at: "2026-11-03T15:00:00Z",
    cycle: 284,
    paused: null,
    paused_at: null,
    no_progress: 0,
  }, null, 2) + "\n",
);

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
  spec: "240",
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

  // 1. Start at `/` in a clean profile, create the initial workspace if prompted, and navigate to Coding -> Runtime via visible controls only.
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  const createBtn = page.getByRole("button", { name: "Create workspace", exact: true });
  if (await createBtn.isVisible()) {
    await page.getByLabel("Workspace name").fill("Supervisor Proof Workspace");
    await createBtn.click();
  }
  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Coding", exact: true }).click();
  await page.getByRole("navigation", { name: "Coding destinations" }).getByRole("link", { name: "Runtime", exact: true }).click();

  const monitor = page.getByRole("region", { name: "Relay owner" });
  const controls = monitor.getByRole("group", { name: "Supervisor controls" });
  const questions = monitor.getByRole("region", { name: "Open maintainer questions" });
  await controls.waitFor();
  await questions.waitFor();
  await page.getByText("Roadmap cycle 284 · active").waitFor();
  await questions.getByText(/Quale modalità preferisci per il controllo supervisor\?/).waitFor();
  await shot("runtime-initial-active-and-question");
  summary.steps.push({ step: "reach_runtime_from_root_and_render_controls", status: "pass" });

  // 2. Click Pause supervisor and verify state + on-disk roadmap.json
  const pauseBtn = controls.getByRole("button", { name: "Pause supervisor", exact: true });
  const resumeBtn = controls.getByRole("button", { name: "Resume supervisor", exact: true });
  const restartBtn = controls.getByRole("button", { name: "Graceful restart", exact: true });
  await pauseBtn.click();
  await controls.getByText("Paused (maintainer stop)").waitFor();
  assert(await pauseBtn.isDisabled(), "Pause button should be disabled when already paused with maintainer_stop");
  const pausedRoadmap = JSON.parse(await readFile(join(continuityDir, "roadmap.json"), "utf8"));
  assert(pausedRoadmap.paused === "maintainer_stop", `expected maintainer_stop on disk, got ${pausedRoadmap.paused}`);
  await shot("supervisor-paused");
  summary.steps.push({ step: "pause_supervisor", status: "pass", paused_on_disk: pausedRoadmap.paused });

  // 3. Click Resume supervisor and verify state + on-disk roadmap.json
  await resumeBtn.click();
  await controls.getByText("Roadmap cycle 284 · active").waitFor();
  const resumedRoadmap = JSON.parse(await readFile(join(continuityDir, "roadmap.json"), "utf8"));
  assert(resumedRoadmap.paused === null, `expected paused=null on disk, got ${resumedRoadmap.paused}`);
  await shot("supervisor-resumed");
  summary.steps.push({ step: "resume_supervisor", status: "pass", paused_on_disk: resumedRoadmap.paused });

  // 4. Click Graceful restart and verify STOP file creation, then Resume to clear STOP
  await restartBtn.click();
  await controls.getByText("Graceful stop requested").waitFor();
  const stopText = (await readFile(join(continuityDir, "STOP"), "utf8")).trim();
  assert(stopText.length > 0, "STOP file was not written");
  await shot("supervisor-graceful-restart-requested");
  await resumeBtn.click();
  await controls.getByText("Roadmap cycle 284 · active").waitFor();
  summary.steps.push({ step: "graceful_restart_and_resume", status: "pass", stop_timestamp: stopText });

  // 5. Answer the open maintainer question by clicking the recommended option button
  const optionBtn = questions.getByRole("button", { name: /Controlli diretti in UI \(consigliata\)/ });
  await optionBtn.click();
  await questions.waitFor({ state: "detached" });
  const ledgerLines = (await readFile(join(controlDir, "ledger.jsonl"), "utf8"))
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  const decision = ledgerLines.find((entry) => entry.kind === "decision" && (entry.supersedes ?? []).includes(questionId));
  assert(decision, "decision entry superseding the question was not written to ledger");
  await shot("question-answered-and-cleared");
  summary.steps.push({ step: "answer_open_question_via_option_button", status: "pass", decision_id: decision.id });

  assert(summary.console_errors.length === 0, `console errors: ${JSON.stringify(summary.console_errors)}`);
  assert(summary.page_errors.length === 0, `page errors: ${JSON.stringify(summary.page_errors)}`);
  summary.passed = summary.steps.length === 5 && summary.steps.every((s) => s.status === "pass");
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
