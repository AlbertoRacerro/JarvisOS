import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { validatePlan } from './plan-lib.mjs';

const plan = JSON.parse(await readFile(new URL('./plans/114-literature.json', import.meta.url), 'utf8'));
validatePlan(plan);
assert.equal(plan.forbidMutatingRequests, undefined, 'Literature proof must not fail on unrelated initialization POSTs');

const baseline = plan.steps.find((step) => step.name === '114:capture-mutation-baseline');
const after = plan.steps.find((step) => step.name === '114:capture-mutation-after-browsing');
const equality = plan.steps.find((step) => step.name === '114:no-new-mutating-requests');
assert.deepEqual(baseline, {op:'capture-mutating-request-count', name:'114:capture-mutation-baseline', capture:'mutationBaseline'});
assert.deepEqual(after, {op:'capture-mutating-request-count', name:'114:capture-mutation-after-browsing', capture:'mutationAfterBrowsing'});
assert.deepEqual(equality.left, {capture:'mutationAfterBrowsing'});
assert.deepEqual(equality.right, {capture:'mutationBaseline'});
assert(plan.steps.indexOf(baseline) < plan.steps.findIndex((step) => step.name === '114:expand-a'));
assert(plan.steps.indexOf(after) > plan.steps.findIndex((step) => step.name === '114:open-source-a'));

const generic = {
  schema: 'jarvisos.browser-proof-plan.v1',
  id: 'generic-mutation-window',
  fixture: 'none',
  steps: [
    {op:'capture-mutating-request-count', name:'before', capture:'before'},
    {op:'capture-mutating-request-count', name:'after', capture:'after'},
    {op:'assert-value-equals', name:'no-delta', left:{capture:'after'}, right:{capture:'before'}},
  ],
};
validatePlan(generic);
assert.throws(() => validatePlan({...generic, steps:[{op:'capture-mutating-request-count', name:'bad', capture:''}]}));
assert.throws(() => validatePlan({...generic, steps:[{op:'capture-mutating-request-count', name:'bad', capture:'x', path:'/ai/context/packs/preview'}]}));

const runSource = await readFile(new URL('./run.mjs', import.meta.url), 'utf8');
assert(runSource.includes('step.op === "capture-mutating-request-count"'));
assert(runSource.includes('mutatingBrowserRequests.length'));
assert(!runSource.includes('planId === "114-literature"'));
assert(!runSource.includes('/ai/context/packs/preview'), 'generic executor must not special-case a product endpoint');
const tracingStop = runSource.indexOf('await context.tracing.stop');
const browserClose = runSource.indexOf('await browser.close()');
const finalMutationAssessment = runSource.lastIndexOf('const pass = mutatingBrowserRequests.length === 0');
assert(tracingStop >= 0 && browserClose >= 0 && finalMutationAssessment >= 0, 'executor must expose teardown and final mutation assessment');
assert(tracingStop < finalMutationAssessment, 'late requests emitted during trace teardown must be observed before PASS');
assert(browserClose < finalMutationAssessment, 'late requests emitted during browser close must be observed before PASS');
assert(runSource.includes('teardownFailure') && runSource.includes('verdict = "FAIL"'), 'teardown failures must fail closed');

console.log('browser-proof mutation-window primitive and teardown ordering PASS');
