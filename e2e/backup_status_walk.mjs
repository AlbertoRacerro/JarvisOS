#!/usr/bin/env node
// Spec 379: clean-profile walk of the Settings > System backup lines for each backup state (fake status file).
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync, closeSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

if (process.argv.includes('--help')) { console.log('node e2e/backup_status_walk.mjs <evidence-dir>'); process.exit(0); }
if (!process.argv[2]) throw Error('evidence directory required');
const root = resolve(new URL('../', import.meta.url).pathname), backend = join(root, 'backend');
const evidence = resolve(process.argv[2]); await mkdir(evidence, { recursive: true });
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8379), base = `http://127.0.0.1:${port}`;
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, '.venv/bin/python');
const summary = { spec: 379, head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(), data_root: 'fresh temporary data root', checks: [], screenshots: [], requests: [], page_errors: [] };
const assert = (condition, message) => { if (!condition) throw Error(message); };
let data, server, browser, failure, log;
try {
  assert(Number.isInteger(port) && port > 0 && port <= 65535 && port !== 8000, 'invalid proof port');
  await new Promise((ok, fail) => createServer().once('error', error => fail(Error(`cannot bind proof port ${port}: ${error.message}`))).once('listening', function () { this.close(ok); }).listen(port, '127.0.0.1'));
  data = await mkdtemp(join(tmpdir(), 'jarvisos-379-')); await mkdir(join(data, 'host'));
  const statusFile = join(data, 'host', 'backup-status.json');
  // Do not inherit provider, pack, runtime, vault or data-root configuration from the host.
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(JARVIS|OPENAI|ANTHROPIC|OLLAMA|SCW_|SCALEWAY|RESTIC)/i.test(key)));
  Object.assign(env, { PYTHONPATH: backend, JARVISOS_DATA_ROOT: data, JARVISOS_BACKUP_STATUS_FILE: statusFile, XDG_STATE_HOME: join(data, 'host'), LOCALAPPDATA: join(data, 'host') });
  const init = spawnSync(python, ['-c', 'from app.core.database import initialize_database; from app.modules.workspaces.service import create_workspace, default_workspace_payload; initialize_database(); create_workspace(default_workspace_payload())'], { cwd: backend, env, encoding: 'utf8' });
  assert(init.status === 0, `isolated database initialization failed: ${init.error ?? init.stderr}`);
  log = openSync(join(evidence, 'backend.log'), 'w');
  server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], { cwd: backend, env, stdio: ['ignore', log, log] });
  for (let i = 0; ; i++) { assert(server.exitCode === null, 'backend exited; see backend.log'); try { if ((await fetch(base + '/health')).ok) break; } catch {} assert(i < 120, 'backend did not become healthy'); await delay(500); }
  const playwright = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
  browser = await playwright.chromium.launch({ headless: true }); summary.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, locale: 'it-IT', timezoneId: 'UTC' });
  const page = await context.newPage(); page.setDefaultTimeout(15000);
  page.on('pageerror', error => summary.page_errors.push(String(error)));
  page.on('request', request => summary.requests.push(`${request.method()} ${new URL(request.url()).pathname}`));
  await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
  const shot = async name => { await page.screenshot({ path: join(evidence, name), fullPage: true }); summary.screenshots.push(name); };
  const fact = term => page.locator('.settings-facts > div', { has: page.getByText(term, { exact: true }) }).locator('dd');
  const now = new Date(), hoursAgo = h => new Date(now.getTime() - h * 3600e3).toISOString().replace(/\.\d+Z$/, 'Z');
  const states = [
    ['never', null, /^mai$/, /^mai$/],
    ['ok', { last_success_at: hoursAgo(2), last_restore_test_at: hoursAgo(200), last_restore_test_ok: true, repositories: { 0: { snapshot_id: 'walk-snapshot-id' } } }, /^\d{2}\/\d{2}\/\d{2},? \d{2}:\d{2}$/, / · riuscito$/],
    ['failed', { last_success_at: hoursAgo(30), last_failure_at: hoursAgo(1), last_failure_reason: 'backup_failed', repositories: {} }, /^non riuscito$/, /^mai$/],
    ['stale', { last_success_at: hoursAgo(72), last_restore_test_at: hoursAgo(100), last_restore_test_ok: false, repositories: {} }, /^vecchio \(/, / · non riuscito$/],
  ];
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  const create = page.getByRole('button', { name: 'Create workspace', exact: true });
  if (await create.isVisible()) { await page.getByLabel('Workspace name').fill('Backup Proof'); await create.click(); }
  for (const [state, content, backupLine, restoreLine] of states) {
    if (content) await writeFile(statusFile, JSON.stringify(content)); else await rm(statusFile, { force: true });
    if (state === 'never') await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('link', { name: 'Settings', exact: true }).click();
    else await page.reload({ waitUntil: 'networkidle' });
    const heading = page.getByRole('heading', { name: 'System', exact: true });
    if (!await heading.isVisible()) await page.getByRole('tab', { name: 'System', exact: true }).or(page.getByRole('button', { name: 'System', exact: true })).first().click();
    await heading.waitFor();
    await page.getByText('Ultimo backup', { exact: true }).scrollIntoViewIfNeeded();
    await fact('Ultimo backup').filter({ hasNotText: 'checking' }).waitFor();
    const shown = (await fact('Ultimo backup').innerText()).trim(), restore = (await fact('Ultimo test di ripristino').innerText()).trim();
    assert(backupLine.test(shown), `${state}: Ultimo backup shows ${JSON.stringify(shown)}`);
    assert(restoreLine.test(restore), `${state}: Ultimo test di ripristino shows ${JSON.stringify(restore)}`);
    const card = await page.locator('.settings-card', { has: page.getByRole('heading', { name: 'System', exact: true }) }).innerText();
    assert(!card.includes('walk-snapshot-id') && !card.includes('backup_failed') && !card.includes(data), `${state}: card leaks backup detail`);
    await shot(`backup-${state}.png`); summary.checks.push(`${state}: "${shown}" / "${restore}"`);
  }
  // The same tab layout used to hide the search-guard panel; it belongs to the AI tab.
  await page.getByRole('tab', { name: 'AI', exact: true }).or(page.getByRole('button', { name: 'AI', exact: true })).first().click();
  await page.getByRole('heading', { name: 'Filtro ricerche', exact: true }).waitFor();
  await page.getByRole('heading', { name: 'Filtro ricerche', exact: true }).scrollIntoViewIfNeeded();
  await shot('ai-search-guard.png'); summary.checks.push('AI tab shows the search-guard panel');
  assert(summary.page_errors.length === 0, `page errors: ${summary.page_errors.join('; ')}`);
} catch (error) { failure = error; summary.failure = String(error.stack ?? error); await browser?.contexts()[0]?.pages()[0]?.screenshot({ path: join(evidence, 'failure.png') }).catch(() => {}); }
finally {
  summary.result = failure ? 'fail' : 'pass'; await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n');
  await browser?.close().catch(() => {}); if (server) { server.kill('SIGTERM'); await delay(500); } if (log !== undefined) closeSync(log); if (data) await rm(data, { recursive: true, force: true });
}
if (failure) { console.error(failure); process.exit(1); }
console.log(`backup status walk: pass (${evidence})`);
