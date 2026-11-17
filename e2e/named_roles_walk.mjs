#!/usr/bin/env node
// Spec 260: clean-profile visible-control walk; no conversation is submitted.
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync, closeSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

if (process.argv.includes('--help')) { console.log('node e2e/named_roles_walk.mjs <evidence-dir>'); process.exit(0); }
if (!process.argv[2]) throw Error('evidence directory required');
const root = resolve(new URL('../', import.meta.url).pathname), backend = join(root, 'backend');
const evidence = resolve(process.argv[2]); await mkdir(evidence, { recursive: true });
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8260), base = `http://127.0.0.1:${port}`;
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, '.venv/bin/python');
const summary = { spec: 260, head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(), data_root: 'fresh temporary data root', checks: [], screenshots: [], requests: [], page_errors: [] };
const assert = (condition, message) => { if (!condition) throw Error(message); };
let data, server, browser, failure, log;
try {
  assert(Number.isInteger(port) && port > 0 && port <= 65535 && port !== 8000, 'invalid proof port');
  await new Promise((ok, fail) => createServer().once('error', error => fail(Error(`cannot bind proof port ${port}: ${error.message}`))).once('listening', function () { this.close(ok); }).listen(port, '127.0.0.1'));
  data = await mkdtemp(join(tmpdir(), 'jarvisos-260-')); await mkdir(join(data, 'host'));
  // Do not inherit provider, pack, runtime or data-root configuration from the host.
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(JARVIS|OPENAI|ANTHROPIC|OLLAMA|SCW_|SCALEWAY)/i.test(key)));
  Object.assign(env, { PYTHONPATH: backend, JARVISOS_DATA_ROOT: data, XDG_STATE_HOME: join(data, 'host'), LOCALAPPDATA: join(data, 'host') });
  const init = spawnSync(python, ['-c', 'from app.core.database import initialize_database; from app.modules.workspaces.service import create_workspace, default_workspace_payload; initialize_database(); create_workspace(default_workspace_payload())'], { cwd: backend, env, encoding: 'utf8' });
  assert(init.status === 0, `isolated database initialization failed: ${init.error ?? init.stderr}`);
  log = openSync(join(evidence, 'backend.log'), 'w');
  server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], { cwd: backend, env, stdio: ['ignore', log, log] });
  for (let i = 0; ; i++) { assert(server.exitCode === null, 'backend exited; see backend.log'); try { if ((await fetch(base + '/health')).ok) break; } catch {} assert(i < 120, 'backend did not become healthy'); await delay(500); }
  const playwright = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
  browser = await playwright.chromium.launch({ headless: true }); summary.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await context.newPage(); page.setDefaultTimeout(15000);
  page.on('pageerror', error => summary.page_errors.push(String(error)));
  page.on('request', request => summary.requests.push(`${request.method()} ${new URL(request.url()).pathname}`));
  // Block every request outside this isolated server, even if the host has provider configuration.
  await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
  const shot = async name => { await page.screenshot({ path: join(evidence, name), fullPage: true }); summary.screenshots.push(name); };
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  const create = page.getByRole('button', { name: 'Create workspace', exact: true });
  if (await create.isVisible()) { await page.getByLabel('Workspace name').fill('Roles Proof'); await create.click(); }
  await page.getByRole('button', { name: /open jarvis/i }).click();
  await page.getByRole('dialog', { name: 'Jarvis' }).getByRole('button', { name: 'Expand' }).click();
  const jarvis = page.getByRole('complementary', { name: 'Jarvis' });
  await jarvis.getByRole('button', { name: /responder|test responder|jarvis agent|no responder/i }).click();
  await page.getByRole('menuitem', { name: /Jarvis agent.*unavailable/i }).click();
  const chooser = jarvis.getByRole('radiogroup', { name: 'Hermes persona' }); await chooser.waitFor();
  const purposes = { Isaac: 'Scientific reasoning colleague', Gregor: 'Scientific internet researcher', Q: 'Tool-using agent', Alfred: 'Mail, calendar, and documents assistant' };
  for (const [name, purpose] of Object.entries(purposes)) {
    const radioName = new RegExp(`^${name}\\b`), radio = chooser.getByRole('radio', { name: radioName });
    // The radio input is visually hidden; a person clicks its visible card.
    await chooser.locator('label', { has: page.getByRole('radio', { name: radioName }) }).click(); assert(await radio.isChecked(), `${name} not selected`);
    assert(await chooser.getByText(purpose, { exact: false }).isVisible(), `${name} purpose missing`);
    assert(await chooser.getByRole('alert').isVisible(), `${name} unavailable reason missing`);
    assert((await radio.evaluate(el => el.parentElement.innerText)).toLowerCase().includes('unavailable'), `${name} unavailable badge missing`);
    assert((await chooser.getByRole('alert').innerText()).trim().length > 0, `${name} reason empty`);
    if (name !== 'Isaac') assert(await chooser.getByRole('checkbox', { name: /Sheldon mode/ }).count() === 0, `Sheldon visible for ${name}`);
  }
  await chooser.locator('label', { has: page.getByRole('radio', { name: /^Isaac\b/ }) }).click();
  const sheldon = chooser.getByRole('checkbox', { name: /Sheldon mode/ }); await sheldon.check(); assert(await sheldon.isChecked(), 'Sheldon did not turn on'); await sheldon.uncheck(); assert(!await sheldon.isChecked(), 'Sheldon did not turn off');
  await jarvis.getByRole('textbox', { name: 'Message' }).fill('Availability proof');
  assert(await jarvis.locator('button[type=submit].jarvis-send').isDisabled(), 'unavailable role can send');
  await shot('roles-unavailable.png'); summary.checks.push('four roles, purposes, unavailable reasons and disabled send', 'Sheldon toggle only for Isaac');
  await jarvis.getByRole('button', { name: 'Close Jarvis' }).click();
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('link', { name: 'Settings', exact: true }).click();
  const aiTab = async () => { await page.getByRole('tab', { name: 'AI', exact: true }).or(page.getByRole('button', { name: 'AI', exact: true })).first().click(); await page.getByRole('heading', { name: 'Hermes role models' }).waitFor(); };
  await aiTab();
  for (const name of Object.keys(purposes)) assert(await page.getByRole('textbox', { name: `${name} model route` }).isVisible(), `${name} route missing`);
  const field = page.getByRole('textbox', { name: 'Isaac model route' });
  const row = page.getByText('Isaac model route', { exact: true }).locator('..');
  await field.fill('local:general'); await row.getByRole('button', { name: 'Save', exact: true }).click();
  await page.getByText('Role model saved. Canonical settings reloaded.').waitFor();
  await page.reload({ waitUntil: 'networkidle' }); if (!await page.getByRole('heading', { name: 'Hermes role models' }).isVisible()) await aiTab(); assert(await field.inputValue() === 'local:general', 'route did not persist');
  await shot('settings-role-routes.png'); summary.checks.push('four Settings route controls and persisted local route');
  assert(!summary.requests.some(line => /^(POST|PUT|PATCH) .*\/(threads|interactions)/.test(line)), 'conversation request sent');
  assert(summary.page_errors.length === 0, `page errors: ${summary.page_errors.join('; ')}`);
} catch (error) { failure = error; summary.failure = String(error.stack ?? error); await browser?.contexts()[0]?.pages()[0]?.screenshot({ path: join(evidence, 'failure.png') }).catch(() => {}); }
finally {
  summary.result = failure ? 'fail' : 'pass'; await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n');
  await browser?.close().catch(() => {}); if (server) { server.kill('SIGTERM'); await delay(500); } if (log !== undefined) closeSync(log); if (data) await rm(data, { recursive: true, force: true });
}
if (failure) { console.error(failure); process.exit(1); }
console.log(`named roles walk: pass (${evidence})`);
