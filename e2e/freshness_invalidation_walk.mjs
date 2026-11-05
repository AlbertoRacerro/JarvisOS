#!/usr/bin/env node
// Clean-profile freshness invalidation against an isolated backend.
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
if (process.argv.includes('--help')) { console.log('node e2e/freshness_invalidation_walk.mjs <evidence-dir>'); process.exit(0); }
if (!process.argv[2]) throw Error('evidence directory required');
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
const root = resolve(new URL('../', import.meta.url).pathname), backend = join(root, 'backend');
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, '.venv/bin/python');
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8791), base = `http://127.0.0.1:${port}`;
if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) throw Error('Invalid proof port');
await new Promise((ok, fail) => { const probe = createServer().once('error', error => fail(Error(`Cannot bind proof port ${port}: ${error.message}`))).once('listening', () => probe.close(ok)).listen(port, '127.0.0.1'); });
const evidence = resolve(process.argv[2]); await mkdir(evidence, { recursive: true });
const data = await mkdtemp(join(tmpdir(), 'jarvisos-245-')); await mkdir(join(data, 'host'));
const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: data, XDG_STATE_HOME: join(data, 'host'), LOCALAPPDATA: join(data, 'host') };
const log = openSync(join(evidence, 'backend.log'), 'w');
const init = spawnSync(python, ['-c', [
  'from app.core.database import initialize_database, open_sqlite_connection',
  'from app.modules.events.service import utc_now',
  'initialize_database()',
  // Proposals need a source AI job; seed one finished fake job row in the temporary data root.
  "with open_sqlite_connection() as k: k.execute(\"INSERT INTO ai_jobs (id, created_at, status, task_kind, route_reason_json) VALUES ('sensitivity-walk-job', ?, 'succeeded', 'walk_seed', '{}')\", (utc_now(),)); k.commit()"].join('\n')], { cwd: backend, env, encoding: 'utf8' });
if (init.status !== 0) throw Error(`database initialization failed: ${init.stderr}`);
const server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], { cwd: backend, env, stdio: ['ignore', log, log] });
const summary = { spec: 245, head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(), steps: [], screenshots: [], inspection_writes: [], errors: [] };
let browser, failure;
try {
  for (let i = 0; ; i++) { if (server.exitCode !== null) throw Error('backend exited'); try { if ((await fetch(base + '/health')).ok) break; } catch {} if (i > 120) throw Error('backend timeout'); await delay(500); }
  const post = async (path, body) => { const r = await fetch(base + path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }); if (!r.ok) throw Error(`${path}: ${r.status} ${await r.text()}`); return r.json(); };
  const ws = await post('/workspaces', { name: 'Freshness Proof', slug: 'freshness-proof' });
  const old = await post(`/workspaces/${ws.id}/parameters`, { name: 'Original length', value: '20', unit: 'm', value_status: 'accepted', status: 'accepted' });
  const dependent = await post('/memory/proposals', { workspace_id: ws.id, record_kind: 'assumption', statement: 'Dependent proof', source_ref: `parameter:${old.id}`, source_ai_job_id: 'sensitivity-walk-job' });
  const replacement = await post('/memory/proposals', { workspace_id: ws.id, record_kind: 'parameter', name: 'Replacement length', value: '22', unit: 'm', supersedes_parameter_id: old.id, source_ai_job_id: 'sensitivity-walk-job' });
  browser = await chromium.launch({ headless: true }); const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } }); const page = await context.newPage(); page.setDefaultTimeout(30000);
  page.on('pageerror', e => summary.errors.push(String(e)));
  // The Jarvis surface brief is a POST that only computes a preview and stores nothing (same allowance as design_discovery_walk).
  const PREVIEW_POSTS = [/^POST \/workspaces\/[^/]+\/actions\/brief$/];
  let inspecting = false; page.on('request', r => { if (r.method() === 'GET' && /\/flowsheet\/invalidations\//.test(new URL(r.url()).pathname)) inspecting = true; const call = `${r.method()} ${new URL(r.url()).pathname}`; if (inspecting && !['GET', 'HEAD', 'OPTIONS'].includes(r.method()) && !PREVIEW_POSTS.some(p => p.test(call))) summary.inspection_writes.push(call); });
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  const picker = page.getByLabel('Project', { exact: true }); if (await picker.count()) await picker.selectOption({ label: 'Freshness Proof' });
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('link', { name: 'Memory', exact: true }).click();
  await page.getByRole('navigation', { name: 'Project knowledge destinations' }).getByRole('link', { name: 'Review', exact: true }).click();
  await page.getByRole('heading', { name: 'Review', exact: true }).waitFor();
  await page.getByTitle('Show navigator').click();
  await page.getByRole('button', { name: /Replacement length/ }).click();
  const acceptance = page.waitForResponse(r => r.url().includes(`/memory/parameter/${replacement.id}/promote-replacement`) && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Accept', exact: true }).click();
  const result = await (await acceptance).json();
  const detail = page.getByRole('region', { name: 'Freshness invalidation detail' });
  await detail.getByText('Upstream parameter was replaced').waitFor();
  await detail.getByText('assumption', { exact: true }).waitFor();
  await detail.getByText(dependent.id, { exact: false }).waitFor();
  await detail.getByText(`parameter:${old.id} → assumption:${dependent.id}`, { exact: false }).waitFor();
  await detail.getByText(String(result.invalidation.affected_count), { exact: true }).waitFor();
  if (result.invalidation.affected_count < 1) throw Error('No affected records');
  await page.screenshot({ path: join(evidence, 'freshness-detail.png') }); summary.screenshots.push('freshness-detail.png'); summary.steps.push('Accepted replacement and inspected freshness detail');
  inspecting = false;
  if (summary.inspection_writes.length || summary.errors.length) throw Error('Unexpected writes or page errors');
} catch (error) { failure = error; summary.failure = String(error.stack ?? error); } finally {
  summary.result = failure ? 'fail' : 'pass'; await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n'); await browser?.close(); server.kill('SIGTERM'); await delay(500); await rm(data, { recursive: true, force: true });
}
if (failure) throw failure;
console.log(`freshness invalidation walk passed: ${evidence}`);
