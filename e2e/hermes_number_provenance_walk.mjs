#!/usr/bin/env node
// Spec 363: clean-profile browser walk proving visible number provenance badges in the Jarvis sidecar.
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync, closeSync, existsSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

if (process.argv.includes('--help')) {
  console.log('node e2e/hermes_number_provenance_walk.mjs <evidence-dir>');
  process.exit(0);
}
if (!process.argv[2]) throw Error('evidence directory required');

const root = resolve(new URL('../', import.meta.url).pathname);
const backend = join(root, 'backend');
const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });

const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8363);
const base = `http://127.0.0.1:${port}`;
const defaultPython = existsSync(join(backend, '.venv/bin/python'))
  ? join(backend, '.venv/bin/python')
  : 'python3';
const python = process.env.JARVISOS_PROOF_PYTHON ?? defaultPython;

const summary = {
  spec: 363,
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

  data = await mkdtemp(join(tmpdir(), 'jarvisos-363-'));
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

  // Seed database with workspace, thread, completed interaction with number provenance
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
        "INSERT INTO ai_threads (id, workspace_id, title, created_at, last_activity_at) VALUES ('thread-walk-363', ?, 'Walk Thread 363', '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:10+00:00')",
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
            'inter-done-363', 'thread-walk-363', 'req-done-363', 'sha256:0', 1,
            'Check column performance and cost estimate.',
            'Absorption efficiency reached 94.2% with a diameter of 1.45 m, but the unverified safety factor is 3.5.', 0,
            ?, 'captured', '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:05+00:00'
        )
        """,
        (flow_done_id,),
    )
    prov_payload = {
        "interaction_id": "inter-done-363",
        "workspace_id": ws_id,
        "verified_count": 2,
        "unverified_count": 1,
        "entries": [
            {
                "raw": "94.2",
                "value": 94.2,
                "status": "verified",
                "source_ref": "bench:run-col-01",
                "tool_name": "jarvis_bench_read",
                "route_hint": "/runs"
            },
            {
                "raw": "1.45",
                "value": 1.45,
                "status": "verified",
                "source_ref": "doi:10.1016/sample.2024",
                "tool_name": "jarvis_paper_search",
                "route_hint": "/memory/literature"
            },
            {
                "raw": "3.5",
                "value": 3.5,
                "status": "unverified"
            }
        ]
    }
    log_event(conn, event_type="hermes.number_provenance", actor="jarvis", target_type="ai_thread_interaction",
              target_id="inter-done-363", workspace_id=ws_id, payload=prov_payload)
    # Spec 366: one offline verification warning on the same answer.
    log_event(conn, event_type="hermes.verification_pass", actor="jarvis", target_type="ai_thread_interaction",
              target_id="inter-done-363", workspace_id=ws_id,
              payload={"status": "warnings", "warning_count": 1, "checks": [
                  {"kind": "magnitude", "severity": "warning",
                   "summary": "An answer quantity differs by at least 10x from a same-turn tool result."}]})
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
  const provContainer = jarvis.locator('.jarvis-number-provenance');
  if (!(await provContainer.isVisible())) {
    const historySummary = jarvis.locator('.jarvis-sidecar__history summary');
    if (await historySummary.isVisible()) {
      await historySummary.click();
      const threadItem = jarvis.getByRole('button', { name: /Walk Thread 363/i });
      if (await threadItem.isVisible()) {
        await threadItem.click();
      }
    }
  }

  // 1. Verify provenance summary bar presence and badge counts
  await provContainer.waitFor({ timeout: 5000 });
  assert(await provContainer.isVisible(), 'number provenance container not visible under completed answer');

  const summaryToggle = provContainer.locator('summary');
  assert(await summaryToggle.isVisible(), 'provenance summary toggle not visible');

  const verifiedBadge = provContainer.locator('[data-testid="provenance-summary-verified"]');
  assert(await verifiedBadge.isVisible(), 'verified count badge not visible in summary');
  assert((await verifiedBadge.innerText()).includes('2 verificati'), 'verified count text mismatch');

  const unverifiedBadge = provContainer.locator('[data-testid="provenance-summary-unverified"]');
  assert(await unverifiedBadge.isVisible(), 'unverified count badge not visible in summary');
  assert((await unverifiedBadge.innerText()).includes('1 non verificato'), 'unverified count text mismatch');

  const verificationWarning = jarvis.locator('.jarvis-verification-warning');
  assert(await verificationWarning.isVisible(), 'spec 366 verification warning badge not visible');
  assert((await verificationWarning.innerText()).includes('1 verification warning'), 'verification warning text mismatch');
  summary.checks.push('spec 366 verification warning badge renders "1 verification warning" under the answer');

  await shot('01-provenance-summary-badges.png');
  summary.checks.push('verified compact provenance bar renders verified (2) and unverified (1) summary badges');

  // 2. Expand chip list and verify chips
  await summaryToggle.click();

  const chipsContainer = provContainer.locator('.jarvis-provenance-chips');
  assert(await chipsContainer.isVisible(), 'expanded chips container not visible after click');

  // Verify verified chip 1: 94.2 -> bench:run-col-01 with route /runs
  const chip1 = chipsContainer.locator('.jarvis-provenance-chip--verified').filter({ hasText: '94.2' });
  assert(await chip1.isVisible(), 'verified chip for 94.2 not visible');
  const link1 = chip1.locator('a');
  assert(await link1.isVisible(), 'navigation link on verified chip 1 not visible');
  assert((await link1.innerText()).includes('bench:run-col-01'), 'source ref label mismatch on chip 1');
  assert((await link1.getAttribute('href')) === '/runs', 'route hint href mismatch on chip 1');

  // Verify verified chip 2: 1.45 -> doi:10.1016/sample.2024 with route /memory/literature
  const chip2 = chipsContainer.locator('.jarvis-provenance-chip--verified').filter({ hasText: '1.45' });
  assert(await chip2.isVisible(), 'verified chip for 1.45 not visible');
  const link2 = chip2.locator('a');
  assert(await link2.isVisible(), 'navigation link on verified chip 2 not visible');
  assert((await link2.innerText()).includes('doi:10.1016/sample.2024'), 'source ref label mismatch on chip 2');
  assert((await link2.getAttribute('href')) === '/memory/literature', 'route hint href mismatch on chip 2');

  // Verify unverified chip: 3.5 -> "Non verificato"
  const chip3 = chipsContainer.locator('.jarvis-provenance-chip--unverified').filter({ hasText: '3.5' });
  assert(await chip3.isVisible(), 'unverified chip for 3.5 not visible');
  assert(await chip3.locator('[data-testid="provenance-chip-warning"]').isVisible(), 'Non verificato warning not visible on unverified chip');
  assert(/non verificato/i.test(await chip3.innerText()), 'warning text missing on unverified chip');

  await shot('02-provenance-chips-expanded.png');
  summary.checks.push('verified expandable chip list renders numbers with backing route links and Non verificato warnings');

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
console.log(`hermes number provenance walk: pass (${evidence})`);
