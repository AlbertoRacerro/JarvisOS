#!/usr/bin/env node
// Clean-profile sensitivity review against an isolated backend.
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
if (process.argv.includes('--help')) { console.log('node e2e/sensitivity_review_walk.mjs <evidence-dir>'); process.exit(0); }
if (!process.argv[2]) throw Error('evidence directory required');
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
const root = resolve(new URL('../', import.meta.url).pathname), backend = join(root, 'backend');
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, '.venv/bin/python');
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8236), base = `http://127.0.0.1:${port}`;
if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) throw Error('Invalid proof port');
await new Promise((ok, fail) => { const probe = createServer().once('error', error => fail(Error(`Cannot bind proof port ${port}: ${error.message}`))).once('listening', () => probe.close(ok)).listen(port, '127.0.0.1'); });
const evidence = resolve(process.argv[2]); await mkdir(evidence, { recursive: true });
const data = await mkdtemp(join(tmpdir(), 'jarvisos-236-')); await mkdir(join(data, 'host'));
const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: data, XDG_STATE_HOME: join(data, 'host'), LOCALAPPDATA: join(data, 'host') };
const log = openSync(join(evidence, 'backend.log'), 'w');
const server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], { cwd: backend, env, stdio: ['ignore', log, log] });
const summary = { spec: 236, head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(), steps: [], screenshots: [], inspection_writes: [], errors: [] };
let browser, failure;
try {
  for (let i = 0; ; i++) { if (server.exitCode !== null) throw Error('backend exited'); try { if ((await fetch(base + '/health')).ok) break; } catch {} if (i > 120) throw Error('backend timeout'); await delay(500); }
  const post = async (path, body) => { const r = await fetch(base + path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }); if (!r.ok) throw Error(`${path}: ${r.status} ${await r.text()}`); return r.json(); };
  const ws = await post('/workspaces', { name: 'Sensitivity Proof', slug: 'sensitivity-proof' });
  const make = (statement) => post('/memory/proposals', { workspace_id: ws.id, record_kind: 'assumption', statement });
  const current = await make('Current proof'); const stale = await make('Stale proof'); await make('Unlabelled proof');
  for (const record of [current, stale]) await post('/ai/sensitivity/labels', { workspace_id: ws.id, subject_ref: `assumption:${record.id}`, level: 'S1' });
  await post(`/memory/assumption/${stale.id}/promote`, {});
  browser = await chromium.launch({ headless: true }); const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } }); const page = await context.newPage(); page.setDefaultTimeout(30000);
  page.on('pageerror', e => summary.errors.push(String(e)));
  let inspecting = false; page.on('request', r => { if (inspecting && !['GET', 'HEAD', 'OPTIONS'].includes(r.method())) summary.inspection_writes.push(`${r.method()} ${new URL(r.url()).pathname}`); });
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  const picker = page.getByLabel('Project'); if (await picker.count()) await picker.selectOption({ label: 'Sensitivity Proof' });
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('link', { name: 'Review', exact: true }).click();
  await page.getByRole('heading', { name: 'Review', exact: true }).waitFor();
  await page.getByLabel('Status', { exact: true }).selectOption('all');
  const review = page.getByRole('region', { name: 'Sensitivity review' });
  for (const [name, expected, screenshot] of [['Current proof', 'Current', 'current'], ['Stale proof', 'Stale', 'stale'], ['Unlabelled proof', 'No sensitivity label recorded', 'no-label']]) {
    await page.getByRole('button', { name: new RegExp(name) }).click(); inspecting = true;
    await review.getByRole('button', { name: 'Check sensitivity' }).click(); await review.getByText(expected, { exact: true }).waitFor();
    if (name === 'Stale proof') await review.getByText('content_digest_mismatch').waitFor();
    const file = `${screenshot}.png`; await page.screenshot({ path: join(evidence, file) }); summary.screenshots.push(file); summary.steps.push(name); inspecting = false;
  }
  if (summary.inspection_writes.length || summary.errors.length) throw Error('Unexpected writes or page errors');
} catch (error) { failure = error; summary.failure = String(error.stack ?? error); } finally {
  summary.result = failure ? 'fail' : 'pass'; await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n'); await browser?.close(); server.kill('SIGTERM'); await delay(500); await rm(data, { recursive: true, force: true });
}
if (failure) throw failure;
console.log(`sensitivity review walk passed: ${evidence}`);
