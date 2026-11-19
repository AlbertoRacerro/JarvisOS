#!/usr/bin/env node
// Spec 328: one browser walk takes a readiness case from seven red cells to seven green through visible controls.
// After the isolated backend starts, this script only READS the board over HTTP; every record is made in the UI.
// A step that cannot be done with visible controls fails the walk at that step (summary.json names the control).

import { createHash } from "node:crypto";
import { openSync } from "node:fs";
import { appendFile, cp, mkdir, mkdtemp, readFile, rm, writeFile, stat } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

const USAGE = "node e2e/case_walk.mjs <evidence-dir> [--pack <pack-dir>] [--case <case-id>] [--data-root-copy <dir>]";
if (process.argv.includes("--help")) {
  console.log(USAGE);
  process.exit(0);
}
const args = process.argv.slice(2);
const flag = (name) => {
  const i = args.indexOf(name);
  if (i < 0) return null;
  const value = args[i + 1];
  if (!value) throw new Error(`${name} needs a value; usage: ${USAGE}`);
  args.splice(i, 2);
  return value;
};
const packArg = flag("--pack");
const caseArg = flag("--case");
const copyArg = flag("--data-root-copy");
if (!args[0]) throw new Error(`usage: ${USAGE}`);

const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8328);
const base = `http://127.0.0.1:${port}`;
if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) throw new Error("Invalid proof port");
const evidence = resolve(args[0]);
const packDir = resolve(packArg ?? join(backend, "tests/fixtures/packs/zephyr"));
const caseId = caseArg ?? "generic-literature-1";

const tomlToJson = (file) => {
  const r = spawnSync(
    python,
    ["-I", "-c", "import json,sys,tomllib\nprint(json.dumps(tomllib.load(open(sys.argv[1],'rb'))))", file],
    { encoding: "utf8", env: { ...process.env, LANG: "C.UTF-8", LC_ALL: "C.UTF-8" } }
  );
  if (r.status !== 0) throw new Error(`cannot read ${file}: ${r.stderr.trim()}`);
  return JSON.parse(r.stdout);
};

const casesFile = join(packDir, "readiness_cases.toml");
const caseDef = (tomlToJson(casesFile).cases ?? []).find((c) => c.id === caseId);
if (!caseDef) throw new Error(`case '${caseId}' not found in ${casesFile}`);
const walkFile = join(packDir, "case_walks", `${caseId}.toml`);
const walkInput = await stat(walkFile).then(() => tomlToJson(walkFile), () => {
  throw new Error(`walk input file missing: ${walkFile}`);
});

let pw;
try {
  pw = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
} catch (err) {
  if (!process.env.PLAYWRIGHT_MODULE) throw err;
  pw = await import(`${process.env.PLAYWRIGHT_MODULE}/index.js`);
}
const chromium = pw.chromium ?? pw.default?.chromium;

await new Promise((ok, fail) => {
  const probe = createServer()
    .once("error", (error) => fail(new Error(`port ${port} is in use: ${error.message}`)))
    .once("listening", () => probe.close(ok))
    .listen(port, "127.0.0.1");
});
await mkdir(evidence, { recursive: true });
await rm(join(evidence, "board.jsonl"), { force: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-328-data-"));
if (copyArg) await cp(resolve(copyArg), dataRoot, { recursive: true });
const docsDir = await mkdtemp(join(tmpdir(), "jarvisos-328-docs-"));
const controlDir = join(dataRoot, "control");
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });

const env = {
  ...process.env,
  LANG: "C.UTF-8",
  LC_ALL: "C.UTF-8",
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
  JARVISOS_AGENT_RELAY_BIN: "/nonexistent/agent-relay"
};
delete env.GH_TOKEN;
delete env.GITHUB_TOKEN;
delete env.JARVISOS_GITHUB_READ_TOKEN;

const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const init = spawnSync(
  python,
  ["-c", "from app.core.bootstrap import initialize_storage\ninitialize_storage(seed_default=False)"],
  { cwd: backend, env, encoding: "utf8" }
);
if (init.status !== 0) throw new Error(`isolated database initialization failed: ${init.stderr}`);

const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], {
  cwd: backend,
  env,
  stdio: ["ignore", log, log]
});

// No absolute data-root, pack or scratch path in the evidence.
const scrub = (text) =>
  String(text).split(dataRoot).join("<data-root>").split(docsDir).join("<docs>").split(packDir).join("<pack>").split(root).join("<repo>");

const STEPS = ["paper", "facts", "bound", "run", "compare", "decide", "answer"];
const summary = {
  spec: "328",
  pack_id: null,
  case_id: caseId,
  head,
  browser: null,
  started_at: new Date().toISOString(),
  passed: false,
  first_failing_step: null,
  failure: null,
  console_errors: [],
  page_errors: [],
  screenshots: [],
  steps: []
};
try {
  summary.pack_id = tomlToJson(join(packDir, "jarvis_pack.toml")).id ?? null;
} catch {
  /* pack manifest is optional for the walk */
}

class MissingControl extends Error {}

let browser;
let page;
let current = "start";
let last = null; // newest board read of this case

const readBoard = async () => {
  const res = await fetch(`${base}/readiness`);
  if (!res.ok) throw new Error(`board read failed: ${res.status}`);
  const board = await res.json();
  const item = board.cases.find((c) => c.id === caseId);
  if (!item) throw new Error(`case '${caseId}' is not on the board (${JSON.stringify(board.errors)})`);
  return item;
};
const states = (item) => Object.fromEntries(STEPS.map((s) => [s, item.steps[s].status]));
const checkpoint = async (step) => {
  const item = await readBoard();
  const cells = states(item);
  const missing = Object.fromEntries(STEPS.filter((s) => cells[s] === "red").map((s) => [s, item.steps[s].missing]));
  await appendFile(join(evidence, "board.jsonl"), `${scrub(JSON.stringify({ step, cells, missing }))}\n`);
  return { item, cells, missing };
};

const shot = async (name) => {
  const file = join(evidence, `${String(summary.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
  await page.screenshot({ path: file, fullPage: true });
  summary.screenshots.push({
    file: file.slice(evidence.length + 1),
    sha256: createHash("sha256").update(await readFile(file)).digest("hex")
  });
};

// A control that must be reachable by role, label or visible text; absence fails the step and names the control.
const need = async (description, locator, timeout = 15_000) => {
  try {
    await locator.waitFor({ timeout });
  } catch {
    throw new MissingControl(description);
  }
  return locator;
};

const cell = (stepId) => {
  const labels = { paper: "Articolo", facts: "Dati", bound: "Ingressi legati", run: "Calcolo", compare: "Confronto", decide: "Decisione", answer: "Risposta" };
  return page.locator(`.readiness-board__cell-button[aria-label^="${caseDef.title} - ${labels[stepId]}"]`).first();
};

const step = async (name, ownCell, fn) => {
  current = name;
  const before = last ? last.cells : null;
  try {
    await fn();
    const now = await checkpoint(name);
    last = now;
    await shot(name);
    if (ownCell && now.cells[ownCell] !== "green") {
      throw new Error(`board cell '${ownCell}' is not green after step '${name}': missing ${JSON.stringify(now.missing[ownCell])}`);
    }
    if (before) {
      const regressed = STEPS.filter((s) => before[s] === "green" && now.cells[s] !== "green");
      if (regressed.length) throw new Error(`cells turned red after step '${name}': ${regressed.join(", ")}`);
    }
    summary.steps.push({ step: name, status: "pass", cells: now.cells });
  } catch (error) {
    await shot(`${name}-failed`).catch(() => {});
    throw error;
  }
};

const openMemory = async (destination) => {
  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Memory", exact: true }).click();
  await need(`link '${destination}'`, page.getByRole("link", { name: destination, exact: true }).first());
  await page.getByRole("link", { name: destination, exact: true }).first().click();
};
const openBoard = async () => {
  await need("link 'Development'", page.getByRole("link", { name: "Development", exact: true }));
  await page.getByRole("link", { name: "Development", exact: true }).click();
  await need("link 'Prontezza'", page.getByRole("link", { name: "Prontezza", exact: true }));
  await page.getByRole("link", { name: "Prontezza", exact: true }).click();
  await need("Prontezza board", page.locator(".readiness-board"));
};

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
  page = await context.newPage();
  page.setDefaultTimeout(30_000);
  page.on("console", (m) => {
    if (m.type() === "error") summary.console_errors.push(scrub(m.text()));
  });
  page.on("pageerror", (e) => summary.page_errors.push(scrub(e)));

  // Checkpoint before the first step.
  last = await checkpoint("initial");

  // 0. From `/`: create the case workspace (or pick it when the copied data root already has it).
  await step("workspace", null, async () => {
    await page.goto(`${base}/`, { waitUntil: "networkidle" });
    const create = page.getByRole("button", { name: "Create workspace" });
    if (await create.count()) {
      await page.getByLabel("Workspace name").fill(caseDef.workspace);
      await page.getByText("Advanced workspace settings").click();
      await page.getByLabel("Workspace slug").fill(caseDef.workspace);
      await create.click();
      await need("workspace shell after 'Create workspace'", page.getByRole("navigation", { name: "Primary navigation" }));
    } else {
      const picker = page.getByLabel("Project", { exact: true });
      if (await picker.count()) await picker.selectOption({ label: caseDef.workspace }).catch(() => {});
    }
  });

  // 1. paper: a literature source titled like the case, with the paper file attached.
  await step("paper", "paper", async () => {
    const paperFile = join(packDir, "case_walks", String(walkInput.paper_file ?? ""));
    if (!walkInput.paper_file) throw new Error(`walk input has no paper_file (${walkFile})`);
    await stat(paperFile).catch(() => {
      throw new Error(`paper file missing: ${walkInput.paper_file}`);
    });
    await openMemory("Literature");
    await need("Literature page", page.locator(".literature-library"));
    await need("button '+ Register source'", page.getByRole("button", { name: "+ Register source" }));
    await page.getByRole("button", { name: "+ Register source" }).click();
    await need("field 'Source title'", page.getByLabel("Source title"));
    await page.getByLabel("Source title").fill(caseDef.source_title);
    await need("field 'Source file (optional)'", page.getByLabel("Source file (optional)"));
    await page.getByLabel("Source file (optional)").setInputFiles(paperFile);
    await page.getByRole("button", { name: "Save source", exact: true }).click();
    await need("registered source with available backing file", page.locator('details[data-source-id][data-backing-availability="available"]'));
  });

  const source = () => page.locator("details[data-source-id]", { hasText: caseDef.source_title }).first();

  const entryIds = {};

  // 2. facts: a located datum per case fact, checked against the paper and accepted.
  await step("facts", "facts", async () => {
    const entries = walkInput.facts ?? [];
    const absent = (caseDef.facts ?? []).filter((q) => !entries.some((f) => f.quantity === q));
    if (absent.length) throw new Error(`walk input has no located value for fact(s): ${absent.join(", ")}`);
    for (const fact of entries.filter((f) => caseDef.facts.includes(f.quantity))) {
      await need("'Add a finding'", source().getByText("Add a finding", { exact: true }));
      if (!(await source().locator(".literature-add-finding form").isVisible())) await source().getByText("Add a finding", { exact: true }).click();
      const form = source().locator(".literature-add-finding form");
      await need("finding form", form);
      const known = await source().locator("details.literature-entry").evaluateAll((els) => els.map((e) => e.getAttribute("data-entry-id")));
      await form.getByLabel("Finding type").selectOption("datum");
      await form.getByLabel("Quantity name (optional)").fill(fact.quantity);
      await form.getByLabel("Reported value").fill(fact.reported);
      await form.getByLabel("Unit (optional)").fill(fact.unit);
      await form.getByLabel("Number (optional)").fill(String(fact.value));
      await form.getByLabel("Lines from (optional)").fill(String(fact.line_from));
      await form.getByLabel("Lines to (optional)").fill(String(fact.line_to));
      await form.getByRole("button", { name: "Save raw finding" }).click();
      await need("'Saved as raw' status", source().getByRole("status").filter({ hasText: "Saved as raw" }).last());
      await need(`saved finding '${fact.quantity}'`, source().locator("details.literature-entry").nth(known.length));
      const ids = await source().locator("details.literature-entry").evaluateAll((els) => els.map((e) => e.getAttribute("data-entry-id")));
      const fresh = ids.filter((id) => !known.includes(id));
      if (fresh.length !== 1) throw new Error(`expected one new finding for '${fact.quantity}', found ${fresh.length}`);
      entryIds[fact.quantity] = fresh[0];
      const entry = source().locator(`details.literature-entry[data-entry-id="${fresh[0]}"]`);
      await entry.locator("summary").first().click();
      await need(`'Checked against the source' for '${fact.quantity}'`, entry.locator("em", { hasText: "Checked against the source" }));
      const accept = entry.getByRole("button", { name: "Accept as fact" });
      await need(`button 'Accept as fact' for '${fact.quantity}'`, accept);
      if (await accept.isDisabled()) throw new MissingControl(`enabled 'Accept as fact' for '${fact.quantity}'`);
      await accept.click();
      await need(`accepted state for '${fact.quantity}'`, entry.locator("summary").first().filter({ hasText: "datum · accepted" }));
    }
  });

  // 3. bound: each accepted datum becomes the case input it binds to.
  await step("bound", "bound", async () => {
    const entries = walkInput.facts ?? [];
    const absent = (caseDef.inputs ?? []).filter((n) => !entries.some((f) => f.input === n));
    if (absent.length) throw new Error(`walk input binds no fact to input(s): ${absent.join(", ")}`);
    for (const fact of entries.filter((f) => caseDef.inputs.includes(f.input))) {
      const entry = source().locator(`details.literature-entry[data-entry-id="${entryIds[fact.quantity]}"]`);
      await need(`finding '${fact.quantity}'`, entry);
      if (!(await entry.getByLabel("Parameter name").isVisible())) await entry.locator("summary").first().click();
      const name = entry.getByLabel("Parameter name");
      await need(`field 'Parameter name' for '${fact.quantity}'`, name);
      await name.fill(fact.input);
      await entry.getByRole("button", { name: "Use as parameter" }).click();
      await need(`parameter '${fact.input}' under 'Used by'`, entry.locator("li", { hasText: `parameter: ${fact.input}` }));
    }
  });

  // 4. run: a Runs card run with the walk input's field values.
  await step("run", "run", async () => {
    const run = walkInput.run;
    if (!run?.card_title) throw new Error(`walk input has no [run] card_title (${walkFile})`);
    const tools = page.getByRole("navigation", { name: "Design tools" });
    await need("navigation 'Design tools'", tools);
    if (!(await tools.getByRole("link", { name: "Runs", exact: true }).isVisible())) await tools.getByText("More Design tools").click();
    await need("link 'Runs'", tools.getByRole("link", { name: "Runs", exact: true }));
    await tools.getByRole("link", { name: "Runs", exact: true }).click();
    const section = page.getByRole("region", { name: "Banco di prova" });
    await need("region 'Banco di prova'", section);
    const form = section.getByRole("region", { name: "Nuova esecuzione" });
    await need("'Nuova esecuzione' card form", form);
    await need("card list", form.getByLabel("Correlazione"));
    await form.getByLabel("Correlazione").selectOption({ label: run.card_title }).catch(() => {
      throw new MissingControl(`card '${run.card_title}' in 'Correlazione'`);
    });
    for (const [label, value] of Object.entries(run.fields ?? {})) {
      const input = form.getByLabel(new RegExp(`^${label}\\b`));
      await need(`field '${label}'`, input);
      await input.fill(String(value));
    }
    for (const [label, value] of Object.entries(run.options ?? {})) {
      const select = form.getByLabel(new RegExp(`^${label}`));
      await need(`option '${label}'`, select);
      await select.selectOption(String(value));
    }
    await form.getByRole("button", { name: "Esegui" }).click();
    const detail = section.getByRole("region", { name: "Dettaglio risultato" });
    await need("'Dettaglio risultato'", detail, 60_000);
    // The board needs a usable run. A result still "Non verificato" has no visible control that records checks or a verdict.
    if ((await detail.innerText()).includes("Non verificato")) {
      throw new MissingControl(
        `a visible control that makes the '${run.card_title}' run usable: the result shows 'Non verificato' ('Nessun controllo registrato') and the Runs screen offers no way to record checks or a verdict on a run`
      );
    }
  });

  // 5. compare: the board's Confronto detail shows computed against published.
  await step("compare", "compare", async () => {
    await openBoard();
    const c = cell("compare");
    await need("cell 'Confronto'", c);
    await c.click();
    const detail = page.locator(".readiness-board__detail");
    await need("Confronto detail", detail);
    for (const text of ["Calcolato", "Pubblicato", "Scarto"]) await need(`'${text}' in Confronto detail`, detail.getByText(text).first());
  });

  // 6. verdict: record it and accept it (no board cell of its own; the Decisione step needs it).
  await step("verdict", null, async () => {
    const detail = page.locator(".readiness-board__detail");
    await need("button 'Registra verdetto'", detail.getByRole("button", { name: "Registra verdetto" }));
    await detail.getByRole("button", { name: "Registra verdetto" }).click();
    const rationale = detail.locator(".readiness-board__rationale-input");
    await need("rationale field", rationale);
    await rationale.fill(String(walkInput.verdict?.rationale ?? "Verified in the case walk."));
    await need("button 'Accetta verdetto'", detail.getByRole("button", { name: "Accetta verdetto" }));
    await detail.getByRole("button", { name: "Accetta verdetto" }).click();
    await need("accepted verdict", page.locator('.readiness-board__verdict-status[data-status="accepted"]'));
  });

  // 7. decide: record the decision on the accepted verdict.
  await step("decide", "decide", async () => {
    await cell("decide").click();
    const form = page.locator(".readiness-board__decision-form");
    await need("decision form", form);
    const decision = walkInput.decision ?? {};
    await form.getByLabel("Titolo").fill(String(decision.title ?? "Case walk decision"));
    await form.getByLabel("Decisione", { exact: true }).fill(String(decision.text ?? "The model reproduces the published value."));
    await form.getByLabel("Motivazione (opzionale)").fill(String(decision.rationale ?? "Accepted verdict within tolerance."));
    await form.getByRole("button", { name: "Registra decisione" }).click();
    await need("green 'Decisione' cell", page.locator('.readiness-board__cell-button[aria-label*="Decisione"][data-status="green"]').first());
  });

  // 8. answer: the Ricerca retrieval check over a folder holding the paper text.
  await step("answer", "answer", async () => {
    const answer = walkInput.answer;
    if (!answer?.question || !answer?.expected) throw new Error(`walk input has no [answer] question and expected (${walkFile})`);
    await writeFile(join(docsDir, "paper.md"), await readFile(join(packDir, "case_walks", walkInput.paper_file)));
    await openMemory("Ricerca");
    await need("field 'Percorso della cartella'", page.getByLabel("Percorso della cartella"));
    await page.getByLabel("Percorso della cartella").fill(docsDir);
    await page.getByRole("button", { name: "Registra cartella" }).click();
    await need("'Cartella registrata'", page.getByText("Cartella registrata:", { exact: false }));
    await page.getByRole("button", { name: "Aggiorna cartella" }).click();
    await need("'Inclusi: 1'", page.getByText("Inclusi: 1", { exact: false }));
    await page.getByLabel("Domanda").fill(answer.question);
    await page.getByLabel("Titolo o ID atteso").fill(answer.expected);
    await page.getByRole("button", { name: "Verifica recupero" }).click();
    await need("verdict 'Utilizzabile'", page.getByText("Utilizzabile", { exact: true }));
  });

  // Final: reload the board; all seven cells green on screen and in the board read.
  await step("final_reload", null, async () => {
    await openBoard();
    await page.reload({ waitUntil: "networkidle" });
    await need("board after reload", page.locator(".readiness-board"));
    for (const s of STEPS) await need(`green '${s}' cell after reload`, page.locator(`.readiness-board__cell-button[data-status="green"]`, { hasText: { paper: "Articolo", facts: "Dati", bound: "Ingressi legati", run: "Calcolo", compare: "Confronto", decide: "Decisione", answer: "Risposta" }[s] }).first());
    const now = await checkpoint("final_reload");
    const red = STEPS.filter((s) => now.cells[s] !== "green");
    if (red.length) throw new Error(`cells still red after reload: ${red.join(", ")}`);
  });

  if (summary.console_errors.length) throw new Error(`console errors: ${JSON.stringify(summary.console_errors)}`);
  if (summary.page_errors.length) throw new Error(`page errors: ${JSON.stringify(summary.page_errors)}`);
  summary.passed = true;
  await context.close();
} catch (error) {
  summary.passed = false;
  summary.first_failing_step = current;
  summary.failure = {
    step: current,
    message: scrub(error.message ?? error),
    missing_control: error instanceof MissingControl ? scrub(error.message) : null,
    board_missing: last?.missing ?? null
  };
  process.exitCode = 1;
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  for (let i = 0; i < 20 && server.exitCode === null; i++) await delay(250);
  if (server.exitCode === null) server.kill("SIGKILL");
  await rm(dataRoot, { recursive: true, force: true }).catch(() => {});
  await rm(docsDir, { recursive: true, force: true }).catch(() => {});
  summary.finished_at = new Date().toISOString();
  await writeFile(join(evidence, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  console.log(JSON.stringify({ pack_id: summary.pack_id, case_id: caseId, passed: summary.passed, first_failing_step: summary.first_failing_step, failure: summary.failure }));
}
