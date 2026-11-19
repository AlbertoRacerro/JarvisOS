#!/usr/bin/env node
// Spec 314: Browser walk of stored case comparison and verdict product object (CASE-VERDICT-1).
// Walk criteria:
// 1. From `/` in a clean browser profile, opens Prontezza via visible controls.
// 2. Confronto cell shows comparison details for a usable bench run.
// 3. Clicks "Registra verdetto" to record a deterministic verdict ("Entro tolleranza", "Registrato").
// 4. Enters rationale and clicks "Accetta verdetto", updating status to "Accettato".
// 5. Reloads page to confirm persistence of the accepted verdict.
// 6. Spec 318: opens the Decisione cell, fills the decision form, clicks "Registra decisione"; the cell turns green
//    and stays green after a reload.
// 7. Confirms zero console errors and zero page errors throughout.

import { createHash } from "node:crypto";
import { openSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/case_verdict_walk.mjs <evidence-dir>");
  process.exit(0);
}

if (!process.argv[2]) {
  throw new Error("usage: case_verdict_walk.mjs <evidence-dir>");
}

const pw = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const chromium = pw.chromium ?? pw.default?.chromium;
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8314);
const base = `http://127.0.0.1:${port}`;

if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) {
  throw new Error("Invalid proof port: cannot be 8000");
}

const evidence = resolve(process.argv[2]);
const assert = (ok, message) => {
  if (!ok) throw new Error(message);
};
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-314-data-"));
const controlDir = join(dataRoot, "control");
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });

const zephyrPack = join(backend, "tests/fixtures/packs/zephyr");

const env = {
  ...process.env,
  PYTHONPATH: backend,
  JARVISOS_DATA_ROOT: dataRoot,
  JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: join(controlDir, "continuity"),
  JARVIS_RELAY_REPO: join(dataRoot, "relay-repo"),
  XDG_STATE_HOME: join(dataRoot, "host"),
  LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_PACK_PATH: zephyrPack,
  JARVISOS_AGY_BIN: "/nonexistent/agy",
  JARVISOS_CODEX_BIN: "/nonexistent/codex",
  JARVISOS_AGENT_RELAY_BIN: "/nonexistent/agent-relay"
};
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

// Initialize isolated database with default workspace
const init = spawnSync(
  python,
  ["-c", "from app.core.bootstrap import initialize_storage\ninitialize_storage(seed_default=True)"],
  { cwd: backend, env, encoding: "utf8" }
);
assert(init.status === 0, `isolated database initialization failed: ${init.stderr}`);

const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], {
  cwd: backend,
  env,
  stdio: ["ignore", log, log]
});

const summary = {
  spec: "314",
  head,
  browser: null,
  started_at: new Date().toISOString(),
  console_errors: [],
  page_errors: [],
  screenshots: [],
  steps: []
};

let browser;
try {
  for (let i = 0; ; i++) {
    assert(server.exitCode === null, "backend exited prematurely");
    try {
      if ((await fetch(`${base}/health`)).ok) break;
    } catch {
      /* wait */
    }
    assert(i < 120, "backend did not become healthy in time");
    await delay(500);
  }

  browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: "light" });
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
      sha256: createHash("sha256").update(await readFile(file)).digest("hex")
    });
  };

  // Helper to post JSON
  const post = async (path, body) => {
    const res = await fetch(`${base}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`);
    return res.json();
  };

  // 1. Create zephyr workspace and fixture bench run with usable density output
  const ws = await post("/workspaces", { name: "Zephyr", slug: "zephyr" });
  const benchRunDir = join(dataRoot, "workspaces", ws.id, "engineering", "bench", "run_walk_314");
  await mkdir(benchRunDir, { recursive: true });
  const runResult = {
    run_id: "run_walk_314",
    evaluator_id: "evaluator.thermal",
    status: "complete",
    verdict: "usable",
    completed_at: new Date().toISOString(),
    outputs: [
      {
        name: "density",
        value: {
          value: 1.0,
          unit: "g/cm3"
        }
      }
    ]
  };
  await writeFile(join(benchRunDir, "result.json"), JSON.stringify(runResult, null, 2));

  // 2. Open Prontezza from root `/` via visible controls only
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  await page.getByRole("link", { name: "Development", exact: true }).click();
  await page.getByRole("link", { name: "Prontezza", exact: true }).click();
  await page.locator(".readiness-board").waitFor();
  await shot("prontezza-board-opened");
  summary.steps.push({ step: "open_prontezza_from_root_via_visible_controls", status: "pass" });

  // 3. Click Confronto cell to open detail panel
  const compareCell = page.locator('.readiness-board__cell-button[aria-label*="Confronto"]').first();
  await compareCell.waitFor();
  await compareCell.click();

  const detailEl = page.locator(".readiness-board__detail");
  await detailEl.waitFor();
  await detailEl.getByText("Calcolato").waitFor();
  await detailEl.getByText("Pubblicato").waitFor();
  await detailEl.getByText("Scarto").waitFor();

  // Verify "Registra verdetto" button is visible
  const recordBtn = detailEl.locator(".readiness-board__record-verdict-button");
  await recordBtn.waitFor();
  assert((await recordBtn.innerText()).includes("Registra verdetto"), "button has text 'Registra verdetto'");
  await shot("confronto-open-record-verdict-button-visible");
  summary.steps.push({ step: "confronto_detail_shows_record_verdict_button", status: "pass" });

  // 4. Click "Registra verdetto" to record verdict
  await recordBtn.click();

  // Verify badge "Entro tolleranza" and status "Registrato"
  const verdictBadge = detailEl.locator(".readiness-board__verdict-badge");
  await verdictBadge.waitFor();
  assert((await verdictBadge.innerText()).includes("Entro tolleranza"), "badge has text 'Entro tolleranza'");

  const verdictStatus = detailEl.locator(".readiness-board__verdict-status");
  await verdictStatus.waitFor();
  assert((await verdictStatus.innerText()).includes("Registrato"), "status has text 'Registrato'");

  // Verify rationale input and "Accetta verdetto" button
  const rationaleInput = detailEl.locator(".readiness-board__rationale-input");
  await rationaleInput.waitFor();
  const acceptBtn = detailEl.locator(".readiness-board__accept-verdict-button");
  await acceptBtn.waitFor();
  assert((await acceptBtn.innerText()).includes("Accetta verdetto"), "button has text 'Accetta verdetto'");
  await shot("verdict-recorded-shows-badge-and-accept-button");
  summary.steps.push({ step: "record_verdict_displays_badge_status_and_accept_button", status: "pass" });

  // 5. Enter rationale and click "Accetta verdetto"
  const walkRationale = "Confronto verificato nel browser walk e2e 314";
  await rationaleInput.fill(walkRationale);
  await acceptBtn.click();

  // Verify status updates to "Accettato"
  await page.locator('.readiness-board__verdict-status[data-status="accepted"]').waitFor();
  assert((await verdictStatus.innerText()).includes("Accettato"), "status updated to 'Accettato'");
  await shot("verdict-accepted");
  summary.steps.push({ step: "accept_verdict_updates_status_to_accettato", status: "pass" });

  // 6. Reload page to confirm persistence of the accepted verdict
  await page.reload({ waitUntil: "networkidle" });
  await page.locator(".readiness-board").waitFor();

  const compareCellAfterReload = page.locator('.readiness-board__cell-button[aria-label*="Confronto"]').first();
  await compareCellAfterReload.waitFor();
  await compareCellAfterReload.click();

  const detailAfterReload = page.locator(".readiness-board__detail");
  await detailAfterReload.waitFor();

  const badgeAfterReload = detailAfterReload.locator(".readiness-board__verdict-badge");
  await badgeAfterReload.waitFor();
  assert((await badgeAfterReload.innerText()).includes("Entro tolleranza"), "persisted badge shows 'Entro tolleranza'");

  const statusAfterReload = detailAfterReload.locator('.readiness-board__verdict-status[data-status="accepted"]');
  await statusAfterReload.waitFor();
  assert((await statusAfterReload.innerText()).includes("Accettato"), "persisted status shows 'Accettato'");

  const rationaleDisplay = detailAfterReload.locator(".readiness-board__verdict-rationale-display");
  await rationaleDisplay.waitFor();
  assert((await rationaleDisplay.innerText()).includes(walkRationale), "persisted rationale displayed");
  await shot("reloaded-verdict-persisted");
  summary.steps.push({ step: "reload_confirms_verdict_persistence", status: "pass" });

  // 6. Spec 318: record a decision on the accepted verdict from the Decisione cell
  const decideCell = page.locator('.readiness-board__cell-button[aria-label*="Decisione"]').first();
  await decideCell.click();
  const decisionForm = page.locator(".readiness-board__decision-form");
  await decisionForm.waitFor();
  await decisionForm.getByLabel("Titolo").fill("Walk decision");
  await decisionForm.getByLabel("Decisione", { exact: true }).fill("The model reproduces the published value.");
  await decisionForm.getByLabel("Motivazione (opzionale)").fill("Accepted verdict within tolerance.");
  await shot("decision-form-filled");
  await decisionForm.getByRole("button", { name: "Registra decisione" }).click();
  await page.locator('.readiness-board__cell-button[aria-label*="Decisione"][data-status="green"]').first().waitFor();
  await page.reload({ waitUntil: "networkidle" });
  await page.locator('.readiness-board__cell-button[aria-label*="Decisione"][data-status="green"]').first().waitFor();
  await shot("decision-cell-green-after-reload");
  summary.steps.push({ step: "record_decision_turns_decide_cell_green", status: "pass" });

  assert(summary.console_errors.length === 0, `console errors: ${JSON.stringify(summary.console_errors)}`);
  assert(summary.page_errors.length === 0, `page errors: ${JSON.stringify(summary.page_errors)}`);
  summary.passed = summary.steps.length === 6 && summary.steps.every((s) => s.status === "pass");
  await context.close();
} catch (error) {
  summary.failure = String(error.stack ?? error);
  summary.passed = false;
  throw error;
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  for (let i = 0; i < 20 && server.exitCode === null; i++) await delay(250);
  if (server.exitCode === null) server.kill("SIGKILL");
  await rm(dataRoot, { recursive: true, force: true }).catch(() => {});
  summary.finished_at = new Date().toISOString();
  await writeFile(join(evidence, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
}

console.log(JSON.stringify({ head, browser: summary.browser, passed: summary.passed, steps: summary.steps }));
