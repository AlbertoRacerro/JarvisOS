#!/usr/bin/env node
// Clean-profile literature source file and datum check against an isolated backend.
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
if (process.argv.includes('--help')) { console.log('node e2e/literature_datum_walk.mjs <evidence-dir>'); process.exit(0); }
if (!process.argv[2]) throw Error('evidence directory required');
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
const root = resolve(new URL('../', import.meta.url).pathname), backend = join(root, 'backend');
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, '.venv/bin/python');
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8791), base = `http://127.0.0.1:${port}`;
if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) throw Error('Invalid proof port');
await new Promise((ok, fail) => { const probe = createServer().once('error', error => fail(Error(`Cannot bind proof port ${port}: ${error.message}`))).once('listening', () => probe.close(ok)).listen(port, '127.0.0.1'); });
const evidence = resolve(process.argv[2]); await mkdir(evidence, { recursive: true });
const data = await mkdtemp(join(tmpdir(), 'jarvisos-295-')); await mkdir(join(data, 'host'));
const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: data, XDG_STATE_HOME: join(data, 'host'), LOCALAPPDATA: join(data, 'host') };
const log = openSync(join(evidence, 'backend.log'), 'w');
const init = spawnSync(python, ['-c', 'from app.core.database import initialize_database\ninitialize_database()'], { cwd: backend, env, encoding: 'utf8' });
if (init.status !== 0) throw Error(`database initialization failed: ${init.stderr}`);
const server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], { cwd: backend, env, stdio: ['ignore', log, log] });
const summary = { spec: 295, head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(), steps: [], screenshots: [], errors: [] };
let browser, failure;
try {
  for (let i = 0; ; i++) { if (server.exitCode !== null) throw Error('backend exited'); try { if ((await fetch(base + '/health')).ok) break; } catch {} if (i > 120) throw Error('backend timeout'); await delay(500); }

  const post = async (path, body) => { const r = await fetch(base + path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }); if (!r.ok) throw Error(`${path}: ${r.status} ${await r.text()}`); return r.json(); };
  const ws = await post('/workspaces', { name: 'Literature Proof', slug: 'literature-proof' });
  const paper = join(data, 'paper.md');
  await writeFile(paper, '# Rate study\nBatch runs at 300 K.\nk = 1.2 x 10^-3 s^-1 at 300 K\nEnd of table.\n');
  const shot = async name => { await page.screenshot({ path: join(evidence, name) }); summary.screenshots.push(name); };
  browser = await chromium.launch({ headless: true }); const context = await browser.newContext({ viewport: { width: 1600, height: 1100 } }); const page = await context.newPage(); page.setDefaultTimeout(30000);
  page.on('pageerror', e => summary.errors.push(String(e)));
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  const picker = page.getByLabel('Project', { exact: true }); if (await picker.count()) await picker.selectOption({ label: 'Literature Proof' });
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('link', { name: 'Memory', exact: true }).click();
  await page.getByRole('navigation', { name: 'Project knowledge destinations' }).getByRole('link', { name: 'Literature', exact: true }).click();
  await page.getByRole('button', { name: '+ Register source' }).click();
  await page.getByLabel('Source title').fill('Rate study');
  await page.getByLabel('Source file (optional)').setInputFiles(paper);
  await shot('register.png'); summary.steps.push('Register source with a markdown file');
  await page.getByRole('button', { name: 'Save source', exact: true }).click();
  const source = page.locator('details[data-source-id]').first();
  await page.locator('details[data-source-id][data-backing-availability="available"]').waitFor();
  await source.getByText('Add a finding', { exact: true }).click();
  const form = source.locator('.literature-add-finding form');
  const addDatum = async (text, number) => {
    await form.getByLabel('Finding type').selectOption('datum');
    await form.getByLabel('Reported value').fill(text);
    await form.getByLabel('Unit (optional)').fill('1/s');
    await form.getByLabel('Number (optional)').fill(number);
    await form.getByLabel('Lines from (optional)').fill('3');
    await form.getByLabel('Lines to (optional)').fill('3');
    await form.getByRole('button', { name: 'Save raw finding' }).click();
    await source.getByRole('status').filter({ hasText: 'Saved as raw' }).waitFor();
  };
  await addDatum('k = 1.2 x 10^-3 s^-1', '0.0012');
  await source.locator('em', { hasText: 'Checked against the source' }).waitFor();
  await addDatum('k = 2.1 x 10^-3 s^-1', '0.0021');
  await source.locator('em', { hasText: 'Does not match the source' }).waitFor();
  await source.locator('details.literature-entry', { hasText: 'Does not match the source' }).locator('summary').first().click();
  const hint = await source.locator('details.literature-entry', { hasText: 'Does not match the source' }).locator('.literature-verdict p').first().innerText();
  if (!hint.trim()) throw Error('No hint shown for the failed check');
  summary.hint = hint;
  await shot('verdicts.png'); summary.steps.push('Matching datum reads "Checked against the source"; wrong number reads "Does not match the source" with a hint');
  const page1 = await (await fetch(`${base}/workspaces/${ws.id}/literature/sources`)).json();
  const verdicts = page1.items[0].entries.map(e => e.verdict).sort();
  if (verdicts.join() !== 'not_usable,usable') throw Error(`Unexpected persisted verdicts: ${verdicts}`);
  summary.persisted_verdicts = verdicts;
  const pdf = join(data, 'paper.pdf');
  const pdfInit = spawnSync(python, ['-c', "from tests.test_literature_pdf_pages import make_pdf; import sys; open(sys.argv[1], 'wb').write(make_pdf(['k = 1.2 x 10^-3 s^-1', 'Introduction']))", pdf], { cwd: backend, env, encoding: 'utf8' });
  if (pdfInit.status !== 0) throw Error(`PDF generation failed: ${pdfInit.stderr}`);
  await page.getByRole('button', { name: '+ Register source' }).click();
  await page.getByLabel('Source title').fill('PDF rate study');
  await page.getByLabel('Source file (optional)').setInputFiles(pdf);
  await page.getByRole('button', { name: 'Save source', exact: true }).click();
  const pdfSource = page.locator('details[data-source-id]').filter({ hasText: 'PDF rate study' });
  await pdfSource.getByText('Add a finding', { exact: true }).click();
  const pdfForm = pdfSource.locator('.literature-add-finding form');
  for (const pageNumber of ['1', '2']) {
    await pdfForm.getByLabel('Finding type').selectOption('datum');
    await pdfForm.getByLabel('Reported value').fill('rate');
    await pdfForm.getByLabel('Unit (optional)').fill('1/s');
    await pdfForm.getByLabel('Number (optional)').fill('0.0012');
    await pdfForm.getByLabel('Pages from (optional)').fill(pageNumber);
    await pdfForm.getByLabel('Pages to (optional)').fill(pageNumber);
    await pdfForm.getByRole('button', { name: 'Save raw finding' }).click();
    await pdfSource.locator('em', { hasText: pageNumber === '1' ? 'Checked against the source' : 'Does not match the source' }).waitFor();
  }
  await shot('pdf-verdicts.png');
  summary.steps.push('PDF page 1 matches; page 2 does not match');
  if (summary.errors.length) throw Error('Page errors');
} catch (error) { failure = error; summary.failure = String(error.stack ?? error); } finally {
  summary.result = failure ? 'fail' : 'pass'; await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n'); await browser?.close(); server.kill('SIGTERM'); await delay(500); await rm(data, { recursive: true, force: true });
}
if (failure) throw failure;
console.log(`literature datum walk passed: ${evidence}`);
