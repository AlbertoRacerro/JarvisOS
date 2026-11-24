#!/usr/bin/env node
// Spec 362: clean-profile browser walk proving visible plan and tool steps in the Jarvis sidecar.
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync, closeSync, existsSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

if (process.argv.includes('--help')) {
  console.log('node e2e/hermes_saved_plan_walk.mjs <evidence-dir>');
  process.exit(0);
}
if (!process.argv[2]) throw Error('evidence directory required');

const root = resolve(new URL('../', import.meta.url).pathname);
const backend = join(root, 'backend');
const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });

const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8362);
const base = `http://127.0.0.1:${port}`;
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, '.venv/bin/python');

const summary = {
  spec: 362,
  head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(),
  data_root: 'fresh temporary data root',
  checks: [],
  screenshots: [],
  requests: [],
  page_errors: [],
};

const assert = (condition, message) => {
  if (!condition) throw Error(message);
};

let data, server, browser, failure, log;

try {
  assert(Number.isInteger(port) && port > 0 && port <= 65535 && port !== 8000, 'invalid proof port');
  await new Promise((ok, fail) =>
    createServer()
      .once('error', error => fail(Error(`cannot bind proof port ${port}: ${error.message}`)))
      .once('listening', function () { this.close(ok); })
      .listen(port, '127.0.0.1')
  );

  data = await mkdtemp(join(tmpdir(), 'jarvisos-362-'));
  await mkdir(join(data, 'host'));

  const env = Object.fromEntries(
    Object.entries(process.env).filter(([key]) => !/^(JARVIS|OPENAI|ANTHROPIC|OLLAMA|SCW_|SCALEWAY)/i.test(key))
  );
  Object.assign(env, {
    PYTHONPATH: backend,
    JARVISOS_DATA_ROOT: data,
    XDG_STATE_HOME: join(data, 'host'),
    LOCALAPPDATA: join(data, 'host'),
  });

  // Seed database with workspace, thread, completed interaction with plan, and running interaction with live plan
  const seedScript = `
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.workspaces.service import create_workspace, default_workspace_payload
from app.modules.ai.settings import ensure_ai_settings
from app.modules.ai.token_flow_service import create_flow_in_transaction
from app.modules.events.service import log_event
import json

initialize_database()
ensure_ai_settings()
ws = create_workspace(default_workspace_payload())
ws_id = ws.id

with open_sqlite_connection() as conn:
    conn.execute(
        "INSERT INTO ai_threads (id, workspace_id, title, created_at, last_activity_at) VALUES ('thread-walk-362', ?, 'Walk Thread 362', '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:10+00:00')",
        (ws_id,),
    )
    flow_done_id = create_flow_in_transaction(conn, task_kind="agent", requested_route_class="hermes:agent", workspace_id=ws_id)
    conn.execute(
        "UPDATE ai_flows SET state = 'complete', updated_at = '2026-01-01T10:00:05+00:00', completed_at = '2026-01-01T10:00:05+00:00' WHERE id = ?",
        (flow_done_id,),
    )
    conn.execute(
        """
        INSERT INTO ai_thread_interactions (
            id, thread_id, request_id, request_digest, interaction_index, user_text,
            assistant_text, assistant_text_truncated, flow_id, persistence_state,
            created_at, updated_at
        ) VALUES (
            'inter-done-362', 'thread-walk-362', 'req-done', 'sha256:0', 1,
            'Size a packed column for CO2 absorption.',
            'Here is the completed column sizing based on the planned stages.', 0,
            ?, 'captured', '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:05+00:00'
        )
        """,
        (flow_done_id,),
    )
    plan_done = {
        "interaction_id": "inter-done-362",
        "steps": [
            {"id": "step-1", "title": "Check gas flow rate and equilibrium", "status": "done"},
            {"id": "step-2", "title": "Compute packing height and diameter", "status": "done", "note": "HETP = 0.6 m"},
        ],
        "findings": [
            {"key": "column_diameter_m", "value": 1.45, "unit": "m", "source_ref": "calc:hexgas"},
            {"key": "packing_height_m", "value": 8.2, "unit": "m", "source_ref": "calc:flooding"},
        ],
    }
    log_event(conn, event_type="hermes.turn_plan", actor="jarvis", target_type="ai_thread_interaction",
              target_id="inter-done-362", workspace_id=ws_id, payload=plan_done)
    log_event(conn, event_type="hermes.tool_started", actor="jarvis", target_type="agent_tool_call",
              target_id="call-done-1", workspace_id=ws_id,
              payload={"call_id": "call-done-1", "tool_name": "jarvis_retrieval_query", "interaction_id": "inter-done-362"})
    log_event(conn, event_type="hermes.tool_result", actor="jarvis", target_type="agent_tool_call",
              target_id="call-done-1", workspace_id=ws_id,
              payload={"call_id": "call-done-1", "tool_name": "jarvis_retrieval_query", "status": "succeeded", "interaction_id": "inter-done-362"})

    flow_run_id = create_flow_in_transaction(conn, task_kind="agent", requested_route_class="hermes:agent", workspace_id=ws_id)
    conn.execute(
        """
        INSERT INTO ai_thread_interactions (
            id, thread_id, request_id, request_digest, interaction_index, user_text,
            assistant_text, assistant_text_truncated, flow_id, persistence_state,
            created_at, updated_at
        ) VALUES (
            'inter-run-362', 'thread-walk-362', 'req-run', 'sha256:1', 2,
            'Calculate condenser heat duty and pressure drop.',
            NULL, 0, ?, 'dispatching',
            '2026-01-01T10:00:10+00:00', '2026-01-01T10:00:10+00:00'
        )
        """,
        (flow_run_id,),
    )
    plan_run = {
        "interaction_id": "inter-run-362",
        "steps": [
            {"id": "step-1", "title": "Check inlet vapor enthalpy", "status": "done"},
            {"id": "step-2", "title": "Calculate cooling water duty", "status": "in_progress"},
            {"id": "step-3", "title": "Estimate pressure drop", "status": "pending"},
        ],
        "findings": [
            {"key": "duty_mw", "value": 3.42, "unit": "MW", "source_ref": "thermo:delta_h"},
        ],
    }
    log_event(conn, event_type="hermes.turn_plan", actor="jarvis", target_type="ai_thread_interaction",
              target_id="inter-run-362", workspace_id=ws_id, payload=plan_run)
    log_event(conn, event_type="hermes.tool_started", actor="jarvis", target_type="agent_tool_call",
              target_id="call-run-1", workspace_id=ws_id,
              payload={"call_id": "call-run-1", "tool_name": "jarvis_turn_plan", "interaction_id": "inter-run-362"})
    conn.commit()
`;

  const init = spawnSync(python, ['-c', seedScript], { cwd: backend, env, encoding: 'utf8' });
  assert(init.status === 0, `isolated database initialization failed: ${init.error ?? init.stderr}`);

  log = openSync(join(evidence, 'backend.log'), 'w');
  server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], {
    cwd: backend,
    env,
    stdio: ['ignore', log, log],
  });

  for (let i = 0; ; i++) {
    assert(server.exitCode === null, 'backend exited; see backend.log');
    try {
      if ((await fetch(base + '/health')).ok) break;
    } catch {}
    assert(i < 120, 'backend did not become healthy');
    await delay(500);
  }

  const npxPw = join(process.env.HOME ?? '', '.npm/_npx/fd3bca3c548369c0/node_modules/playwright/index.mjs');
  const pwModule = process.env.PLAYWRIGHT_MODULE ?? (existsSync(npxPw) ? npxPw : 'playwright');
  const playwright = await import(pwModule);
  const chromium = playwright.chromium ?? playwright.default?.chromium;
  browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;

  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);

  page.on('pageerror', error => summary.page_errors.push(String(error)));
  page.on('request', request => summary.requests.push(`${request.method()} ${new URL(request.url()).pathname}`));

  await context.route('**/*', route => (new URL(route.request().url()).origin === base ? route.continue() : route.abort()));

  const shot = async name => {
    await page.screenshot({ path: join(evidence, name), fullPage: true });
    summary.screenshots.push(name);
  };

  await page.goto(base + '/', { waitUntil: 'networkidle' });

  // Open Jarvis sidecar
  const openBtn = page.getByRole('button', { name: /open jarvis/i });
  await openBtn.click();

  // Expand if dialog
  const expandBtn = page.getByRole('dialog', { name: 'Jarvis' }).getByRole('button', { name: 'Expand' });
  if (await expandBtn.isVisible()) {
    await expandBtn.click();
  }

  const jarvis = page.getByRole('complementary', { name: 'Jarvis' });
  await jarvis.waitFor();

  // Switch to the seeded thread if not active
  const historyBtn = jarvis.getByRole('button', { name: /conversations|history/i });
  if (await historyBtn.isVisible()) {
    await historyBtn.click();
    const threadItem = jarvis.getByRole('button', { name: /Walk Thread 362/i });
    if (await threadItem.isVisible()) {
      await threadItem.click();
    }
  }

  // 1. Verify live progress rendering under running turn
  const liveProgress = jarvis.locator('.jarvis-live-progress');
  await liveProgress.waitFor({ timeout: 5000 });
  assert(await liveProgress.isVisible(), 'live progress container not visible in running turn');

  // Verify plan checklist items
  assert(await liveProgress.getByText('Check inlet vapor enthalpy').isVisible(), 'completed step not visible in live plan');
  assert(await liveProgress.getByText('Calculate cooling water duty').isVisible(), 'in_progress step not visible in live plan');
  assert(await liveProgress.getByText('Estimate pressure drop').isVisible(), 'pending step not visible in live plan');

  // Verify recorded findings
  assert(await liveProgress.getByText('duty_mw').isVisible(), 'finding key not visible in live plan');
  assert(await liveProgress.getByText(/3\.42 MW/).isVisible(), 'finding value not visible in live plan');

  // Verify live tool step
  assert(await liveProgress.getByText(/Updating turn plan/i).isVisible(), 'live tool step not visible in running turn');

  await shot('live-plan-and-steps.png');
  summary.checks.push('verified running turn displays live plan checklist, recorded findings, and live tool steps below working indicator');

  // 2. Verify completed turn collapsible summary toggle
  const completedProgress = jarvis.locator('.jarvis-turn-progress');
  await completedProgress.waitFor({ timeout: 5000 });
  assert(await completedProgress.isVisible(), 'completed turn progress collapsible summary not visible');

  const summaryToggle = completedProgress.locator('summary');
  assert(await summaryToggle.isVisible(), 'summary toggle not visible');
  assert((await summaryToggle.innerText()).includes('Plan & tool activity'), 'summary toggle label mismatch');

  // Expand the collapsible section
  await summaryToggle.click();
  const content = completedProgress.locator('.jarvis-turn-progress__content');
  assert(await content.isVisible(), 'expanded content not visible after clicking summary toggle');

  assert(await content.getByText('Check gas flow rate and equilibrium').isVisible(), 'completed turn plan step 1 not visible');
  assert(await content.getByText('Compute packing height and diameter').isVisible(), 'completed turn plan step 2 not visible');
  assert(await content.getByText(/HETP = 0\.6 m/).isVisible(), 'completed turn plan step note not visible');
  assert(await content.getByText('column_diameter_m').isVisible(), 'completed turn finding 1 not visible');
  assert(await content.getByText(/1\.45 m/).isVisible(), 'completed turn finding 1 value not visible');

  await shot('completed-plan-expanded.png');
  summary.checks.push('verified completed turn keeps plan & tool activity collapsible under summary toggle and expands correctly');

  assert(summary.page_errors.length === 0, `page errors: ${summary.page_errors.join('; ')}`);
} catch (error) {
  failure = error;
  summary.failure = String(error.stack ?? error);
  await browser?.contexts()[0]?.pages()[0]?.screenshot({ path: join(evidence, 'failure.png') }).catch(() => {});
} finally {
  summary.result = failure ? 'fail' : 'pass';
  await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n');
  await browser?.close().catch(() => {});
  if (server) {
    server.kill('SIGTERM');
    await delay(500);
  }
  if (log !== undefined) closeSync(log);
  if (data) await rm(data, { recursive: true, force: true });
}

if (failure) {
  console.error(failure);
  process.exit(1);
}
console.log(`hermes saved plan walk: pass (${evidence})`);
