#!/usr/bin/env node
// Clean-profile requirement edit against an isolated backend.
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
if (process.argv.includes('--help')) { console.log('node e2e/requirement_edit_walk.mjs <evidence-dir>'); process.exit(0); }
if (!process.argv[2]) throw Error('evidence directory required');
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
const root = resolve(new URL('../', import.meta.url).pathname), backend = join(root, 'backend');
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, '.venv/bin/python');
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8792), base = `http://127.0.0.1:${port}`;
if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) throw Error('Invalid proof port');
await new Promise((ok, fail) => { const probe = createServer().once('error', error => fail(Error(`Cannot bind proof port ${port}: ${error.message}`))).once('listening', () => probe.close(ok)).listen(port, '127.0.0.1'); });
const evidence = resolve(process.argv[2]); await mkdir(evidence, { recursive: true });
const data = await mkdtemp(join(tmpdir(), 'jarvisos-246-')); await mkdir(join(data, 'host'));
const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: data, XDG_STATE_HOME: join(data, 'host'), LOCALAPPDATA: join(data, 'host') };
const log = openSync(join(evidence, 'backend.log'), 'w');
const init = spawnSync(python, ['-c', 'from app.core.database import initialize_database\ninitialize_database()'], { cwd: backend, env, encoding: 'utf8' });
if (init.status !== 0) throw Error(`database initialization failed: ${init.stderr}`);
const server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], { cwd: backend, env, stdio: ['ignore', log, log] });
const summary = { spec: 246, head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(), steps: [], screenshots: [], inspection_writes: [], errors: [] };
let browser, failure;
try {
  for (let i = 0; ; i++) { if (server.exitCode !== null) throw Error('backend exited'); try { if ((await fetch(base + '/health')).ok) break; } catch {} if (i > 120) throw Error('backend timeout'); await delay(500); }
  const post = async (path, body) => { const r = await fetch(base + path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }); if (!r.ok) throw Error(`${path}: ${r.status} ${await r.text()}`); return r.json(); };
  const ws = await post('/workspaces', { name: 'Requirement Proof', slug: 'requirement-proof' });
  const requirement = await post(`/workspaces/${ws.id}/requirements`, { statement: 'Original proof requirement', status: 'active' });
  browser = await chromium.launch({ headless: true }); const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } }); const page = await context.newPage(); page.setDefaultTimeout(30000);
  page.on('pageerror', e => summary.errors.push(String(e)));
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  const picker = page.getByLabel('Project', { exact: true }); if (await picker.count()) await picker.selectOption({ label: 'Requirement Proof' });
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('link', { name: 'Memory', exact: true }).click();
  await page.getByRole('navigation', { name: 'Project knowledge destinations' }).getByRole('link', { name: 'Project Basis', exact: true }).click();
  const basis = page.getByRole('region', { name: 'Project Basis' });
  await basis.getByRole('button', { name: /Original proof requirement/ }).click();
  await basis.getByRole('button', { name: 'Edit', exact: true }).click();
  await basis.getByLabel('Edit statement').fill('Corrected proof requirement');
  await basis.getByLabel('Edit requirement').getByRole('button', { name: 'Preview impact' }).click();
  await basis.getByText('Impact complete', { exact: true }).waitFor();
  await page.screenshot({ path: join(evidence, 'preview.png') }); summary.screenshots.push('preview.png'); summary.steps.push('Preview impact');
  await basis.getByRole('button', { name: 'Approve all' }).click();
  await basis.getByText('Deterministic revalidation').waitFor();
  await basis.getByRole('button', { name: 'Final reconcile' }).click();
  await basis.getByRole('button', { name: /Corrected proof requirement/ }).waitFor();
  const response = await fetch(`${base}/requirements/${requirement.id}`); if (!response.ok) throw Error(`requirement GET: ${response.status}`);
  const persisted = await response.json(); if (persisted.statement !== 'Corrected proof requirement') throw Error(`Unexpected canonical statement: ${persisted.statement}`);
  await page.screenshot({ path: join(evidence, 'reconciled.png') }); summary.screenshots.push('reconciled.png'); summary.steps.push('Approve, reconcile, verify list and canonical GET');
  if (summary.errors.length) throw Error('Unexpected writes or page errors');
} catch (error) { failure = error; summary.failure = String(error.stack ?? error); } finally {
  summary.result = failure ? 'fail' : 'pass'; await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n'); await browser?.close(); server.kill('SIGTERM'); await delay(500); await rm(data, { recursive: true, force: true });
}
if (failure) throw failure;
console.log(`requirement edit walk passed: ${evidence}`);
