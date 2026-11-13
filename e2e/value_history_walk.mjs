#!/usr/bin/env node
// Spec 258: clean-profile walk for value history: which value versions a run used,
// newer version exists marker, version history chain, and comparing two versions.
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

if (process.argv.includes('--help')) {
  console.log('node e2e/value_history_walk.mjs <evidence-dir>');
  process.exit(0);
}
if (!process.argv[2]) throw Error('evidence directory required');

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
const root = resolve(new URL('../', import.meta.url).pathname), backend = join(root, 'backend');
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, '.venv/bin/python');
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8794), base = `http://127.0.0.1:${port}`;
if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) throw Error('Invalid proof port');

await new Promise((ok, fail) => {
  const probe = createServer()
    .once('error', error => fail(Error(`Cannot bind proof port ${port}: ${error.message}`)))
    .once('listening', () => probe.close(ok))
    .listen(port, '127.0.0.1');
});

const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });
const data = await mkdtemp(join(tmpdir(), 'jarvisos-258-'));
await mkdir(join(data, 'host'));
const env = {
  ...process.env,
  PYTHONPATH: backend,
  JARVISOS_DATA_ROOT: data,
  XDG_STATE_HOME: join(data, 'host'),
  LOCALAPPDATA: join(data, 'host')
};
const log = openSync(join(evidence, 'backend.log'), 'w');

// Initialize database with a fake AI job row so parameter replacement proposal can succeed
const init = spawnSync(python, ['-c', [
  'from app.core.database import initialize_database, open_sqlite_connection',
  'from app.modules.events.service import utc_now',
  'initialize_database()',
  "with open_sqlite_connection() as k: k.execute(\"INSERT INTO ai_jobs (id, created_at, status, task_kind, route_reason_json) VALUES ('walk-seed-job', ?, 'succeeded', 'walk_seed', '{}')\", (utc_now(),)); k.commit()"
].join('\n')], { cwd: backend, env, encoding: 'utf8' });

if (init.status !== 0) throw Error(`database initialization failed: ${init.stderr}`);

const server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], { cwd: backend, env, stdio: ['ignore', log, log] });
const summary = { spec: 258, head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(), steps: [], screenshots: [], errors: [] };
let browser, failure;

try {
  for (let i = 0; ; i++) {
    if (server.exitCode !== null) throw Error('backend exited');
    try {
      if ((await fetch(base + '/health')).ok) break;
    } catch {}
    if (i > 120) throw Error('backend timeout');
    await delay(500);
  }

  const post = async (path, body) => {
    const r = await fetch(base + path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (!r.ok) throw Error(`${path}: ${r.status} ${await r.text()}`);
    return r.json();
  };

  // 1. Seed workspace through public API routes
  const ws = await post('/workspaces', { name: 'Value History Proof', slug: 'val-hist-proof' });

  // 2. Create canonical parameter
  const oldParam = await post(`/workspaces/${ws.id}/parameters`, {
    name: 'tube_length',
    value: '10.0',
    unit: 'm',
    value_status: 'accepted',
    status: 'accepted'
  });

  // 3. Create model spec and implementation with schema v2 input contract
  const spec = await post(`/workspaces/${ws.id}/model-specs`, {
    title: 'Tube Sizing Calculation',
    engineering_question: 'Demonstrate run value history tracking.'
  });

  const script = "import json\nwith open('result.json', 'w') as f:\n    json.dump({'outputs': {'area': {'value': 31.4, 'unit': 'm^2', 'quantity': 'area'}}}, f)";
  const impl = await post(`/workspaces/${ws.id}/model-implementations`, {
    model_spec_id: spec.id,
    version_label: 'impl-v2',
    implementation_kind: 'calc_v0',
    script_text: script,
    input_contract: {
      schema_version: 2,
      evaluation_mode: 'forward',
      variables: [
        {
          name: 'tube_length',
          label: 'Tube Length',
          unit: 'm',
          physical_dimension: 'length',
          required: true,
          category: 'design',
          description: 'Canonical tube length parameter'
        }
      ]
    }
  });

  // 4. Create runner job linking tube_length to oldParam
  const job = await post(`/workspaces/${ws.id}/runner-jobs`, {
    model_version_id: impl.id,
    input_set: {
      tube_length: {
        value: 10.0,
        unit: 'm',
        source_parameter_id: oldParam.id,
        source_parameter_updated_at: oldParam.updated_at
      }
    }
  });

  // 5. Run the job to record simulation run with linked parameter
  await post(`/runner-jobs/${job.runner_job.id}/run`, {});

  // 6. Supersede oldParam with a new version
  const replacement = await post('/memory/proposals', {
    workspace_id: ws.id,
    record_kind: 'parameter',
    name: 'tube_length',
    value: '15.0',
    unit: 'm',
    supersedes_parameter_id: oldParam.id,
    source_ai_job_id: 'walk-seed-job'
  });
  await post(`/memory/parameter/${replacement.id}/promote-replacement`, {});

  // 7. Launch browser walk from / with visible controls
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  page.on('pageerror', e => summary.errors.push(String(e)));

  await page.goto(base + '/', { waitUntil: 'networkidle' });

  // Switch to the proof workspace if project picker is present
  const picker = page.getByLabel('Project', { exact: true });
  if (await picker.count()) {
    await picker.selectOption({ label: 'Value History Proof' });
  }

  // Navigate to Runs workbench using rail navigation
  const designTools = page.getByRole('navigation', { name: 'Design tools' });
  const moreDetails = designTools.locator('details');
  if (await moreDetails.count()) {
    const isOpen = await moreDetails.getAttribute('open');
    if (isOpen === null) {
      await designTools.getByText('More Design tools').click();
    }
  }
  await designTools.getByRole('link', { name: 'Runs', exact: true }).click();
  await page.getByRole('heading', { name: 'Runs', exact: true }).waitFor();

  // Select the executed run in the runs list
  const runRow = page.getByRole('button', { name: /Tube Sizing Calculation|Unnamed run/ }).first();
  await runRow.waitFor();
  await runRow.click();

  // Inspect Values used section
  const valuesUsed = page.getByRole('region', { name: 'Values used' });
  await valuesUsed.waitFor();
  await valuesUsed.getByText('tube_length').waitFor();
  await valuesUsed.getByText('newer version exists').waitFor();

  await page.screenshot({ path: join(evidence, 'values-used.png') });
  summary.screenshots.push('values-used.png');
  summary.steps.push('Inspected run detail and observed Values used with newer version exists marker');

  // Open History view from Values used row
  await valuesUsed.getByRole('button', { name: 'History' }).first().click();

  const historyDialog = page.getByRole('dialog', { name: 'Value history' });
  await historyDialog.waitFor();
  await historyDialog.getByText('Version chain').waitFor();
  await historyDialog.getByText('Version 0').waitFor();
  await historyDialog.getByText('Version 1').waitFor();

  await page.screenshot({ path: join(evidence, 'version-chain.png') });
  summary.screenshots.push('version-chain.png');
  summary.steps.push('Opened History view from Values used and verified ordered version chain');

  // Compare the two versions
  await historyDialog.getByLabel('Select Version A').selectOption({ index: 1 });
  await historyDialog.getByLabel('Select Version B').selectOption({ index: 2 });
  await historyDialog.getByRole('button', { name: 'Compare', exact: true }).click();

  const comparison = historyDialog.getByRole('region', { name: 'Value comparison result' });
  await comparison.waitFor();
  await comparison.getByText('Runs used').waitFor();
  await comparison.getByText('Requirement checks').waitFor();

  await page.screenshot({ path: join(evidence, 'version-comparison.png') });
  summary.screenshots.push('version-comparison.png');
  summary.steps.push('Executed comparison between Version 0 and Version 1 showing both sides and their users');

  // Close the History dialog
  await historyDialog.getByRole('button', { name: 'Close history view' }).click();
  await historyDialog.waitFor({ state: 'detached' });

  if (summary.errors.length) throw Error(`Unexpected browser errors: ${summary.errors.join('; ')}`);
} catch (error) {
  failure = error;
  summary.failure = String(error.stack ?? error);
} finally {
  summary.result = failure ? 'fail' : 'pass';
  await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n');
  await browser?.close();
  server.kill('SIGTERM');
  await delay(500);
  await rm(data, { recursive: true, force: true });
}

if (failure) throw failure;
console.log(`value history walk passed: ${evidence}`);
