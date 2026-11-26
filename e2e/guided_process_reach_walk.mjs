#!/usr/bin/env node
// Spec 319: the guided path reaches the Process editor (GUIDED-PROCESS-REACH-1)
// Walks from `/`, opens Design, creates a question, seeds a requirement and a source for the
// question's workspace through the backend modules, then checks that the "Prossimo passo" card
// opens the Process editor, and that a process draft with one unit turns "Modello" done.

import { createHash } from "node:crypto";
import { openSync } from "node:fs";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/guided_process_reach_walk.mjs <evidence-dir> [<data-root-copy>]");
  process.exit(0);
}

if (!process.argv[2]) {
  throw new Error("usage: node e2e/guided_process_reach_walk.mjs <evidence-dir> [<data-root-copy>]");
}

const pw = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const chromium = pw.chromium ?? pw.default?.chromium;

const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8319);
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
// Spec 377: a source data root inside or containing the private vault is refused before it is read.
if (process.argv[3] && spawnSync("python3", [resolve(root, "scripts/vault_guard.py"), "check", resolve(process.argv[3])], { stdio: "inherit" }).status !== 0) {
  throw new Error("source data root refused by the vault guard");
}
const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-319-data-"));
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
  spec: "319",
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

  const seed = (code, workspaceId) => {
    const result = spawnSync(python, ["-c", code, workspaceId], { cwd: backend, env, encoding: "utf8" });
    if (result.status !== 0) throw new Error(`seed failed: ${result.stderr}`);
  };

  // 1. Open root, then Design through the primary navigation
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  await shot("root");
  await page
    .getByRole("navigation", { name: "Primary navigation" })
    .getByRole("link", { name: "Design", exact: true })
    .click();
  await page.locator(".guided-flow-bar").waitFor();
  summary.steps.push({ step: "open_design_screen", status: "pass" });

  // 2. Create a question with the visible form; read its workspace from the app's own request
  await page.locator(".guided-flow-bar__new-btn").click();
  await page.locator('.guided-flow-bar__create-form input[type="text"]').first().fill("Quale portata della pompa?");
  const created = page.waitForResponse(
    (r) => r.request().method() === "POST" && /\/engineering\/guided-questions$/.test(r.url())
  );
  await page.getByRole("button", { name: "Crea domanda" }).click();
  const workspaceId = (await (await created).json()).workspace_id;
  if (!workspaceId) throw new Error("question has no workspace_id");
  await page.locator(".guided-flow-bar__question-card", { hasText: "Quale portata della pompa?" }).waitFor();
  await shot("question-created");
  summary.steps.push({ step: "create_question", status: "pass" });

  // 3. Seed the frame and sources steps (test setup, not part of the reachability claim)
  seed(
    [
      "import sys",
      "from uuid import uuid4",
      "from app.core.database import open_sqlite_connection",
      "from app.modules.events.service import utc_now",
      "ws, now = sys.argv[1], utc_now()",
      "with open_sqlite_connection() as c:",
      "    c.execute(\"INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) VALUES (?, ?, 'Portata minima 1 L/min', 'active', ?, ?)\", (str(uuid4()), ws, now, now))",
      "    c.execute(\"INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, created_at, updated_at) VALUES (?, ?, 'Manuale', 'book', 'accepted', ?, ?)\", (str(uuid4()), ws, now, now))",
      "    c.commit()",
    ].join("\n"),
    workspaceId
  );
  await page.reload({ waitUntil: "networkidle" });
  await page.locator(".guided-flow-bar__question-card", { hasText: "Quale portata della pompa?" }).click();
  const modelNode = page.locator('button.guided-flow-bar__step-node[data-step="model"]');
  await modelNode.waitFor();
  if ((await modelNode.getAttribute("data-state")) !== "not_started") {
    throw new Error(`Modello should be not_started, got ${await modelNode.getAttribute("data-state")}`);
  }
  await page.locator(".guided-flow-bar__next-title", { hasText: "Apri il diagramma di processo" }).waitFor();
  await shot("next-card-process");
  summary.steps.push({ step: "next_card_names_process_editor", status: "pass" });

  // 4. The card's visible button opens the Process editor
  await page.locator(".guided-flow-bar__next-btn").click();
  await page.waitForURL("**/design/process");
  await shot("process-editor");
  summary.steps.push({ step: "next_card_opens_process_editor", status: "pass" });

  // 5. A process draft with one unit turns Modello done
  seed(
    [
      "import sys",
      "from app.modules.process_stack import draft",
      "from app.modules.process_stack.draft_models import AddUnit",
      "ws = sys.argv[1]",
      "state = draft.create_draft(ws, 'Linea 1')",
      "draft.patch(ws, state['draft_id'], state['revision'], [AddUnit(op='add_unit', id='pump', type='Pump', tag='Pompa', x=0, y=0)])",
    ].join("\n"),
    workspaceId
  );
  await page
    .getByRole("navigation", { name: "Primary navigation" })
    .getByRole("link", { name: "Design", exact: true })
    .click();
  await page.locator(".guided-flow-bar__question-card", { hasText: "Quale portata della pompa?" }).click();
  await page.locator('button.guided-flow-bar__step-node[data-step="model"][data-state="done"]').waitFor();
  await shot("model-done");
  summary.steps.push({ step: "process_draft_makes_model_done", status: "pass" });

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
