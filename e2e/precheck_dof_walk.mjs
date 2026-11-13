#!/usr/bin/env node
// Spec 257: clean-profile walk for the PBR loop-hydraulics pre-check and the degrees-of-freedom panel.
// Seeds a draft whose PBR mean temperature is outside the water EOS, a heater whose outlet temperature a stored run
// supplies (available) and a heater nobody specified (unset); everything after `/` is driven through visible controls.
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

if (process.argv.includes('--help')) {
  console.log('node e2e/precheck_dof_walk.mjs <evidence-dir>');
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
const data = await mkdtemp(join(tmpdir(), 'jarvisos-257-'));
await mkdir(join(data, 'host'));
const env = {
  ...process.env,
  PYTHONPATH: backend,
  JARVISOS_DATA_ROOT: data,
  XDG_STATE_HOME: join(data, 'host'),
  LOCALAPPDATA: join(data, 'host')
};
const log = openSync(join(evidence, 'backend.log'), 'w');

const seedScript = `
import sys
sys.path.insert(0, 'tests')
from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import AddStream, AddUnit, Connect, SetUnitParams
from plumbing_170_support import pbr_ops, pbr_quantities
initialize_storage(seed_default=True)
created = draft.create_draft('main', 'PBR loop pre-check')
ops = pbr_ops(params=False) + [
    SetUnitParams(op='set_unit_params', unit='pbr', values=pbr_quantities(
        temperature_mean=(2500.0, 'K'), temperature_amplitude=(0.1, 'K'))),
    AddUnit(op='add_unit', id='h1', type='Heater', tag='H1', x=300, y=0),
    AddStream(op='add_stream', id='heated', tag='Heated', x=400, y=0),
    Connect(op='connect', stream='product', end='target', unit='h1', port=0),
    Connect(op='connect', stream='heated', end='source', unit='h1', port=0),
    AddUnit(op='add_unit', id='h2', type='Heater', tag='H2', x=500, y=0),
    AddStream(op='add_stream', id='final', tag='Final', x=600, y=0),
    Connect(op='connect', stream='heated', end='target', unit='h2', port=0),
    Connect(op='connect', stream='final', end='source', unit='h2', port=0),
]
draft.patch('main', created['draft_id'], created['revision'], ops)
now = '2026-11-10T00:00:00+00:00'
with open_sqlite_connection() as c:
    c.execute("INSERT INTO simulation_runs (id, workspace_id, status, created_at) VALUES ('run-walk-257', 'main', 'succeeded', ?)", (now,))
    c.execute("INSERT INTO simulation_result_batches (id, workspace_id, run_id, source_payload_digest, extractor_version, notification_state, created_at) VALUES ('batch-walk-257', 'main', 'run-walk-257', 'digest-walk', '1.0', 'none', ?)", (now,))
    c.execute("INSERT INTO simulation_result_values (id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version, quantity, value_text, unit, conditions_json, status, created_at) VALUES ('val-walk-257', 'batch-walk-257', 'h1_outlet', 'digest-walk', 'extractor-walk', '1.0', 'H1.outlet_temperature', '350.0', 'K', '{}', 'passed', ?)", (now,))
    c.commit()
`;

const init = spawnSync(python, ['-c', seedScript], { cwd: backend, env, encoding: 'utf8' });
if (init.status !== 0) throw Error(`seed failed: ${init.stderr}`);

const server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], {
  cwd: backend,
  env,
  stdio: ['ignore', log, log]
});
const summary = {
  spec: 257,
  head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(),
  steps: [],
  screenshots: [],
  errors: []
};
const shot = async (page, name, step) => {
  await page.screenshot({ path: join(evidence, name), fullPage: true });
  summary.screenshots.push(name);
  summary.steps.push(step);
};
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
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  page.on('pageerror', e => summary.errors.push(String(e)));
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  // `/` opens Design → Plant on the only draft; blockers open the readiness guidance below the flowsheet.
  const findings = page.getByRole('group', { name: 'Draft findings' });
  const blocker = findings.locator('li[data-severity="blocker"]').filter({ hasText: /^PBR/ }).filter({ hasText: /temperature|water/i });
  await blocker.first().scrollIntoViewIfNeeded();
  await blocker.first().waitFor();
  summary.blocker_text = await blocker.first().innerText();
  await shot(page, '01-prerun-hydraulics-blocker.png', 'Opened the seeded draft from / and saw the PBR loop-hydraulics Pre-Run blocker');
  await page.getByRole('button', { name: 'Degrees of freedom' }).click();
  const panel = page.getByRole('region', { name: 'Degrees of freedom' });
  await panel.getByText('H1 · ', { exact: false }).first().waitFor();
  const h1 = panel.getByRole('listitem').filter({ hasText: /^H1 · Outlet temperature/i });
  await h1.getByText('Available from a run').waitFor();
  await h1.getByText('Run run-walk-257').waitFor();
  const h2 = panel.getByRole('listitem').filter({ hasText: /^H2 · Outlet temperature/i });
  await h2.getByText('Unset', { exact: true }).waitFor();
  await shot(page, '02-dof-available-and-unset.png', 'Opened the Degrees of freedom panel: H1 outlet temperature available from run-walk-257, H2 outlet temperature unset');
  if (summary.errors.length) throw Error(`Unexpected page errors: ${summary.errors.join('; ')}`);
} catch (error) {
  failure = error;
  summary.failure = String(error.stack ?? error);
  try { await browser?.contexts()[0]?.pages()[0]?.screenshot({ path: join(evidence, 'failure.png'), fullPage: true }); } catch {}
} finally {
  summary.result = failure ? 'fail' : 'pass';
  await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n');
  await browser?.close();
  server.kill('SIGTERM');
  await delay(500);
  await rm(data, { recursive: true, force: true });
}
if (failure) throw failure;
console.log(`precheck dof walk passed: ${evidence}`);
