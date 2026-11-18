#!/usr/bin/env node
// Spec 309: source and check-state chip on numeric inputs.
// Clean-profile walk: from `/` opens the Process editor on a seeded draft,
// inspects a numeric input, types a value, and verifies the stored `Letteratura · Da fonte` chip
// turns into `Digitato · Non verificato` once edited, with no console or page errors.
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

if (process.argv.includes('--help')) {
  console.log('node e2e/numeric_source_chip_walk.mjs <evidence-dir>');
  process.exit(0);
}
if (!process.argv[2]) throw new Error('evidence directory required');

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
const root = resolve(new URL('../', import.meta.url).pathname);
const backend = join(root, 'backend');
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, '.venv/bin/python');
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8796);
const base = `http://127.0.0.1:${port}`;

if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) {
  throw new Error('Invalid proof port');
}

await new Promise((ok, fail) => {
  const probe = createServer()
    .once('error', (error) => fail(new Error(`Cannot bind proof port ${port}: ${error.message}`)))
    .once('listening', () => probe.close(ok))
    .listen(port, '127.0.0.1');
});

const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });
const data = await mkdtemp(join(tmpdir(), 'jarvisos-309-'));
await mkdir(join(data, 'host'));

const env = {
  ...process.env,
  PYTHONPATH: backend,
  JARVISOS_DATA_ROOT: data,
  XDG_STATE_HOME: join(data, 'host'),
  LOCALAPPDATA: join(data, 'host'),
};
delete env.GH_TOKEN;
delete env.GITHUB_TOKEN;
delete env.JARVISOS_GITHUB_READ_TOKEN;

const log = openSync(join(evidence, 'backend.log'), 'w');

// Seed storage with a process draft containing a Heater unit
const seedScript = `
import sys
sys.path.insert(0, 'tests')
from app.core.bootstrap import initialize_storage
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import AddUnit, SetUnitParams, DraftQuantity

initialize_storage(seed_default=True)
created = draft.create_draft('main', 'Source chip proof draft')
ops = [
    AddUnit(op='add_unit', id='h1', type='Heater', tag='H1', x=300, y=150),
    SetUnitParams(op='set_unit_params', unit='h1', values={
        'outlet_temperature': DraftQuantity(value=350.0, unit='K', source={'kind': 'literature', 'check_state': 'source_checked', 'label': 'Valore di prova da letteratura'})
    })
]
draft.patch('main', created['draft_id'], created['revision'], ops)
`;

const init = spawnSync(python, ['-c', seedScript], { cwd: backend, env, encoding: 'utf8' });
if (init.status !== 0) throw new Error(`seed failed: ${init.stderr}`);

const server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], {
  cwd: backend,
  env,
  stdio: ['ignore', log, log],
});

const summary = {
  spec: 309,
  head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(),
  steps: [],
  screenshots: [],
  errors: [],
};

const shot = async (page, name, step) => {
  await page.screenshot({ path: join(evidence, name), fullPage: true });
  summary.screenshots.push(name);
  summary.steps.push(step);
};

let browser;
let failure;

try {
  for (let i = 0; ; i++) {
    if (server.exitCode !== null) throw new Error('backend exited prematurely');
    try {
      const res = await fetch(base + '/health');
      if (res.ok) break;
    } catch {}
    if (i > 120) throw new Error('backend start timeout');
    await delay(500);
  }

  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);

  page.on('pageerror', (err) => {
    summary.errors.push(`Page error: ${String(err)}`);
  });
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      summary.errors.push(`Console error: ${msg.text()}`);
    }
  });

  // 1. Navigate to `/`
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  await shot(page, '01-process-editor.png', 'Navigated to / and opened the Process editor flowsheet');

  // 2. Select unit H1 on the flowsheet
  const unitNode = page.locator('[data-testid="node-H1"]');
  await unitNode.waitFor({ state: 'visible' });
  await unitNode.click();

  // 3. Inspect the unit: numeric input for Outlet temperature is visible
  const tempInput = page.getByRole('textbox', { name: 'Outlet temperature' });
  await tempInput.waitFor({ state: 'visible' });
  const tempField = page.locator('label.draft-quantity').filter({ has: tempInput });

  // 4. Verify the stored source chip: "Letteratura · Da fonte"
  const initialChip = tempField.locator('.source-chip[data-source-kind="literature"][data-check-state="source_checked"]');
  await initialChip.waitFor({ state: 'visible' });
  const initialChipText = await initialChip.innerText();
  if (!initialChipText.includes('Letteratura · Da fonte')) {
    throw new Error(`Expected 'Letteratura · Da fonte', got '${initialChipText}'`);
  }
  await shot(page, '02-unit-selected-stored-chip.png', 'Selected Heater H1 and verified stored source chip is Letteratura · Da fonte');

  // 5. Type a new value in the numeric input
  await tempInput.fill('375');
  await shot(page, '03-value-typed-chip-visible.png', 'Typed a new value (375) and verified source chip shows Digitato · Non verificato');

  const typedChip = tempField.locator('.source-chip[data-source-kind="typed"][data-check-state="unchecked"]');
  await typedChip.waitFor({ state: 'visible' });
  const typedChipText = await typedChip.innerText();
  if (!typedChipText.includes('Digitato · Non verificato')) {
    throw new Error(`Expected 'Digitato · Non verificato' after typing, got '${typedChipText}'`);
  }

  summary.verified_chip_text = typedChipText;

  if (summary.errors.length > 0) {
    throw new Error(`Unexpected errors occurred during walk: ${summary.errors.join('; ')}`);
  }
} catch (error) {
  failure = error;
  summary.failure = String(error.stack ?? error);
  try {
    await browser?.contexts()[0]?.pages()[0]?.screenshot({
      path: join(evidence, 'failure.png'),
      fullPage: true,
    });
  } catch {}
} finally {
  summary.result = failure ? 'fail' : 'pass';
  await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n');
  await browser?.close();
  server.kill('SIGTERM');
  await delay(500);
  await rm(data, { recursive: true, force: true });
}

if (failure) throw failure;
console.log(`numeric source chip walk passed: ${evidence}`);
