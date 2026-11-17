#!/usr/bin/env node
// Spec 299: real-Chromium walk of the "Banco di prova" section on Runs. Starts at `/` in a clean profile, opens
// Runs through the visible navigation, opens a run, compares two and reads the check table. The bench runs are
// fixture result.json folders under a temporary data root; nothing is started from the screen.
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/bench_runs_walk.mjs <evidence-dir>
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/bench_runs_walk.mjs <evidence-dir>  (needs built frontend, Playwright)");
  process.exit(0);
}
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8299);
const base = `http://127.0.0.1:${port}`;
if (!process.argv[2]) throw new Error("usage: bench_runs_walk.mjs <evidence-dir>");
const evidence = resolve(process.argv[2]);
const assert = (ok, message) => { if (!ok) throw new Error(message); };
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-299-data-"));
const controlDir = join(dataRoot, "control");
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });

const env = {
  ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: join(controlDir, "continuity"), JARVIS_RELAY_REPO: join(dataRoot, "relay-repo"),
  XDG_STATE_HOME: join(dataRoot, "host"), LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_AGY_BIN: "/nonexistent/agy", JARVISOS_CODEX_BIN: "/nonexistent/codex",
  JARVISOS_AGENT_RELAY_BIN: "/nonexistent/agent-relay",
};
delete env.GH_TOKEN; delete env.GITHUB_TOKEN; delete env.JARVISOS_GITHUB_READ_TOKEN;

await new Promise((ok, fail) => {
  const probe = createServer().once("error", () => fail(new Error(`port ${port} is in use`)))
    .once("listening", () => probe.close(ok)).listen(port, "127.0.0.1");
});
const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();

// One workspace and four fixture runs, one per verdict, in the workspace bench root of the temporary data root.
const seed = `
import json
from pathlib import Path
from app.core.database import initialize_database
from app.modules.engineering.operator_service import workspace_dir
from app.modules.workspaces.service import create_workspace, default_workspace_payload
initialize_database()
ws = create_workspace(default_workspace_payload())
bench = workspace_dir(ws.id).parent / "bench"
def check(cid, status, actual=1.5, limit=3.0, unit="mm", family="geometry"):
    return {"id": cid, "family": family, "status": status, "actual": actual, "limit": limit, "unit": unit,
            "hint": "controllo di prova", "source": "evaluator"}
def out(name, value, unit):
    return {"name": name, "value": {"value": value, "unit": unit}}
runs = {
  "run_solid_ok": ("bluecad.solid", "succeeded", "usable", "2026-11-17T10:00:00Z", None,
                   [check("volume_positive", "pass")], [out("volume", 12.0, "mm3")]),
  "run_solid_warn": ("bluecad.solid", "succeeded", "usable_with_warnings", "2026-11-17T11:00:00Z", None,
                     [check("volume_positive", "pass"), check("wall_thickness", "warn", 0.9, 1.0)], [out("volume", 15.0, "mm3")]),
  "run_mesh_unchecked": ("bluecad.mesh", "succeeded", "unchecked", "2026-11-17T12:00:00Z", None,
                         [check("aspect_ratio", "not_evaluated", None, None, None, "mesh")], []),
  "run_fem_failed": ("bluecad.fem", "failed", "not_usable", "2026-11-17T13:00:00Z",
                     {"code": "SOLVER_DIVERGED", "message": "il solutore non converge"},
                     [check("equilibrium", "fail", 0.2, 0.01, "1", "equilibrium")], []),
}
for run_id, (ev, status, verdict, done, failure, checks, outputs) in runs.items():
    folder = bench / run_id
    folder.mkdir(parents=True)
    (folder / "result.json").write_text(json.dumps({"evaluator_id": ev, "status": status, "verdict": verdict,
        "completed_at": done, "failure": failure, "checks": checks, "outputs": outputs}))
`;
const init = spawnSync(python, ["-c", seed], { cwd: backend, env, encoding: "utf8" });
assert(init.status === 0, `isolated database initialization failed: ${init.stderr}`);
const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)],
  { cwd: backend, env, stdio: ["ignore", log, log] });

const summary = { spec: "299", head, browser: null, started_at: new Date().toISOString(),
  console_errors: [], page_errors: [], screenshots: [], steps: [] };
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
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: "light" });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);
  page.on("console", (m) => { if (m.type() === "error") summary.console_errors.push(m.text()); });
  page.on("pageerror", (e) => summary.page_errors.push(String(e)));
  const shot = async (name) => {
    const file = join(evidence, `${String(summary.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
    await page.screenshot({ path: file, fullPage: true });
    summary.screenshots.push({ file: file.slice(evidence.length + 1), sha256: createHash("sha256").update(await readFile(file)).digest("hex") });
  };

  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  const tools = page.getByRole("navigation", { name: "Design tools" });
  if (!await tools.getByRole("link", { name: "Runs", exact: true }).isVisible()) await tools.getByText("More Design tools").click();
  await tools.getByRole("link", { name: "Runs", exact: true }).click();
  await page.getByRole("heading", { name: "Runs", level: 1 }).waitFor();
  const section = page.getByRole("region", { name: "Banco di prova" });
  await section.waitFor();
  summary.steps.push({ step: "open_runs_from_root", status: "pass" });

  // List: four runs, newest first, four distinct verdict labels.
  const rows = section.locator("li.bench-row");
  await rows.nth(3).waitFor();
  assert(await rows.count() === 4, "four fixture runs listed");
  const order = await rows.locator(".bench-row__open strong").allInnerTexts();
  assert(order.join("|") === "Struttura statica (FEM)|Mesh BlueCAD|Solido BlueCAD|Solido BlueCAD", `newest first, got ${order}`);
  const listText = await section.innerText();
  for (const label of ["Utilizzabile con avvertenze", "Non verificato", "Non utilizzabile"]) assert(listText.includes(label), `list shows ${label}`);
  assert(/Utilizzabile(?! con)/.test(listText), "list shows Utilizzabile");
  assert(!listText.includes("/tmp") && !listText.includes(dataRoot), "no filesystem path on screen");
  await shot("bench-list");
  summary.steps.push({ step: "list_with_four_verdicts_newest_first", status: "pass" });

  // Detail of the failed run: failure and check table.
  await rows.nth(0).locator(".bench-row__open").click();
  const detail = section.getByRole("region", { name: "Dettaglio risultato" });
  await detail.getByText("il solutore non converge").waitFor();
  await detail.getByRole("columnheader", { name: "Controllo" }).waitFor();
  await detail.getByRole("cell", { name: "equilibrium", exact: true }).waitFor();
  await shot("run-detail-failed");
  summary.steps.push({ step: "open_failed_run_and_see_check_table", status: "pass" });

  // The not_evaluated check reads as such.
  await rows.nth(1).locator(".bench-row__open").click();
  await detail.getByRole("cell", { name: "Non valutato" }).waitFor();
  summary.steps.push({ step: "not_evaluated_is_shown", status: "pass" });

  // Compare the two solid runs.
  await rows.nth(2).getByRole("checkbox").check();
  await rows.nth(3).getByRole("checkbox").check();
  await section.getByRole("button", { name: "Confronta i due selezionati" }).click();
  const compare = section.getByRole("region", { name: "Confronto risultati" });
  await compare.getByRole("cell", { name: "volume", exact: true }).waitFor();
  assert((await compare.innerText()).includes("3 mm3"), "volume difference shown with its unit");
  await compare.getByRole("cell", { name: "wall_thickness", exact: true }).waitFor();
  await shot("compare");
  summary.steps.push({ step: "compare_two_runs", status: "pass" });

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
