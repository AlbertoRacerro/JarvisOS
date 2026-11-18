#!/usr/bin/env node
// Spec 312: Guided question flow walk in Design (GUIDED-FLOW-1)
// Walks from `/`, opens Design, creates a question, verifies the 7 steps and
// "Prossimo passo" card, toggles Libero / Guidato, and clicks a step to navigate,
// with no console or page errors.

import { createHash } from "node:crypto";
import { openSync } from "node:fs";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/guided_flow_walk.mjs <evidence-dir> [<data-root-copy>]");
  process.exit(0);
}

if (!process.argv[2]) {
  throw new Error("usage: node e2e/guided_flow_walk.mjs <evidence-dir> [<data-root-copy>]");
}

let pw;
const defaultCandidates = [
  process.env.PLAYWRIGHT_MODULE,
  "playwright",
  "/home/thera/jarvis-control/work/tools/pw/node_modules/playwright/index.mjs",
  "/home/thera/.npm/_npx/fd3bca3c548369c0/node_modules/playwright/index.js",
].filter(Boolean);

for (const candidate of defaultCandidates) {
  try {
    pw = await import(candidate);
    break;
  } catch {
    // try next candidate
  }
}

if (!pw) throw new Error("playwright could not be loaded");
const chromium = pw.chromium ?? pw.default?.chromium;

const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8312);
const base = `http://127.0.0.1:${port}`;

if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) {
  throw new Error("Invalid proof port");
}

await new Promise((ok, fail) => {
  const probe = createServer()
    .once("error", (error) => fail(new Error(`port ${port} is in use: ${error.message}`)))
    .once("listening", () => probe.close(ok))
    .listen(port, "127.0.0.1");
});

const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });

const sourceRoot = process.argv[3] ? resolve(process.argv[3]) : null;
const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-312-data-"));
const controlDir = join(dataRoot, "control");
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });

const env = {
  ...process.env,
  PYTHONPATH: backend,
  JARVISOS_DATA_ROOT: dataRoot,
  JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: join(controlDir, "continuity"),
  JARVIS_RELAY_REPO: join(dataRoot, "relay-repo"),
  XDG_STATE_HOME: join(dataRoot, "host"),
  LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_AGY_BIN: "/nonexistent/agy",
  JARVISOS_CODEX_BIN: "/nonexistent/codex",
  JARVISOS_AGENT_RELAY_BIN: "/nonexistent/agent-relay",
};
delete env.GH_TOKEN;
delete env.GITHUB_TOKEN;
delete env.JARVISOS_GITHUB_READ_TOKEN;

if (sourceRoot) {
  await cp(sourceRoot, dataRoot, {
    recursive: true,
    filter: (p) => !/jarvisos\.db(-wal|-shm|-journal)?$/.test(p),
  });
  const snapshot = spawnSync(
    python,
    [
      "-I",
      "-c",
      [
        "import sqlite3,sys",
        "src=sqlite3.connect('file:'+sys.argv[2]+'/jarvisos.db?mode=ro',uri=True); dst=sqlite3.connect(sys.argv[1]+'/jarvisos.db'); src.backup(dst)",
        "n=dst.execute(\"UPDATE artifacts SET stored_path=? || substr(stored_path, ?) WHERE stored_path LIKE ? || '%'\",(sys.argv[1],len(sys.argv[2])+1,sys.argv[2])).rowcount",
        "dst.commit(); print(n)",
      ].join("\n"),
      dataRoot,
      sourceRoot,
    ],
    { encoding: "utf8" }
  );
  if (snapshot.status !== 0) throw new Error(`snapshot failed: ${snapshot.stderr}`);
} else {
  const init = spawnSync(
    python,
    [
      "-c",
      "from app.core.bootstrap import initialize_storage\ninitialize_storage(seed_default=True)",
    ],
    { cwd: backend, env, encoding: "utf8" }
  );
  if (init.status !== 0) throw new Error(`storage initialization failed: ${init.stderr}`);
}

const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(
  python,
  ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)],
  {
    cwd: backend,
    env,
    stdio: ["ignore", log, log],
  }
);

const summary = {
  spec: "312",
  head,
  browser: null,
  started_at: new Date().toISOString(),
  console_errors: [],
  page_errors: [],
  screenshots: [],
  steps: [],
};

let browser;
try {
  for (let i = 0; ; i++) {
    if (server.exitCode !== null) throw new Error("backend exited prematurely");
    try {
      if ((await fetch(`${base}/health`)).ok) break;
    } catch {
      /* wait */
    }
    if (i > 120) throw new Error("backend did not become healthy in time");
    await delay(500);
  }

  browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({
    viewport: { width: 1440, height: 960 },
    colorScheme: "light",
  });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);

  page.on("console", (m) => {
    if (m.type() === "error") summary.console_errors.push(m.text());
  });
  page.on("pageerror", (e) => summary.page_errors.push(String(e)));

  const shot = async (name) => {
    const file = join(evidence, `${String(summary.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
    await page.screenshot({ path: file, fullPage: true });
    summary.screenshots.push({
      file: file.slice(evidence.length + 1),
      sha256: createHash("sha256").update(await readFile(file)).digest("hex"),
    });
  };

  // 1. Open root
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  await shot("01-root");
  summary.steps.push({ step: "open_root", status: "pass" });

  // 2. Open Design
  await page
    .getByRole("navigation", { name: "Primary navigation" })
    .getByRole("link", { name: "Design", exact: true })
    .click();
  await page.locator(".design-project").waitFor();
  await page.locator(".guided-flow-bar").waitFor();
  await shot("02-design-screen");
  summary.steps.push({ step: "open_design_screen", status: "pass" });

  // 3. Verify GuidedFlowBar initial elements
  const guidatoBtn = page.getByRole("radio", { name: "Guidato" });
  const liberoBtn = page.getByRole("radio", { name: "Libero" });
  await guidatoBtn.waitFor();
  await liberoBtn.waitFor();

  const isGuidato = await guidatoBtn.getAttribute("aria-checked");
  if (isGuidato !== "true") {
    throw new Error(`Expected Guidato mode to be default active, got ${isGuidato}`);
  }

  // Verify 7 steps visible
  const expectedSteps = [
    "Inquadra",
    "Fonti",
    "Modello",
    "Calcolo",
    "Esplora",
    "Confronta",
    "Decidi",
  ];
  for (const stepName of expectedSteps) {
    await page.locator(".guided-flow-bar__step-label", { hasText: stepName }).waitFor();
  }

  // Verify Prossimo passo card is visible
  await page.locator(".guided-flow-bar__next-card").waitFor();
  await shot("03-guided-bar-initial");
  summary.steps.push({ step: "verify_initial_guided_bar", status: "pass" });

  // 4. Create a question
  await page.locator(".guided-flow-bar__new-btn").click();
  const titleInput = page.locator('.guided-flow-bar__create-form input[type="text"]').first();
  await titleInput.waitFor();
  await titleInput.fill("Come ottimizzare lo scambiatore?");

  const whatChangesInput = page.locator('.guided-flow-bar__create-form input[type="text"]').nth(1);
  await whatChangesInput.fill("Aumento superficie alettata");

  await shot("04-question-form");
  await page.getByRole("button", { name: "Crea domanda" }).click();

  // Wait for question card to appear
  const questionCard = page.locator(".guided-flow-bar__question-card", {
    hasText: "Come ottimizzare lo scambiatore?",
  });
  await questionCard.waitFor();
  await shot("05-question-created");
  summary.steps.push({ step: "create_question", status: "pass" });

  // 5. Verify the 7 steps and Prossimo passo card for the created question
  const frameStep = page.locator('button.guided-flow-bar__step-node[data-step="frame"]');
  await frameStep.waitFor();
  const nextBtn = page.locator(".guided-flow-bar__next-btn");
  await nextBtn.waitFor();
  await shot("06-seven-steps-and-prossimo-card");
  summary.steps.push({ step: "verify_seven_steps_and_prossimo_card", status: "pass" });

  // 6. Toggle Libero mode
  await liberoBtn.click();
  await delay(300);
  // Path bar and questions should not be visible in Libero mode
  const pathBarCount = await page.locator(".guided-flow-bar__path-bar").count();
  if (pathBarCount !== 0) {
    throw new Error("Path bar should be hidden in Libero mode");
  }
  await shot("07-mode-libero");
  summary.steps.push({ step: "toggle_mode_libero", status: "pass" });

  // 7. Toggle back to Guidato mode
  await guidatoBtn.click();
  await delay(300);
  await page.locator(".guided-flow-bar__path-bar").waitFor();
  await questionCard.waitFor();
  await shot("08-mode-guidato-restored");
  summary.steps.push({ step: "toggle_mode_guidato_restored", status: "pass" });

  // 8. Click a step to navigate (click Inquadra)
  await frameStep.click();
  await page.waitForURL("**/memory/project-basis");
  await shot("09-navigated-to-step");
  summary.steps.push({ step: "navigate_via_step_click", status: "pass" });

  if (summary.console_errors.length > 0) {
    throw new Error(`Console errors during walk: ${JSON.stringify(summary.console_errors)}`);
  }
  if (summary.page_errors.length > 0) {
    throw new Error(`Page errors during walk: ${JSON.stringify(summary.page_errors)}`);
  }

  summary.completed_at = new Date().toISOString();
  await writeFile(join(evidence, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
  console.log(`WALK PASS: ${evidence}`);
} finally {
  if (browser) await browser.close();
  server.kill("SIGTERM");
  await delay(500);
  try {
    await rm(dataRoot, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
}
