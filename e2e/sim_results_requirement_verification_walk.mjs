#!/usr/bin/env node
// Spec 253: clean-profile walk for simulation result ingestion, red-dot exceptions,
// single batch notification, resolution (correct + confirm), requirement verdicts and unvalidated markers.
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { openSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

if (process.argv.includes('--help')) {
  console.log('node e2e/sim_results_requirement_verification_walk.mjs <evidence-dir>');
  process.exit(0);
}
if (!process.argv[2]) throw Error('evidence directory required');

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? 'playwright');
const root = resolve(new URL('../', import.meta.url).pathname), backend = join(root, 'backend');
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, '.venv/bin/python');
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8793), base = `http://127.0.0.1:${port}`;
if (!Number.isInteger(port) || port < 1 || port > 65535 || port === 8000) throw Error('Invalid proof port');
await new Promise((ok, fail) => {
  const probe = createServer()
    .once('error', error => fail(Error(`Cannot bind proof port ${port}: ${error.message}`)))
    .once('listening', () => probe.close(ok))
    .listen(port, '127.0.0.1');
});

const evidence = resolve(process.argv[2]);
await mkdir(evidence, { recursive: true });
const data = await mkdtemp(join(tmpdir(), 'jarvisos-253-'));
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
from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.modules.project_knowledge.models import (
    ApprovalRequest,
    DraftCreate,
    ProjectKnowledgeOperation,
    ValidationRequest,
)
from app.modules.project_knowledge.service import (
    approve_draft,
    create_draft,
    evaluate_requirement,
    preview_impact,
    revalidation_status,
)
from app.modules.runner.models import ModelImplementationCreate, RunnerJobCreate
from app.modules.runner.service import (
    create_model_implementation,
    create_runner_job,
    run_runner_job,
)

initialize_storage(seed_default=True)
now = "2026-11-06T00:00:00+00:00"
with open_sqlite_connection() as conn:
    conn.execute(
        \"\"\"
        INSERT INTO parameters (
            id, workspace_id, name, value, unit, value_status, value_min, value_max,
            status, origin, created_at, updated_at, lifecycle_state
        ) VALUES
            ('param-pressure', 'main', 'coolant_pressure', '12.0', 'MPa', 'accepted', 5.0, 20.0, 'accepted', 'human', ?, ?, 'active'),
            ('param-heat', 'main', 'heat_flux', '8.0', 'MW/m^2', 'validated', 1.0, 10.0, 'accepted', 'human', ?, ?, 'active'),
            ('param-flow', 'main', 'mass_flow', '45.0', 'kg/s', 'validated', 10.0, 100.0, 'accepted', 'human', ?, ?, 'active')
        \"\"\",
        (now, now, now, now, now, now),
    )
    conn.execute(
        \"\"\"
        INSERT INTO requirements (
            id, workspace_id, statement, status, schema_version, created_at, updated_at,
            basis_kind, reconciliation_gate, criterion_output_name, criterion_operator,
            criterion_expected_value, criterion_expected_unit, criterion_rule_version
        ) VALUES
            ('req-flow', 'main', 'Mass flow >= 40 kg/s', 'active', 1, ?, ?, 'acceptance_criterion', 'required', 'mass_flow', '>=', '40.0', 'kg/s', 'scalar-v1'),
            ('req-heat', 'main', 'Heat flux <= 10 MW/m^2', 'active', 1, ?, ?, 'acceptance_criterion', 'required', 'heat_flux', '<=', '10.0', 'MW/m^2', 'scalar-v1'),
            ('req-press', 'main', 'Pressure == 12 MPa under off-normal', 'active', 1, ?, ?, 'acceptance_criterion', 'required', 'coolant_pressure', '==', '12.0', 'MPa', 'scalar-v1')
        \"\"\",
        (now, now, now, now, now, now),
    )
    for idx, req_id in enumerate(('req-flow', 'req-heat', 'req-press'), start=1):
        conn.execute(
            \"\"\"
            INSERT INTO requirement_applicability (
                id, workspace_id, requirement_id, target_kind, target_id, effect,
                lifecycle_state, created_at, updated_at
            ) VALUES (?, 'main', ?, 'workspace', 'main', 'include', 'active', ?, ?)
            \"\"\",
            (f'app-{idx}', req_id, now, now),
        )
    conn.execute(
        \"\"\"
        INSERT INTO model_specs (
            id, workspace_id, title, engineering_question, status, maturity_status,
            schema_version, created_at, updated_at
        ) VALUES ('spec-253', 'main', 'Primary Loop Thermal Model', 'Verify loop thermal margin.', 'active', 'draft', 1, ?, ?)
        \"\"\",
        (now, now),
    )
    conn.commit()

draft = create_draft(
    DraftCreate(
        workspace_id='main',
        operations=[
            ProjectKnowledgeOperation(
                owner_kind='requirement',
                operation_kind='create',
                fields={
                    'statement': 'Loop verification baseline',
                    'status': 'active',
                    'basis_kind': 'requirement',
                    'reconciliation_gate': 'advisory',
                },
            )
        ],
    )
)
preview = preview_impact('main', draft.id)
approval = approve_draft(
    ApprovalRequest(
        workspace_id='main',
        approval_request_key='walk-253-rev',
        draft_id=draft.id,
        expected_draft_revision_token=draft.revision_token,
        expected_preview_digest=preview.digest,
    )
)
rev_id = approval.working_revision_id
basis_digest = preview.digest

impl = create_model_implementation(
    'main',
    ModelImplementationCreate(
        model_spec_id='spec-253',
        version_label='calc-v0-253-walk',
        implementation_kind='calc_v0',
        script_text=\"\"\"import json
with open('result.json', 'w', encoding='utf-8') as f:
    json.dump({
        'outputs': {
            'mass_flow': {'value': 45.0, 'unit': 'kg/s', 'quantity': 'mass_flow', 'conditions': {'regime': 'nominal'}},
            'heat_flux': {'value': 25.0, 'unit': 'MW/m^2', 'quantity': 'heat_flux', 'conditions': {'regime': 'nominal'}},
            'coolant_pressure': {'value': 15.0, 'unit': 'MPa', 'quantity': 'coolant_pressure', 'conditions': {'regime': 'nominal'}}
        }
    }, f)
\"\"\",
    ),
)
job = create_runner_job(
    'main',
    RunnerJobCreate(
        model_version_id=impl.id,
        input_set={'inlet_temp': {'value': 300, 'unit': 'K'}},
    ),
)
run_id = job.simulation_run.id
with open_sqlite_connection() as conn:
    conn.execute('UPDATE simulation_runs SET project_knowledge_revision_id = ? WHERE id = ?', (rev_id, run_id))
    conn.commit()
run_runner_job(job.runner_job.id)

reval = revalidation_status('main', rev_id)
app_digest = reval.applicability_set_digest

# 1. pass verdict (mass_flow)
evaluate_requirement(
    ValidationRequest(
        workspace_id='main',
        working_revision_id=rev_id,
        requirement_id='req-flow',
        expected_requirement_updated_at=now,
        validated_basis_digest=basis_digest,
        applicability_set_digest=app_digest,
        source_run_id=run_id,
        required_conditions={'regime': 'nominal'},
    )
)
# 2. fail verdict with unvalidated marker (heat_flux)
evaluate_requirement(
    ValidationRequest(
        workspace_id='main',
        working_revision_id=rev_id,
        requirement_id='req-heat',
        expected_requirement_updated_at=now,
        validated_basis_digest=basis_digest,
        applicability_set_digest=app_digest,
        source_run_id=run_id,
        required_conditions={'regime': 'nominal'},
    )
)
# 3. not_evaluable verdict outside stated conditions with unvalidated marker (coolant_pressure)
evaluate_requirement(
    ValidationRequest(
        workspace_id='main',
        working_revision_id=rev_id,
        requirement_id='req-press',
        expected_requirement_updated_at=now,
        validated_basis_digest=basis_digest,
        applicability_set_digest=app_digest,
        source_run_id=run_id,
        required_conditions={'regime': 'off-normal'},
    )
)
`;

const init = spawnSync(python, ['-c', seedScript], { cwd: backend, env, encoding: 'utf8' });
if (init.status !== 0) throw Error(`database initialization failed: ${init.stderr}`);

const server = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], {
  cwd: backend,
  env,
  stdio: ['ignore', log, log]
});
const summary = {
  spec: 253,
  head: spawnSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).stdout.trim(),
  steps: [],
  screenshots: [],
  errors: []
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
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('link', { name: 'Memory', exact: true }).click();
  await page.getByRole('navigation', { name: 'Project knowledge destinations' }).getByRole('link', { name: 'Project Basis', exact: true }).click();

  const pkPanel = page.getByRole('region', { name: 'Project Knowledge' });
  await pkPanel.getByText('Jarvis batch notifications').waitFor();
  await pkPanel.getByText('Simulation result batches').waitFor();

  // Verify single batch notification with 2 flagged values
  const noticeSection = pkPanel.getByRole('region', { name: 'Batch notifications' });
  await noticeSection.getByText(/2 flagged value\(s\) requiring review/).waitFor();

  // Verify passing value and 2 red-dot values
  const batchesSection = pkPanel.getByRole('region', { name: 'Simulation result batches' });
  await batchesSection.getByText(/mass_flow.*45\.0 kg\/s.*\(passed\)/).waitFor();
  const redDots = await batchesSection.getByLabel('Exception red dot').count();
  if (redDots !== 2) throw Error(`Expected 2 red dots, found ${redDots}`);

  // Verify pass, fail, not_evaluable requirement verdicts and unvalidated marker
  const verdictsSection = pkPanel.getByRole('region', { name: 'Requirement verification verdicts' });
  await verdictsSection.getByText(/req-flow · Verdict: pass/).waitFor();
  await verdictsSection.getByText(/req-heat · Verdict: fail/).waitFor();
  await verdictsSection.getByText(/req-press · Verdict: not_evaluable \(outside_stated_conditions\)/).waitFor();
  await verdictsSection.getByText(/Unvalidated source value/).first().waitFor();

  await page.screenshot({ path: join(evidence, '01-ingested-batch-and-verdicts.png'), fullPage: true });
  summary.screenshots.push('01-ingested-batch-and-verdicts.png');
  summary.steps.push('Observed passing value, 2 red-dot exceptions, single batch notification, and pass/fail/not_evaluable requirement verdicts with unvalidated markers');

  // Open first flagged value (heat_flux) from the batch notification and correct it
  await noticeSection.getByRole('button', { name: /Open heat_flux/ }).click();
  const resolveHeat = batchesSection.getByRole('region', { name: 'Resolve exception heat_flux' });
  await resolveHeat.getByLabel('Resolution action').selectOption('correct');
  await resolveHeat.getByLabel('Corrected value').fill('7.5');
  await resolveHeat.getByLabel('Corrected unit').fill('MW/m^2');
  await resolveHeat.getByLabel('Resolution reason').fill('Sensor calibration factor 0.3 applied');
  await resolveHeat.getByRole('button', { name: 'Submit correction' }).click();

  await batchesSection.getByText(/heat_flux.*7\.5 MW\/m\^2.*\(corrected\)/).waitFor();
  await page.screenshot({ path: join(evidence, '02-corrected-heat-flux.png'), fullPage: true });
  summary.screenshots.push('02-corrected-heat-flux.png');
  summary.steps.push('Opened heat_flux from batch notification and corrected it to 7.5 MW/m^2');

  // Open second flagged value (coolant_pressure) from the batch notification and confirm it
  await noticeSection.getByRole('button', { name: /Open coolant_pressure/ }).click();
  const resolvePress = batchesSection.getByRole('region', { name: 'Resolve exception coolant_pressure' });
  await resolvePress.getByLabel('Resolution action').selectOption('confirm');
  await resolvePress.getByLabel('Resolution reason').fill('Accepted transient peak for sensitivity run');
  await resolvePress.getByRole('button', { name: 'Confirm exception' }).click();

  await batchesSection.getByText(/coolant_pressure.*15\.0 MPa.*\(confirmed\)/).waitFor();
  await noticeSection.getByText('State · read').waitFor();
  await page.screenshot({ path: join(evidence, '03-confirmed-coolant-pressure.png'), fullPage: true });
  summary.screenshots.push('03-confirmed-coolant-pressure.png');
  summary.steps.push('Opened coolant_pressure from batch notification, confirmed exception, and observed notification transition to read');

  if (summary.errors.length) throw Error(`Unexpected page errors: ${summary.errors.join('; ')}`);
} catch (error) {
  failure = error;
  summary.failure = String(error.stack ?? error);
} finally {
  summary.result = failure ? 'fail' : 'pass';
  await writeFile(join(evidence, 'evidence.json'), JSON.stringify(summary, null, 2) + '\n');
  await browser?.close();
  server.kill('SIGTERM');
  await delay(500);
  await rm(data, { recursive: true, force: true });
}
if (failure) throw failure;
console.log(`sim results requirement verification walk passed: ${evidence}`);
