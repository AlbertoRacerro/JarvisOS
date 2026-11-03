import assert from 'node:assert/strict';
import { PLAN_SCHEMA, validatePlan } from './plan-lib.mjs';
import { isMutatingSameOriginRequest } from './request-policy.mjs';

for (const method of ['GET', 'HEAD', 'OPTIONS']) {
  assert.equal(isMutatingSameOriginRequest(method, '/any/path'), false, `${method} must remain read-only`);
}
const readOnlyPostPaths = new Set(['/ai/context/packs/preview']);
assert.equal(isMutatingSameOriginRequest('POST', '/ai/context/packs/preview'), true, 'POST is mutating unless the trusted plan declares an exact exception');
assert.equal(isMutatingSameOriginRequest('POST', '/ai/context/packs/preview', readOnlyPostPaths), false, 'declared context preview POST is read-only');
assert.equal(isMutatingSameOriginRequest('post', '/ai/context/packs/preview', readOnlyPostPaths), false, 'method classification is case-normalized');
assert.equal(isMutatingSameOriginRequest('POST', '/ai/context/packs/preview/extra', readOnlyPostPaths), true, 'read-only POST classification is exact-path only');
assert.equal(isMutatingSameOriginRequest('POST', '/api/write', readOnlyPostPaths), true, 'unclassified POST remains mutating');
assert.equal(isMutatingSameOriginRequest('PUT', '/ai/context/packs/preview', readOnlyPostPaths), true, 'only POST may use the read-only endpoint classification');
assert.equal(isMutatingSameOriginRequest('PATCH', '/ai/context/packs/preview', readOnlyPostPaths), true, 'PATCH remains mutating');
assert.equal(isMutatingSameOriginRequest('DELETE', '/ai/context/packs/preview', readOnlyPostPaths), true, 'destructive methods remain mutating');
assert.equal(isMutatingSameOriginRequest(null, '/ai/context/packs/preview', readOnlyPostPaths), true, 'malformed methods fail closed');
assert.equal(isMutatingSameOriginRequest('POST', null, readOnlyPostPaths), true, 'malformed paths fail closed');
assert.equal(isMutatingSameOriginRequest('POST', '/ai/context/packs/preview', ['/ai/context/packs/preview']), true, 'non-Set policy input fails closed');
for (const encoded of ['/api/%77rite', '/ai/context/packs/%70review', '/ai/context/packs/preview%2Fextra', '/bad%ZZ']) {
  assert.equal(isMutatingSameOriginRequest('POST', encoded, new Set([encoded])), true, `encoded alias ${encoded} must fail closed even when declared read-only`);
}

const basePlan = {
  schema: PLAN_SCHEMA,
  id: 'request-policy-test',
  forbidMutatingRequests: true,
  steps: [{op:'navigate', name:'open', route:'/ok'}],
};
assert.doesNotThrow(() => validatePlan({...basePlan, readOnlySameOriginPostPaths:['/ai/context/packs/preview']}));
for (const invalid of [
  [],
  'not-an-array',
  ['/same', '/same'],
  ['relative/path'],
  ['/with?query=1'],
  ['/with#fragment'],
  ['/a//b'],
  ['/a/../b'],
  Array.from({length:21}, (_value, index) => `/safe/${index}`),
]) assert.throws(() => validatePlan({...basePlan, readOnlySameOriginPostPaths:invalid}));

console.log('browser-proof request policy PASS');
