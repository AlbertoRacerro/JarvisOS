#!/usr/bin/env node
// Spec 304: Browser walk of the "Prontezza" readiness board under Development.
// Criteria 2-6:
// 2. From `/` in a clean browser profile, Development -> Prontezza opens the board using visible controls only.
// 3. With fake pack and fresh data root, the case row shows seven red cells, summary reads 0/7, leftmost red cell names case and Articolo.
// 4. After creating zephyr workspace and literature source with readable paper file, reload shows Articolo green and Dati red listing missing quantity names.
// 5. Red Dati cell link opens Literature with case workspace selected.
// 6. Confronto shows "Funzione non ancora disponibile nel prodotto" and no link.

import { createHash } from "node:crypto";
import { openSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/readiness_board_walk.mjs <evidence-dir>");
  process.exit(0);
}

const pw = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const chromium = pw.chromium ?? pw.default?.chromium;
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8314);
const base = `http://127.0.0.1:${port}`;

if (!process.argv[2]) throw new Error("usage: readiness_board_walk.mjs <evidence-dir>");
const evidence = resolve(process.argv[2]);
const assert = (ok, message) => {
  if (!ok) throw new Error(message);
};
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-304-data-"));
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

// Initialize isolated database with default workspace (zephyr case workspace not created yet)
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
  spec: "304",
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

  // 1. Criterion 2: From `/` in a clean browser profile, Development -> Prontezza opens the board using visible controls only.
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  await page.getByRole("link", { name: "Development", exact: true }).click();
  await page.getByRole("link", { name: "Prontezza", exact: true }).click();
  await page.locator(".readiness-board").waitFor();
  await shot("prontezza-initial-from-root");
  summary.steps.push({ step: "open_prontezza_from_root_via_visible_controls", status: "pass" });

  // 2. Criterion 3: With fake pack and fresh data root, 7 red cells, summary 0/7, leftmost red cell names case and Articolo.
  const summaryEl = page.locator(".readiness-board__summary");
  await summaryEl.waitFor();
  const summaryText = await summaryEl.innerText();
  assert(summaryText.includes("0/7"), `expected 0/7 in summary, got: ${summaryText}`);
  assert(
    summaryText.includes("Generic Literature Case") && summaryText.includes("Articolo"),
    `expected leftmost red cell naming case and Articolo, got: ${summaryText}`
  );
  const redCells = page.locator('.readiness-board__cell-button[data-status="red"]');
  const countRed = await redCells.count();
  assert(countRed === 7, `expected 7 red cells, got ${countRed}`);
  await shot("fresh-data-root-seven-red-cells");
  summary.steps.push({ step: "fresh_data_root_shows_seven_red_cells_and_summary_0_7", status: "pass" });

  // 3. Criterion 4: Create zephyr workspace and literature source with readable paper file via existing routes;
  // reload shows Articolo green and Dati red listing missing quantity names.
  const post = async (path, body) => {
    const res = await fetch(`${base}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!res.ok) throw new Error(`${path}: ${res.status} ${await res.text()}`);
    return res.json();
  };

  const ws = await post("/workspaces", { name: "Zephyr", slug: "zephyr" });
  const paperContent =
    "# Benchmark Validation Study\n\nLine 2: density is 1000 kg/m^3\nLine 3: conductivity is 0.6 W/(m·K)\n";
  const fileUploadRes = await fetch(`${base}/workspaces/${ws.id}/literature/files?filename=paper.md`, {
    method: "POST",
    headers: { "content-type": "text/markdown" },
    body: paperContent
  });
  assert(fileUploadRes.ok, `file upload failed: ${fileUploadRes.status}`);
  const fileUploadData = await fileUploadRes.json();

  await post(`/workspaces/${ws.id}/literature/sources`, {
    title: "Benchmark Validation Study",
    source_kind: "paper",
    state: "accepted",
    artifact_id: fileUploadData.artifact_id
  });

  // Reload the Prontezza screen
  await page.reload({ waitUntil: "networkidle" });
  await page.locator('.readiness-board__cell-button[data-status="green"]').waitFor();

  const paperCell = page.locator('.readiness-board__cell-button[aria-label*="Articolo"]').first();
  assert((await paperCell.getAttribute("data-status")) === "green", "Articolo cell is green");

  const factsCell = page.locator('.readiness-board__cell-button[aria-label*="Dati"]').first();
  assert((await factsCell.getAttribute("data-status")) === "red", "Dati cell is red");

  await factsCell.click();
  const detailEl = page.locator(".readiness-board__detail");
  await detailEl.waitFor();
  await detailEl.locator(".readiness-board__missing-list", { hasText: "density" }).waitFor();
  await detailEl.locator(".readiness-board__missing-list", { hasText: "thermal_conductivity" }).waitFor();
  await shot("paper-green-facts-red-missing-quantities");
  summary.steps.push({ step: "reload_shows_paper_green_and_facts_red_with_missing_quantities", status: "pass" });

  // 4. Criterion 5: Red Dati cell link opens Literature with case workspace selected.
  const litLink = detailEl.getByRole("link", { name: "Letteratura", exact: false });
  await litLink.waitFor();
  await litLink.click();
  await page.waitForURL("**/memory/literature*");
  await page.getByRole("heading", { name: "Project knowledge", level: 1 }).waitFor();
  // The source exists only in the case workspace, so seeing it proves the link selected that workspace.
  await page.getByText("Benchmark Validation Study").first().waitFor();
  await shot("opened-literature-from-red-dati-cell");
  summary.steps.push({ step: "red_dati_cell_link_opens_literature_with_case_workspace", status: "pass" });

  // 5. Criterion 6: Confronto shows "Funzione non ancora disponibile nel prodotto" and no link.
  await page.getByRole("link", { name: "Development", exact: true }).click();
  await page.getByRole("link", { name: "Prontezza", exact: true }).click();
  await page.locator(".readiness-board").waitFor();

  const compareCell = page.locator('.readiness-board__cell-button[aria-label*="Confronto"]').first();
  await compareCell.click();
  const compareDetail = page.locator(".readiness-board__detail");
  await compareDetail.getByText("Funzione non ancora disponibile nel prodotto").waitFor();
  const linksInCompare = compareDetail.getByRole("link");
  assert((await linksInCompare.count()) === 0, "Confronto detail must have no link");
  await shot("compare-capability-missing-no-link");
  summary.steps.push({ step: "compare_shows_capability_missing_and_no_link", status: "pass" });

  assert(summary.console_errors.length === 0, `console errors: ${JSON.stringify(summary.console_errors)}`);
  assert(summary.page_errors.length === 0, `page errors: ${JSON.stringify(summary.page_errors)}`);
  summary.passed = summary.steps.length === 5 && summary.steps.every((s) => s.status === "pass");
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
