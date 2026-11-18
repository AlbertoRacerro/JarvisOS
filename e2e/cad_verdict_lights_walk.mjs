#!/usr/bin/env node
// Spec 301: real-Chromium walk of the shared verdict lights on CAD evidence.
// Starts at `/` in a clean profile, navigates to the CAD screen using visible controls,
// opens the candidate's "Evidence references", and asserts the verdict lights,
// Italian labels, and checks table for mesh (unchecked) and FEM (not_usable).
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/cad_verdict_lights_walk.mjs <evidence-dir>
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/cad_verdict_lights_walk.mjs <evidence-dir>  (needs built frontend, Playwright)");
  process.exit(0);
}
let pwModule = process.env.PLAYWRIGHT_MODULE ?? "playwright";
let chromium;
try {
  const pw = await import(pwModule);
  chromium = pw.chromium ?? pw.default?.chromium;
} catch {
  const fallback = "/home/thera/jarvis-control/work/tools/pw/node_modules/playwright/index.js";
  const pw = await import(fallback);
  chromium = pw.chromium ?? pw.default?.chromium;
}
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = resolve(root, process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python"));
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8301);
const base = `http://127.0.0.1:${port}`;
if (!process.argv[2]) throw new Error("usage: cad_verdict_lights_walk.mjs <evidence-dir>");
const evidence = resolve(process.argv[2]);
const assert = (ok, message) => { if (!ok) throw new Error(message); };
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-301-data-"));
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

// Seed database with one workspace, one candidate with one attempt, and evidence records:
// 1. solid validation evidence -> usable
// 2. mesh quality evidence with missing quality check -> unchecked
// 3. FEM static evidence with unbalanced reaction (failed required check) -> not_usable
const seed = `
import json
from pathlib import Path
from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.modules.workspaces.service import list_workspaces

initialize_storage()
ws = list_workspaces()[0]
wid = ws.id
NOW = "2026-11-18T02:00:00+00:00"

with open_sqlite_connection() as conn:
    conn.execute(
        """
        INSERT INTO bluecad_candidates (
            id, workspace_id, brief_text, brief_digest, status, parked_reason,
            spec_artifact_id, glb_artifact_id, report_artifact_id,
            promoted_decision_id, origin, parent_candidate_id, loop_config_json,
            created_at, updated_at, notes
        ) VALUES ('cand-1', ?, 'Spec 301 CAD Verdict Lights Candidate', ?, 'valid', NULL, NULL, NULL, NULL,
                  NULL, 'ai', NULL, '{}', ?, ?, NULL)
        """,
        (wid, 'sha256:' + 'a' * 64, NOW, NOW),
    )

    art_dir = build_paths().artifacts_dir / wid / 'bluecad'
    def seed_art(aid, payload):
        p = art_dir / aid / f'{aid}.json'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload), encoding='utf-8')
        conn.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, filename, stored_path, artifact_type, mime_type,
                sha256, source_ref, status, created_at
            ) VALUES (?, ?, ?, ?, 'bluecad_report', 'application/json', ?, NULL, 'registered', ?)
            """,
            (aid, wid, f'{aid}.json', str(p), 'a' * 64, NOW),
        )
        return p

    # 1. Solid validation: manifest + report + attempt + evidence -> usable
    manifest = {
        'assembly': {
            'total_volume_mm3': 1000.0,
            'bbox_mm': {'min': [0.0, 0.0, 0.0], 'max': [10.0, 10.0, 10.0]},
            'kernel_checks': {'part1': {'brep_valid': True, 'manifold': True}},
        },
        'parts': {},
    }
    mpath = seed_art('art-manifest-1', manifest)
    (mpath.parent / 'model.step').write_text('ISO-10303-21;', encoding='utf-8')

    report = {
        'report_version': 'bluecad_validation_report_v0_1',
        'spec_id': 'sha256:testspec',
        'verdict': 'pass',
        'checks': [
            {'id': 'T1_BREP_VALID', 'status': 'pass'},
            {'id': 'T1_WATERTIGHT', 'status': 'pass'},
            {'id': 'T1_VOLUME_DECL', 'status': 'pass', 'detail': {'actual': 1000.0, 'rel_error': 0.0002, 'rel_tol': 0.001}},
        ],
        'errors': [],
    }
    seed_art('art-report-1', report)

    conn.execute(
        """
        INSERT INTO bluecad_attempts (
            id, candidate_id, attempt_no, route_class, proposal_outcome,
            build_outcome, validation_verdict, spec_artifact_id,
            report_artifact_id, manifest_artifact_id, started_at, finished_at,
            error_detail_json
        ) VALUES ('att-1', 'cand-1', 1, 'auto', 'ok', 'pass', 'pass', NULL, 'art-report-1', 'art-manifest-1', ?, ?, NULL)
        """,
        (NOW, NOW),
    )

    conn.execute(
        """
        INSERT INTO evidence_records (
            id, workspace_id, kind, verdict, metrics_json, source_run_id,
            candidate_id, attempt_id, report_artifact_id, created_at
        ) VALUES ('ev-solid', ?, 'validation_v0', 'pass', '{}', NULL, 'cand-1', 'att-1', 'art-report-1', ?)
        """,
        (wid, NOW),
    )

    # 2. Mesh quality missing quality check -> unchecked
    mesh_dir = art_dir / 'mesh_out'
    mesh_dir.mkdir(parents=True, exist_ok=True)
    log_path = mesh_dir / 'gmsh_no_qual.log'
    log_path.write_text('Info: Gmsh 4.15.2 exited successfully with 0 errors\\n', encoding='utf-8')
    mesh_payload = {
        'schema_version': 'bluecad_mesh_result_v0_1',
        'verdict': 'pass',
        'errors': [],
        'attempts': [
            {
                'attempt_no': 1,
                'target_size': 25.0,
                'gmsh_returncode': 0,
                'counts': {'elements_total': 50, 'nodes_total': 20, 'volume_element_types': {'C3D4': 50}},
                'warnings': [],
                'errors': [],
                'artifacts': {'gmsh_log': {'path': str(log_path), 'sha256': 'y' * 64, 'bytes': 50}},
            }
        ],
        'artifacts': {'gmsh_log': {'path': str(log_path), 'sha256': 'y' * 64, 'bytes': 50}},
    }
    seed_art('art-mesh', mesh_payload)
    conn.execute(
        """
        INSERT INTO simulation_runs (
            id, workspace_id, model_version_id, run_label, status,
            input_payload, parameter_payload, output_payload,
            started_at, completed_at, created_at, notes
        ) VALUES ('sim-mesh', ?, NULL, 'sim-mesh', 'succeeded', '{}', ?, NULL, ?, ?, ?, 'notes')
        """,
        (wid, json.dumps({'analysis_id': 'mesh-spec-1'}), NOW, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO evidence_records (
            id, workspace_id, kind, verdict, metrics_json, source_run_id,
            candidate_id, attempt_id, report_artifact_id, created_at
        ) VALUES ('ev-mesh', ?, 'mesh_quality_v0', 'pass', '{}', 'sim-mesh', 'cand-1', 'att-1', 'art-mesh', ?)
        """,
        (wid, NOW),
    )

    # 3. FEM static with unbalanced reaction -> not_usable
    fem_spec = {
        'analysis_id': 'fem-spec',
        'analysis_type': 'static',
        'bcs': [{'port_label': 'fixed_port', 'kind': 'fixed'}],
        'loads': [{'port_label': 'load_port', 'type': 'force_total', 'force': [100.0, 0.0, 0.0]}],
    }
    conn.execute(
        """
        INSERT INTO simulation_runs (
            id, workspace_id, model_version_id, run_label, status,
            input_payload, parameter_payload, output_payload,
            started_at, completed_at, created_at, notes
        ) VALUES ('sim-fem', ?, NULL, 'sim-fem', 'succeeded', '{}', ?, NULL, ?, ?, ?, 'notes')
        """,
        (wid, json.dumps(fem_spec), NOW, NOW, NOW),
    )
    fem_unbalanced = {
        'schema_version': 'bluecad_result_summary_v0_1',
        'verdict': 'pass',
        'errors': [],
        'solver': {'tool_id': 'calculix', 'version': '2.22', 'returncode': 0},
        'analysis_type': 'static',
        'max_displacement': {'node_id': 10, 'value': 0.045},
        'max_von_mises': {'element_id': 1, 'node_id': 10, 'value': 120.5},
        'reactions': [{'node_id': 1, 'force': [0.0, 0.0, 0.0]}],
        'artifacts': {},
    }
    seed_art('art-fem', {'result_summary': fem_unbalanced, 'report': None})
    conn.execute(
        """
        INSERT INTO evidence_records (
            id, workspace_id, kind, verdict, metrics_json, source_run_id,
            candidate_id, attempt_id, report_artifact_id, created_at
        ) VALUES ('ev-fem', ?, 'fem_static_v0', 'pass', '{}', 'sim-fem', 'cand-1', 'att-1', 'art-fem', ?)
        """,
        (wid, NOW),
    )

    conn.commit()
`;

const init = spawnSync(python, ["-c", seed], { cwd: backend, env, encoding: "utf8" });
assert(init.status === 0, `isolated database initialization failed: ${init.stderr}`);
const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)],
  { cwd: backend, env, stdio: ["ignore", log, log] });

const summary = { spec: "301", head, browser: null, started_at: new Date().toISOString(),
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

  // Step 1: Start at / in a clean profile and reach the CAD screen using visible controls only.
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  const tools = page.getByRole("navigation", { name: "Design tools" });
  if (!await tools.getByRole("link", { name: "BlueCad workbench", exact: true }).isVisible()) {
    await tools.getByText("More Design tools").click();
  }
  await tools.getByRole("link", { name: "BlueCad workbench", exact: true }).click();
  await page.getByRole("heading", { name: "Model workbench" }).waitFor();
  await shot("cad-screen-reached");
  summary.steps.push({ step: "reach_cad_screen_from_root", status: "pass" });

  // Step 2: Open "Evidence references" in the Analysis dock using visible controls.
  const evidenceBtn = page.getByRole("button", { name: "Evidence references" });
  await evidenceBtn.waitFor();
  await evidenceBtn.click();

  const dock = page.locator("#shell-analysis-dock");
  await dock.waitFor();
  const evidenceHeading = dock.getByRole("heading", { name: "Evidence references" });
  await evidenceHeading.waitFor();
  await shot("evidence-references-dock");
  summary.steps.push({ step: "open_evidence_references", status: "pass" });

  // Step 3: Assert Italian labels and verdict status lights.
  // - "non verificato" for unchecked mesh quality
  // - "non utilizzabile" for failed FEM
  // - "utilizzabile" for solid validation
  const dockText = await dock.innerText();
  assert(dockText.includes("non verificato"), "dock shows 'non verificato' for mesh");
  assert(dockText.includes("non utilizzabile"), "dock shows 'non utilizzabile' for FEM");
  assert(await dock.locator('.bench-verdict[data-verdict="usable"]').first().innerText() === "utilizzabile", "dock shows 'utilizzabile' for solid");

  const uncheckedVerdict = dock.locator('.bench-verdict[data-verdict="unchecked"]').first();
  await uncheckedVerdict.waitFor();
  assert(await uncheckedVerdict.locator(".bench-light").isVisible(), "light visible for unchecked verdict");

  const notUsableVerdict = dock.locator('.bench-verdict[data-verdict="not_usable"]').first();
  await notUsableVerdict.waitFor();
  assert(await notUsableVerdict.locator(".bench-light").isVisible(), "light visible for not_usable verdict");

  summary.steps.push({ step: "assert_verdict_lights_and_italian_labels", status: "pass" });

  // Step 4: Open "Checks" details element on the failed FEM evidence and assert failed required check.
  const femRow = dock.locator("li").filter({ hasText: "fem_static_v0" }).first();
  await femRow.waitFor();
  const checksDisclosure = femRow.locator("details summary").filter({ hasText: "Checks" });
  await checksDisclosure.waitFor();
  await checksDisclosure.click();

  const checksTable = femRow.locator("table.bluecad-table");
  await checksTable.waitFor();

  // Assert failed required check FORCE_EQUILIBRIUM in checks table
  const failedRow = checksTable.locator('tr[data-status="fail"]');
  await failedRow.waitFor();
  const failedRowText = await failedRow.innerText();
  assert(failedRowText.includes("FORCE_EQUILIBRIUM"), "failed row has check name FORCE_EQUILIBRIUM");
  assert(failedRowText.includes("conservation"), "failed row has family conservation");
  assert(failedRowText.includes("fail"), "failed row has status fail");
  assert(failedRowText.includes("yes"), "failed row is marked as required (yes)");
  await shot("checks-table-failed-required");
  summary.steps.push({ step: "assert_failed_required_check_table", status: "pass" });

  // Step 5: Assert no filesystem / absolute paths leak on the page.
  const fullPageText = await page.innerText("body");
  assert(!fullPageText.includes("/tmp") && !fullPageText.includes("/home") && !fullPageText.includes(dataRoot),
    "no filesystem paths leak on page");
  summary.steps.push({ step: "assert_no_paths_leak", status: "pass" });

  // Step 6: Console and page errors verification
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
  await writeFile(join(evidence, "result.json"), `${JSON.stringify(summary, null, 2)}\n`);
  process.exitCode = summary.passed ? 0 : 1;
}
console.log(JSON.stringify({ head, browser: summary.browser, passed: summary.passed, steps: summary.steps }));
