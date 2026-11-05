import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
const stage = await readFile(new URL('../src/stages/ReviewStage.tsx', import.meta.url), 'utf8');
const api = await readFile(new URL('../src/api/memory.ts', import.meta.url), 'utf8');
for (const pattern of ['encodeURIComponent(workspaceId)', 'encodeURIComponent(id)', '/flowsheet/invalidations/', 'requestJson<FreshnessInvalidationDetail>']) assert.ok(api.includes(pattern), pattern);
for (const pattern of ['replacement.invalidation.id', 'workspaceRef.current === targetWorkspaceId', 'acceptsSensitivityRequest(currentFreshness.current, request)', 'freshnessGeneration.current += 1', '[workspaceId]', 'state: "loading"', 'state: "error"', 'loadFreshness(target.workspaceId, target.id)', 'affected_count === 0', 'No record became stale', 'marks.slice(0, 20)', 'Show all', 'setShowAllFreshness(true)', 'Upstream parameter was replaced', ': mark.reason_code', 'mark.path.join(" → ")']) assert.ok(stage.includes(pattern), pattern);
const helper = await readFile(new URL('../src/components/review/sensitivityRequest.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(helper, { compilerOptions: { module: ts.ModuleKind.ES2020 } }).outputText;
const { acceptsSensitivityRequest } = await import(`data:text/javascript,${encodeURIComponent(js)}`);
const first = { generation: 1, key: JSON.stringify(['workspace A', 'invalidation A']) };
for (const next of [{ generation: 2, key: first.key }, { generation: 2, key: JSON.stringify(['workspace B', 'invalidation A']) }, { generation: 2, key: JSON.stringify(['workspace A', 'invalidation B']) }]) {
  assert.equal(acceptsSensitivityRequest(next, first), false);
  assert.equal(acceptsSensitivityRequest(next, next), true);
}
assert.equal(acceptsSensitivityRequest(null, first), false);
console.log('245 freshness invalidation detail checks passed');
