#!/usr/bin/env node
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
if (process.argv.includes('--help')) { console.log('node e2e/local_model_switch.mjs <evidence-dir>'); process.exit(0); }
if (!process.argv[2]) throw Error('evidence directory required');
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
const root = resolve(new URL('../', import.meta.url).pathname), backend = join(root, 'backend');
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, '.venv/bin/python');
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8793), base = `http://127.0.0.1:${port}`;
if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) throw Error('Invalid proof port');
await new Promise((ok, fail) => { const probe = createServer().once('error', fail).once('listening', () => probe.close(ok)).listen(port, '127.0.0.1'); });
const evidence = resolve(process.argv[2]); await mkdir(evidence, { recursive: true });
const data = await mkdtemp(join(tmpdir(), 'jarvisos-250-')); await mkdir(join(data, 'host'));
const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: data, XDG_STATE_HOME: join(data, 'host'), LOCALAPPDATA: join(data, 'host') };
for (const key of Object.keys(env)) if (key.startsWith('JARVISOS_LLAMACPP_') || key === 'JARVISOS_MANAGE_LLAMACPP') delete env[key];
const init = spawnSync(python, ['-c', 'from app.core.database import initialize_database; from app.modules.workspaces.service import create_workspace, default_workspace_payload; initialize_database(); create_workspace(default_workspace_payload())'], { cwd: backend, env, encoding: 'utf8' });
if (init.status !== 0) throw Error(`database initialization failed: ${init.stderr}`);
const log = openSync(join(evidence, 'backend.log'), 'w');
const server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], { cwd: backend, env, stdio: ['ignore', log, log] });
const summary = { spec: 250, head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(), steps: [], screenshots: [], posts: [], errors: [] };
let browser, failure;
try {
  for (let i = 0; ; i++) { if (server.exitCode !== null) throw Error('backend exited'); try { if ((await fetch(base + '/health')).ok) break; } catch {} if (i > 120) throw Error('backend timeout'); await delay(500); }
  browser = await chromium.launch({ headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1600, height: 1000 } })).newPage(); page.setDefaultTimeout(30000);
  page.on('pageerror', e => summary.errors.push(String(e)));
  let active = 'default';
  const running = { configured: true, runtime_reachable: true, model_loaded: true, reason_code: null, message: 'Model running', model_file: '/models/default.gguf', ctx_size: 8192, n_gpu_layers: 10, vram_bytes: null, ram_bytes: null, spawned_by_jarvis: true, crash_count: 0, last_crash_at: null };
  const entries = [
    { id: 'default', label: 'Default', runtime: 'llama_cpp', qualification: 'default', model_file: 'default.gguf', installed: true, digest_state: 'verified' },
    { id: 'candidate', label: 'Candidate', runtime: 'llama_cpp', qualification: 'qualified', model_file: 'candidate.gguf', installed: true, digest_state: 'not_verified' },
    { id: 'strata', label: 'Strata', runtime: 'strata', qualification: 'under_qualification', model_file: 'strata.gguf', installed: false, digest_state: 'missing' },
  ];
  await page.route(/\/local-ai\/models(\/.*)?$/, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() === 'POST') {
      summary.posts.push(path);
      active = path.includes('restore-default') ? 'default' : 'candidate';
      await route.fulfill({ json: { ...running, model_id: active, model_file: `/models/${active}.gguf` } });
    } else await route.fulfill({ json: { registry_present: true, active_id: active, models: entries } });
  });
  await page.route(/\/local-ai\/runtime\/llama-cpp(\/.*)?$/, async route => {
    if (route.request().method() === 'POST') summary.posts.push(new URL(route.request().url()).pathname);
    await route.fulfill({ json: { ...running, model_id: active, model_file: `/models/${active}.gguf` } });
  });
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  const create = page.getByRole('button', { name: 'Create workspace', exact: true });
  if (await create.isVisible()) { await page.getByLabel('Workspace name').fill('Runtime Proof'); await create.click(); }
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('link', { name: 'Coding', exact: true }).click();
  await page.getByRole('navigation', { name: 'Coding destinations' }).getByRole('link', { name: 'Runtime', exact: true }).click();
  const panel = page.getByRole('region', { name: 'Local model' });
  await panel.getByText('Registered models').waitFor();
  if (summary.posts.length) throw Error('POST on load');
  if (await panel.getByRole('button', { name: 'Activate' }).count() !== 1) throw Error('Wrong activate availability');
  await page.screenshot({ path: join(evidence, 'registry.png'), fullPage: true }); summary.screenshots.push('registry.png'); summary.steps.push('registry and no POST on load');
  page.once('dialog', dialog => dialog.accept());
  await panel.getByRole('button', { name: 'Activate' }).click();
  await panel.getByRole('button', { name: 'Restore default' }).waitFor();
  await page.screenshot({ path: join(evidence, 'candidate.png'), fullPage: true }); summary.screenshots.push('candidate.png'); summary.steps.push('confirmed candidate activation');
  page.once('dialog', dialog => dialog.accept());
  await panel.getByRole('button', { name: 'Restore default' }).click();
  await panel.getByText('Model file · default.gguf').waitFor();
  if (summary.posts.length !== 2) throw Error('Unexpected POST count');
  await page.screenshot({ path: join(evidence, 'restored.png'), fullPage: true }); summary.screenshots.push('restored.png'); summary.steps.push('confirmed default restoration');
  if (summary.errors.length) throw Error('Page errors occurred');
} catch (error) { failure = error; summary.failure = String(error.stack ?? error); } finally {
  summary.result = failure ? 'fail' : 'pass'; await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n'); await browser?.close(); server.kill('SIGTERM'); await delay(500); await rm(data, { recursive: true, force: true });
}
if (failure) throw failure;
console.log(`local model walk passed: ${evidence}`);
