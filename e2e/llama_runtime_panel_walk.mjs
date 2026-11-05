#!/usr/bin/env node
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
if (process.argv.includes('--help')) { console.log('node e2e/llama_runtime_panel_walk.mjs <evidence-dir>'); process.exit(0); }
if (!process.argv[2]) throw Error('evidence directory required');
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
const root = resolve(new URL('../', import.meta.url).pathname), backend = join(root, 'backend');
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, '.venv/bin/python');
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8793), base = `http://127.0.0.1:${port}`;
if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) throw Error('Invalid proof port');
await new Promise((ok, fail) => { const probe = createServer().once('error', fail).once('listening', () => probe.close(ok)).listen(port, '127.0.0.1'); });
const evidence = resolve(process.argv[2]); await mkdir(evidence, { recursive: true });
const data = await mkdtemp(join(tmpdir(), 'jarvisos-247-')); await mkdir(join(data, 'host'));
const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: data, XDG_STATE_HOME: join(data, 'host'), LOCALAPPDATA: join(data, 'host') };
for (const key of Object.keys(env)) if (key.startsWith('JARVISOS_LLAMACPP_') || key === 'JARVISOS_MANAGE_LLAMACPP') delete env[key];
const init = spawnSync(python, ['-c', 'from app.core.database import initialize_database; from app.modules.workspaces.service import create_workspace, default_workspace_payload; initialize_database(); create_workspace(default_workspace_payload())'], { cwd: backend, env, encoding: 'utf8' });
if (init.status !== 0) throw Error(`database initialization failed: ${init.stderr}`);
const log = openSync(join(evidence, 'backend.log'), 'w');
const server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], { cwd: backend, env, stdio: ['ignore', log, log] });
const summary = { spec: 247, head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(), steps: [], screenshots: [], posts: [], errors: [] };
let browser, failure;
try {
  for (let i = 0; ; i++) { if (server.exitCode !== null) throw Error('backend exited'); try { if ((await fetch(base + '/health')).ok) break; } catch {} if (i > 120) throw Error('backend timeout'); await delay(500); }
  browser = await chromium.launch({ headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1600, height: 1000 } })).newPage(); page.setDefaultTimeout(30000);
  page.on('pageerror', e => summary.errors.push(String(e)));
  let fixture = false, failAction = false;
  const running = { configured: true, runtime_reachable: true, model_loaded: true, reason_code: null, message: 'Model running', model_file: '/models/proof.gguf', ctx_size: 8192, n_gpu_layers: 10, vram_bytes: null, ram_bytes: null, spawned_by_jarvis: true, crash_count: 0, last_crash_at: null };
  const stopped = { ...running, runtime_reachable: false, model_loaded: false, spawned_by_jarvis: false, reason_code: 'LLAMACPP_RUNTIME_UNREACHABLE', message: 'Model stopped' };
  await page.route(/\/local-ai\/runtime\/llama-cpp(\/[a-z-]+)?(\?.*)?$/, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() === 'POST') { summary.posts.push(path); await route.fulfill(failAction ? { status: 500, json: { detail: 'Fixture action failure' } } : { json: stopped }); }
    else if (fixture) await route.fulfill({ json: running }); else await route.continue();
  });
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  const create = page.getByRole('button', { name: 'Create workspace', exact: true });
  if (await create.isVisible()) { await page.getByLabel('Workspace name').fill('Runtime Proof'); await create.click(); }
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('link', { name: 'Coding', exact: true }).click();
  await page.getByRole('navigation', { name: 'Coding destinations' }).getByRole('link', { name: 'Runtime', exact: true }).click();
  const panel = page.getByRole('region', { name: 'Local model' });
  await panel.getByText(/Not configured.*Configure a llama-server/).waitFor();
  if (!await panel.getByRole('button', { name: 'Start', exact: true }).isDisabled() || summary.posts.length) throw Error('Initial state or POST check failed');
  await page.screenshot({ path: join(evidence, 'not-configured.png'), fullPage: true }); summary.screenshots.push('not-configured.png'); summary.steps.push('not configured; no POST on load');
  fixture = true; await panel.getByRole('button', { name: 'Refresh', exact: true }).click(); await panel.getByText(/Model running/).waitFor();
  page.once('dialog', dialog => dialog.accept()); await panel.getByRole('button', { name: 'Stop', exact: true }).click(); await panel.getByText(/Model stopped/).waitFor();
  await page.screenshot({ path: join(evidence, 'stopped.png'), fullPage: true }); summary.screenshots.push('stopped.png'); summary.steps.push('running, confirmed stop, returned status');
  await panel.getByRole('button', { name: 'Refresh', exact: true }).click(); await panel.getByText(/Model running/).waitFor();
  failAction = true; page.once('dialog', dialog => dialog.accept()); await panel.getByRole('button', { name: 'Stop', exact: true }).click();
  const alert = panel.getByRole('alert'); await alert.waitFor();
  if (!/Fixture action failure.*Last known status/.test(await alert.textContent())) throw Error('Failure was not labelled');
  if (await panel.getByText(/Model stopped/).count()) throw Error('Failure presented as stopped');
  await page.screenshot({ path: join(evidence, 'failure.png'), fullPage: true }); summary.screenshots.push('failure.png'); summary.steps.push('failure retains labelled last known status');
  if (summary.errors.length) throw Error('Page errors occurred');
} catch (error) { failure = error; summary.failure = String(error.stack ?? error); } finally {
  summary.result = failure ? 'fail' : 'pass'; await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n'); await browser?.close(); server.kill('SIGTERM'); await delay(500); await rm(data, { recursive: true, force: true });
}
if (failure) throw failure;
console.log(`local model walk passed: ${evidence}`);
