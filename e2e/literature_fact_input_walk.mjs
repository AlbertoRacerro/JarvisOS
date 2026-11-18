#!/usr/bin/env node
// Spec 311: Browser walk of accepting a checked datum as a named fact and using it as a parameter.
// Criteria 7:
// Walk from `/` with a temporary data root: open Literature, register a text source with a file,
// record a located datum with a quantity name, accept it, use it as a parameter, see the parameter
// under "Used by"; then open Development → Prontezza and, for a seeded case naming that quantity,
// see "Dati" and "Ingressi legati" green; no console or page errors.

import { createHash } from "node:crypto";
import { openSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/literature_fact_input_walk.mjs <evidence-dir>");
  process.exit(0);
}

if (!process.argv[2]) throw new Error("usage: literature_fact_input_walk.mjs <evidence-dir>");

let pw;
try {
  pw = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
} catch (err) {
  if (process.env.PLAYWRIGHT_MODULE) {
    pw = await import(`${process.env.PLAYWRIGHT_MODULE}/index.js`);
  } else {
    throw err;
  }
}
const chromium = pw.chromium ?? pw.default?.chromium;
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8311);
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

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-311-data-"));
const controlDir = join(dataRoot, "control");
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });

// Seeded pack directory with readiness case matching our source and datum
const packDir = await mkdtemp(join(tmpdir(), "jarvisos-311-pack-"));
await writeFile(
  join(packDir, "jarvis_pack.toml"),
  'id = "walk311"\n\n[workspace]\nslug = "walk311"\nname = "Walk 311 Workspace"\n\n[readiness]\ncases = "readiness_cases.toml"\n',
  "utf8"
);
await writeFile(
  join(packDir, "readiness_cases.toml"),
  '[[cases]]\nid = "walk-case-1"\ntitle = "Walk 311 Case"\nworkspace = "walk311"\nsource_title = "Rate study"\nfacts = ["k_rate"]\ninputs = ["k_param"]\n',
  "utf8"
);

const env = {
  ...process.env,
  PYTHONPATH: backend,
  JARVISOS_DATA_ROOT: dataRoot,
  JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: join(controlDir, "continuity"),
  JARVIS_RELAY_REPO: join(dataRoot, "relay-repo"),
  XDG_STATE_HOME: join(dataRoot, "host"),
  LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_PACK_PATH: packDir,
  JARVISOS_AGY_BIN: "/nonexistent/agy",
  JARVISOS_CODEX_BIN: "/nonexistent/codex",
  JARVISOS_AGENT_RELAY_BIN: "/nonexistent/agent-relay",
};
delete env.GH_TOKEN;
delete env.GITHUB_TOKEN;
delete env.JARVISOS_GITHUB_READ_TOKEN;

// Initialize isolated database with default workspace from pack
const init = spawnSync(
  python,
  ["-c", "from app.core.bootstrap import initialize_storage\ninitialize_storage(seed_default=True)"],
  { cwd: backend, env, encoding: "utf8" }
);
if (init.status !== 0) throw new Error(`isolated database initialization failed: ${init.stderr}`);

const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], {
  cwd: backend,
  env,
  stdio: ["ignore", log, log],
});

const summary = {
  spec: "311",
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
      sha256: createHash("sha256").update(await readFile(file)).digest("hex"),
    });
  };

  const paper = join(dataRoot, "paper.md");
  await writeFile(paper, "# Rate study\nLine 2: k_rate is 0.0012 1/s at 300 K\nLine 3: end of data\n", "utf8");

  // 1. Open root from a clean profile
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  const picker = page.getByLabel("Project", { exact: true });
  if (await picker.count()) {
    await picker.selectOption({ label: "Walk 311 Workspace" });
  }
  await shot("01-root");
  summary.steps.push({ step: "open_root_via_visible_controls", status: "pass" });

  // 2. Open Memory -> Literature
  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Memory", exact: true }).click();
  await page.getByRole("navigation", { name: "Project knowledge destinations" }).getByRole("link", { name: "Literature", exact: true }).click();
  await page.locator(".literature-library").waitFor();
  await shot("02-literature-page");
  summary.steps.push({ step: "open_literature_page", status: "pass" });

  // 3. Register text source with file
  await page.getByRole("button", { name: "+ Register source" }).click();
  await page.getByLabel("Source title").fill("Rate study");
  await page.getByLabel("Source file (optional)").setInputFiles(paper);
  await shot("03-register-source");
  await page.getByRole("button", { name: "Save source", exact: true }).click();

  const source = page.locator("details[data-source-id]").first();
  await page.locator('details[data-source-id][data-backing-availability="available"]').waitFor();
  await shot("04-source-registered");
  summary.steps.push({ step: "register_text_source_with_backing_file", status: "pass" });

  // 4. Record located datum with quantity name
  await source.getByText("Add a finding", { exact: true }).click();
  const form = source.locator(".literature-add-finding form");
  await form.getByLabel("Finding type").selectOption("datum");
  await form.getByLabel("Quantity name (optional)").fill("k_rate");
  await form.getByLabel("Reported value").fill("k_rate is 0.0012 1/s");
  await form.getByLabel("Unit (optional)").fill("1/s");
  await form.getByLabel("Number (optional)").fill("0.0012");
  await form.getByLabel("Lines from (optional)").fill("2");
  await form.getByLabel("Lines to (optional)").fill("2");
  await shot("05-add-datum-finding");
  await form.getByRole("button", { name: "Save raw finding" }).click();
  await source.getByRole("status").filter({ hasText: "Saved as raw" }).waitFor();

  // Wait for verdict to show usable ("Checked against the source")
  await source.locator("em", { hasText: "Checked against the source" }).waitFor();
  const entry = source.locator("details.literature-entry").first();
  await entry.locator("summary").first().click();
  await shot("06-datum-checked-usable");
  summary.steps.push({ step: "record_located_datum_with_quantity", status: "pass" });

  // 5. Accept as fact
  const acceptBtn = entry.getByRole("button", { name: "Accept as fact" });
  if (await acceptBtn.isDisabled()) {
    throw new Error("Accept as fact button is unexpectedly disabled for usable datum");
  }
  await acceptBtn.click();
  await entry.locator("summary").first().filter({ hasText: "datum · accepted" }).waitFor();
  await shot("07-accepted-as-fact");
  summary.steps.push({ step: "accept_datum_as_fact", status: "pass" });

  // 6. Use as parameter
  const paramInput = entry.getByLabel("Parameter name");
  await paramInput.waitFor();
  await paramInput.fill("k_param");
  await shot("08-use-as-parameter-form");
  await entry.getByRole("button", { name: "Use as parameter" }).click();

  await entry.getByRole("status").filter({ hasText: "Parameter k_param created as draft." }).waitFor();
  await entry.locator("li", { hasText: "parameter: k_param" }).waitFor();
  await shot("09-parameter-created-under-used-by");
  summary.steps.push({ step: "use_datum_as_parameter_and_see_under_used_by", status: "pass" });

  // 7. Open Development -> Prontezza
  await page.getByRole("link", { name: "Development", exact: true }).click();
  await page.getByRole("link", { name: "Prontezza", exact: true }).click();
  await page.locator(".readiness-board").waitFor();
  await shot("10-prontezza-board");

  // Verify Dati and Ingressi legati are green
  const factsBtn = page.locator('button.readiness-board__cell-button[data-status="green"]', { hasText: "Dati" });
  const boundBtn = page.locator('button.readiness-board__cell-button[data-status="green"]', { hasText: "Ingressi legati" });
  await factsBtn.waitFor();
  await boundBtn.waitFor();
  await shot("11-dati-and-ingressi-legati-green");
  summary.steps.push({ step: "prontezza_dati_and_ingressi_legati_green", status: "pass" });

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
    await rm(packDir, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
}
