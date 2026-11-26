#!/usr/bin/env node
// Specs 380 (b) and 381: clean-profile browser walk of Settings > AI "Consulente", consult approval and a Sage answer.
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync, closeSync, existsSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

if (process.argv.includes('--help')) {
  console.log('node e2e/380-consult-walk.mjs <evidence-dir>');
  process.exit(0);
}
if (!process.argv[2]) throw Error('evidence directory required');

const root = resolve(new URL('../', import.meta.url).pathname);
const backend = join(root, 'backend');
const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });

const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8380);
const base = `http://127.0.0.1:${port}`;
const defaultPython = existsSync(join(backend, '.venv/bin/python'))
  ? join(backend, '.venv/bin/python')
  : 'python3';
const python = process.env.JARVISOS_PROOF_PYTHON ?? defaultPython;

const summary = {
  spec: 380,
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

let data, vaultDir, packDir, server, browser, failure, log;

try {
  assert(Number.isInteger(port) && port > 0 && port <= 65535 && port !== 8000, 'invalid proof port');
  await new Promise((ok, fail) =>
    createServer()
      .once('error', error => fail(Error(`cannot bind proof port ${port}: ${error.message}`)))
      .once('listening', function () { this.close(ok); })
      .listen(port, '127.0.0.1')
  );

  data = await mkdtemp(join(tmpdir(), 'jarvisos-380-data-'));
  await mkdir(join(data, 'host'));

  vaultDir = await mkdtemp(join(tmpdir(), 'jarvisos-380-vault-'));
  packDir = await mkdtemp(join(tmpdir(), 'jarvisos-380-pack-'));

  const briefText = '## Nucleus Brief\n\nThis is the authoritative nucleus brief for trusted cloud consults.\n\n**M1 — Thermal context**: Indicative heat-exchange overview.\n';
  await writeFile(join(packDir, 'brief.md'), briefText, 'utf8');
  await writeFile(
    join(packDir, 'jarvis_pack.toml'),
    'id = "zephyr"\n[sensitivity]\nterms = []\n[consult]\nbrief = "brief.md"\nnucleus_heading = "Nucleus Brief"\n',
    'utf8'
  );

  // Compute brief sha256 via python or crypto
  const crypto = await import('node:crypto');
  const briefSha256 = crypto.createHash('sha256').update(briefText, 'utf8').digest('hex');

  await writeFile(join(vaultDir, 'jarvis_vault.toml'), '', 'utf8');
  await writeFile(
    join(vaultDir, 'consult.toml'),
    `consult_mode = "ask"\nbrief_sha256 = "${briefSha256}"\n[[trusted_accounts]]\nid = "sub"\nagent = "claude"\nmodel = "fake-consultant"\ntraining_off_verified_on = 2026-11-25\n`,
    'utf8'
  );

  const env = Object.fromEntries(
    Object.entries(process.env).filter(([key]) => !/^(JARVIS|OPENAI|ANTHROPIC|OLLAMA|SCW_|SCALEWAY)/i.test(key))
  );
  Object.assign(env, {
    PYTHONPATH: backend,
    JARVISOS_DATA_ROOT: data,
    JARVISOS_VAULT_PATH: vaultDir,
    JARVISOS_PACK_PATH: packDir,
    JARVISOS_CONSULT_FAKE_TRANSPORT: '1',
    XDG_STATE_HOME: join(data, 'host'),
    LOCALAPPDATA: join(data, 'host'),
  });

  const seedScript = `
import hashlib
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.workspaces.service import create_workspace, default_workspace_payload
from app.modules.ai.settings import ensure_ai_settings
from app.modules.ai.token_flow_service import create_flow_in_transaction

initialize_database()
ensure_ai_settings()
ws = create_workspace(default_workspace_payload())
ws_id = ws.id

packet = "Question:\\nQuali condizioni ottimizzano la crescita delle radici?\\nContext:\\nTerreno sabbioso con drenaggio rapido."
packet_sha = hashlib.sha256(packet.encode("utf-8")).hexdigest()

with open_sqlite_connection() as conn:
    conn.execute(
        "INSERT INTO ai_threads (id, workspace_id, title, created_at, last_activity_at) VALUES ('thread-walk-380', ?, 'Walk Thread 380', '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:10+00:00')",
        (ws_id,),
    )
    flow_id = create_flow_in_transaction(conn, task_kind="agent", requested_route_class="hermes:agent", workspace_id=ws_id)
    conn.execute(
        "UPDATE ai_flows SET state = 'complete', updated_at = '2026-01-01T10:00:05+00:00', completed_at = '2026-01-01T10:00:05+00:00' WHERE id = ?",
        (flow_id,),
    )
    conn.execute(
        """
        INSERT INTO ai_thread_interactions (
            id, thread_id, request_id, request_digest, interaction_index, user_text,
            assistant_text, assistant_text_truncated, flow_id, persistence_state,
            created_at, updated_at
        ) VALUES (
            'turn-walk-380', 'thread-walk-380', 'req-walk-380', 'sha256:380', 1,
            'Ottimizzazione radici',
            'Ho preparato la richiesta per il consulente esperto:\\n\\n{{consult:c-walk-380}}\\n\\nIn attesa di approvazione.',
            0, ?, 'captured', '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:05+00:00'
        )
        """,
        (flow_id,),
    )
    conn.execute(
        """
        INSERT INTO consult_attempts (
            id, turn_id, thread_id, workspace_id, kind, modules_json, brief_sha256,
            packet_text, packet_sha256, account_id, model_id, state, mode, gate_steps_json,
            audit, created_at, updated_at
        ) VALUES (
            'c-walk-380', 'turn-walk-380', 'thread-walk-380', ?, 'physics', '["M1"]',
            '${briefSha256}', ?, ?, 'sub', 'fake-consultant', 'awaiting_approval', 'ask', '[]',
            0, '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:05+00:00'
        )
        """,
        (ws_id, packet, packet_sha),
    )
    # Spec 381: a second turn whose local_only consult was answered by Sage after a model swap.
    sage_answer = "Risposta locale di Sage: mantenere il drenaggio e irrigare a impulsi brevi."
    sage_flow_id = create_flow_in_transaction(conn, task_kind="agent", requested_route_class="hermes:agent", workspace_id=ws_id)
    conn.execute(
        "UPDATE ai_flows SET state = 'complete', updated_at = '2026-01-01T10:01:05+00:00', completed_at = '2026-01-01T10:01:05+00:00' WHERE id = ?",
        (sage_flow_id,),
    )
    conn.execute(
        """
        INSERT INTO ai_thread_interactions (
            id, thread_id, request_id, request_digest, interaction_index, user_text,
            assistant_text, assistant_text_truncated, flow_id, persistence_state,
            created_at, updated_at
        ) VALUES (
            'turn-walk-381', 'thread-walk-380', 'req-walk-381', 'sha256:381', 2,
            'Domanda locale',
            'Sage ha risposto in locale:\\n\\n{{consult:c-walk-381}}',
            0, ?, 'captured', '2026-01-01T10:01:00+00:00', '2026-01-01T10:01:05+00:00'
        )
        """,
        (sage_flow_id,),
    )
    conn.execute(
        """
        INSERT INTO consult_attempts (
            id, turn_id, thread_id, workspace_id, kind, modules_json, brief_sha256,
            packet_text, packet_sha256, account_id, model_id, state, mode, gate_steps_json,
            audit, local_only, created_at, updated_at
        ) VALUES (
            'c-walk-381', 'turn-walk-381', 'thread-walk-380', ?, 'physics', '["M1"]',
            '${briefSha256}', ?, ?, 'sage_local', 'sage-walk', 'sent', 'ask',
            '[{"step": "sage_swap", "swap_to_sage_s": 41.5, "swap_back_s": 38.25, "total_swap_s": 79.75}]',
            0, 1, '2026-01-01T10:01:00+00:00', '2026-01-01T10:01:05+00:00'
        )
        """,
        (ws_id, packet, packet_sha),
    )
    conn.execute(
        "INSERT INTO consult_answers (id, attempt_id, answer_text, answer_sha256, model_id, created_at) VALUES ('ans-walk-381', 'c-walk-381', ?, ?, 'sage-walk', '2026-01-01T10:01:04+00:00')",
        (sage_answer, hashlib.sha256(sage_answer.encode("utf-8")).hexdigest()),
    )
    conn.commit()
`;

  const init = spawnSync(python, ['-c', seedScript], { cwd: backend, env, encoding: 'utf8' });
  assert(init.status === 0, `database seed failed: ${init.error ?? init.stderr}`);

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

  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, locale: 'it-IT', timezoneId: 'UTC' });
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

  // 1. Settings -> AI tab -> Consulente card
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('tab', { name: 'AI', exact: true }).or(page.getByRole('button', { name: 'AI', exact: true })).first().click();

  const consultPanel = page.locator('[data-consult-panel]');
  await consultPanel.waitFor({ timeout: 10000 });
  await consultPanel.scrollIntoViewIfNeeded();
  await consultPanel.getByText('ask').waitFor({ timeout: 10000 });

  const panelText = await consultPanel.innerText();
  assert(panelText.includes('Consulente'), 'Consulente heading missing');
  assert(panelText.includes('ask'), 'Modalità ask missing');
  assert(panelText.includes('Sì'), 'Brief approvato missing');
  assert(panelText.includes('sub'), 'Trusted account sub missing');
  assert(panelText.includes('2026-11-25'), 'Training off verified date missing');

  await shot('01-settings-consult.png');
  summary.checks.push('Settings AI tab shows Consulente card with mode ask, brief approved, and trusted account');

  // 2. Open Jarvis sidecar and navigate to Walk Thread 380
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  const openBtn = page.getByRole('button', { name: /open jarvis/i });
  if (await openBtn.isVisible()) {
    await openBtn.click();
  }

  const expandBtn = page.getByRole('dialog', { name: 'Jarvis' }).getByRole('button', { name: 'Expand' });
  if (await expandBtn.isVisible()) {
    await expandBtn.click();
  }

  const jarvis = page.getByRole('complementary', { name: 'Jarvis' });
  await jarvis.waitFor();

  const consultCard = jarvis.locator('[data-consult-card="c-walk-380"]');
  if (!(await consultCard.isVisible())) {
    const historySummary = jarvis.locator('.jarvis-sidecar__history summary');
    if (await historySummary.isVisible()) {
      await historySummary.click();
      const threadItem = jarvis.getByRole('button', { name: /Walk Thread 380/i });
      if (await threadItem.isVisible()) {
        await threadItem.click();
      }
    }
  }

  await consultCard.waitFor({ timeout: 10000 });
  assert(await consultCard.isVisible(), 'ConsultCard c-walk-380 not visible in sidecar');
  assert(await consultCard.getAttribute('data-consult-state') === 'awaiting_approval', 'ConsultCard not awaiting approval');

  const approveBtn = consultCard.locator('.consult-btn--approve');
  const editBtn = consultCard.locator('.consult-btn--edit');
  const rejectBtn = consultCard.locator('.consult-btn--reject');
  assert(await approveBtn.isVisible(), 'Approva button not visible');
  assert(await editBtn.isVisible(), 'Modifica button not visible');
  assert(await rejectBtn.isVisible(), 'Rifiuta button not visible');

  await shot('02-consult-pending.png');
  summary.checks.push('Pending consult card rendered with Approva, Modifica, Rifiuta buttons');

  // 3. Approve the consult packet
  await approveBtn.click();

  // Wait for the card to transition to answered state
  const answeredCard = jarvis.locator('[data-consult-card="c-walk-380"][data-consult-state="sent"]');
  await answeredCard.waitFor({ timeout: 15000 });

  const answeredText = await answeredCard.innerText();
  assert(answeredText.includes('dice ·'), 'Answered card does not include "dice ·"');
  assert(answeredText.includes('simulated') || answeredText.includes('Expert consultation response'), 'Answer text missing simulated content');

  const detailsElem = answeredCard.locator('details summary');
  assert(await detailsElem.isVisible(), 'domanda inviata details summary missing');

  await shot('03-consult-answered.png');
  summary.checks.push('Consult approved and updated to answered state with fake transport response');

  // 4. Spec 381: a consult answered locally by Sage shows its model and the measured swap time
  const sageCard = jarvis.locator('[data-consult-card="c-walk-381"][data-consult-state="sent"]');
  await sageCard.waitFor({ timeout: 10000 });
  await sageCard.scrollIntoViewIfNeeded();
  const sageText = await sageCard.innerText();
  assert(sageText.includes('sage-walk dice ·'), 'Sage card does not name the Sage model');
  assert(sageText.includes('Risposta locale di Sage'), 'Sage answer text missing');
  const swapText = (await sageCard.getByTestId('consult-swap-time').innerText()).trim();
  assert(swapText === 'tempo di swap: 79.75s', `unexpected swap time text: ${swapText}`);
  await shot('04-sage-answered-swap.png');
  summary.checks.push('Sage-answered local consult shows model name and "tempo di swap: 79.75s"');

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
  if (vaultDir) await rm(vaultDir, { recursive: true, force: true });
  if (packDir) await rm(packDir, { recursive: true, force: true });
}

if (failure) {
  console.error(failure);
  process.exit(1);
}
console.log(`380 consult walk: pass (${evidence})`);
