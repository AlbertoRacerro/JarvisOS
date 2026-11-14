#!/usr/bin/env node
// Spec 258: clean-profile walk for value history: which value versions a run used,
// newer version exists marker, version history chain, comparing two versions, and
// simulation result value history.
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

// Seed calculation model and run via internal runner service (offline test runner),
// following the sibling walk e2e/sim_results_requirement_verification_walk.mjs.
// Direct INSERT INTO ai_jobs is omitted because parameter replacement uses the public
// parameter proposal and promote-replacement routes which do not require an ai_job row.
const seedScript = `
import json
from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.modules.runner.models import ModelImplementationCreate, RunnerJobCreate
from app.modules.runner.service import (
    create_model_implementation,
    create_runner_job,
    run_runner_job,
)

initialize_storage(seed_default=True)

ws_id = "val-hist-proof"
now = "2026-11-13T00:00:00+00:00"
with open_sqlite_connection() as conn:
    conn.execute(
        "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
        (ws_id, "Value History Proof", "val-hist-proof", now, now),
    )
    conn.execute(
        """
        INSERT INTO parameters (
            id, workspace_id, name, value, unit, value_status, status, origin,
            created_at, updated_at, lifecycle_state
        ) VALUES ('param-length-v0', ?, 'tube_length', '10.0', 'm', 'accepted', 'accepted', 'user', ?, ?, 'active')
        """,
        (ws_id, now, now),
    )
    conn.execute(
        """
        INSERT INTO model_specs (
            id, workspace_id, title, engineering_question, status, maturity_status, schema_version, created_at, updated_at
        ) VALUES ('spec-sizing', ?, 'Tube Sizing Calculation', 'Demonstrate run value history tracking.', 'active', 'draft', 1, ?, ?)
        """,
        (ws_id, now, now),
    )
    conn.commit()

v2_contract = {
    "schema_version": 2,
    "evaluation_mode": "forward",
    "variables": [
        {
            "name": "tube_length",
            "label": "Tube Length",
            "unit": "m",
            "physical_dimension": "length",
            "required": True,
            "category": "design",
            "description": "Canonical tube length parameter",
        }
    ],
}
script = '''import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({'outputs': {'area': {'value': 31.4, 'unit': 'm^2', 'quantity': 'area', 'conditions': {'regime': 'nominal'}}}}, f)
'''
impl = create_model_implementation(
    ws_id,
    ModelImplementationCreate(
        model_spec_id='spec-sizing',
        version_label='impl-v2',
        implementation_kind='calc_v0',
        script_text=script,
        input_contract=v2_contract,
    ),
)
job = create_runner_job(
    ws_id,
    RunnerJobCreate(
        model_version_id=impl.id,
        run_label='Tube Sizing Calculation',
        input_set={
            'tube_length': {
                'value': 10.0,
                'unit': 'm',
                'source_parameter_id': 'param-length-v0',
                'source_parameter_updated_at': now,
            }
        },
    ),
)
run_res = run_runner_job(job.runner_job.id)
assert run_res.error is None
`;

const init = spawnSync(python, ['-c', seedScript], { cwd: backend, env, encoding: 'utf8' });
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

  // Supersede param-length-v0 with a new version through public API routes
  const replacement = await post('/workspaces/val-hist-proof/parameters', {
    name: 'tube_length',
    value: '15.0',
    unit: 'm',
    status: 'proposed',
    supersedes_parameter_id: 'param-length-v0'
  });
  await post(`/memory/parameter/${replacement.id}/promote-replacement`, {});

  // Confirm exception on the ingested simulation result value through public API route
  const batches = await (await fetch(base + '/project-knowledge/workspaces/val-hist-proof/result-batches')).json();
  if (batches.length > 0 && batches[0].values.length > 0) {
    const areaValue = batches[0].values.find(v => v.output_name === 'area') ?? batches[0].values[0];
    await post(`/project-knowledge/result-values/${areaValue.id}/resolve`, {
      workspace_id: 'val-hist-proof',
      action: 'confirm',
      reason: 'Engineering review accepted calculated area'
    });
  }

  // Launch browser walk from / with visible controls
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
  await historyDialog.getByText('Parameter History').waitFor();
  await historyDialog.getByText('tube_length').waitFor();
  await historyDialog.getByText('Version chain').waitFor();
  await historyDialog.getByText('Version 0', { exact: true }).waitFor();
  await historyDialog.getByText('Version 1', { exact: true }).waitFor();

  await page.screenshot({ path: join(evidence, 'version-chain.png') });
  summary.screenshots.push('version-chain.png');
  summary.steps.push('Opened History view from Values used and verified ordered version chain');

  // Compare the two versions
  await historyDialog.getByLabel('Select Version A').selectOption({ index: 1 });
  await historyDialog.getByLabel('Select Version B').selectOption({ index: 2 });
  await historyDialog.getByRole('button', { name: 'Compare', exact: true }).click();

  const comparison = historyDialog.getByRole('region', { name: 'Value comparison result' });
  await comparison.waitFor();
  await comparison.getByText('Runs used', { exact: true }).waitFor();
  await comparison.getByText('Requirement checks', { exact: true }).waitFor();

  await page.screenshot({ path: join(evidence, 'version-comparison.png') });
  summary.screenshots.push('version-comparison.png');
  summary.steps.push('Executed comparison between Version 0 and Version 1 showing both sides and their users');

  // Close the History dialog
  await historyDialog.getByRole('button', { name: 'Close history view' }).click();
  await historyDialog.waitFor({ state: 'detached' });

  // Navigate to Memory -> Project Basis to inspect simulation result value history
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('link', { name: 'Memory', exact: true }).click();
  await page.getByRole('navigation', { name: 'Project knowledge destinations' }).getByRole('link', { name: 'Project Basis', exact: true }).click();

  const pkPanel = page.getByRole('region', { name: 'Project Knowledge' });
  await pkPanel.waitFor();
  const batchesSection = pkPanel.getByRole('region', { name: 'Simulation result batches' });
  await batchesSection.waitFor();
  await batchesSection.getByText('area', { exact: true }).waitFor();

  // Open History view for the simulation result value
  await batchesSection.getByRole('button', { name: 'History' }).first().click();

  const resHistoryDialog = page.getByRole('dialog', { name: 'Value history' });
  await resHistoryDialog.waitFor();
  await resHistoryDialog.getByText('Result Value History').waitFor();
  await resHistoryDialog.getByText('area', { exact: true }).waitFor();
  await resHistoryDialog.getByText('Version chain').waitFor();
  await resHistoryDialog.getByText('Version 0', { exact: true }).waitFor();

  await page.screenshot({ path: join(evidence, 'sim-result-value-history.png') });
  summary.screenshots.push('sim-result-value-history.png');
  summary.steps.push('Opened simulation result value History view from Project Basis and verified Version chain');

  // Close the Result Value History dialog
  await resHistoryDialog.getByRole('button', { name: 'Close history view' }).click();
  await resHistoryDialog.waitFor({ state: 'detached' });

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
