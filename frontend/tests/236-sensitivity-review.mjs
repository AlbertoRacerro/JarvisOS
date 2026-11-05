import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const routes = await readFile(new URL('../src/app/routes.ts', import.meta.url), 'utf8');
const header = await readFile(new URL('../src/components/fusion/FinalWorkspaceHeader.tsx', import.meta.url), 'utf8');
assert.ok(routes.includes('{ id: "review", path: "/review", title: "Review", primaryNav: "memory", stageKind: "review" }'));
assert.ok(routes.includes('{ label: "Review", href: "/review" }'));
assert.ok(header.includes('["Review", "/review", "review"]'));
const stage = await readFile(new URL('../src/stages/ReviewStage.tsx', import.meta.url), 'utf8');
const api = await readFile(new URL('../src/api/memory.ts', import.meta.url), 'utf8');
for (const pattern of ['workspace_id: workspaceId', 'subject_ref: `${kind}:${id}`', 'URLSearchParams', '/ai/sensitivity/labels/current?', 'if (!response.ok)', 'throw new MemoryRequestError']) assert.ok(api.includes(pattern), pattern);
for (const pattern of ['sensitivityIdentity(workspaceId, statusFilter, selected.record_kind, selected.id)', 'sensitivity?.key === sensitivityKey', 'sensitivityGeneration.current += 1', 'acceptsSensitivityRequest(currentSensitivity.current, request)', 'No sensitivity label recorded', 'Loading sensitivity', '"Current" : "Stale"', 'stale_reason', 'classification_source', 'policy_version', 'created_at', '"Retry"', '"Refresh sensitivity"', 'state: "error"']) assert.ok(stage.includes(pattern), pattern);
console.log('236 sensitivity review checks passed');

// Exercise the same identity and generation guard used by Review, including retry and late responses.
const ts = await import('typescript');
const helper = await readFile(new URL('../src/components/review/sensitivityRequest.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(helper, { compilerOptions: { module: ts.ModuleKind.ES2020 } }).outputText;
const { sensitivityIdentity, acceptsSensitivityRequest } = await import(`data:text/javascript,${encodeURIComponent(js)}`);
const a = sensitivityIdentity('workspace A', 'all', 'assumption', 'a:1');
const b = sensitivityIdentity('workspace B', 'all', 'assumption', 'a:1');
const c = sensitivityIdentity('workspace A', 'all', 'decision', 'b');
const d = sensitivityIdentity('workspace A', 'proposed', 'assumption', 'a:1');
assert.equal(new Set([a, b, c, d]).size, 4);
const first = { generation: 1, key: a };
for (const next of [{ generation: 2, key: a }, { generation: 2, key: b }, { generation: 2, key: c }, { generation: 2, key: d }]) {
  assert.equal(acceptsSensitivityRequest(next, first), false);
  assert.equal(acceptsSensitivityRequest(next, next), true);
}
assert.equal(acceptsSensitivityRequest(null, first), false);
