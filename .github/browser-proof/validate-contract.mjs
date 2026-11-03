import { readdir } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { trustedFixturePaths } from './fixture-registry.mjs';
import { loadTrustedPlan } from './plan-lib.mjs';

const root = dirname(fileURLToPath(import.meta.url));
const planDir = join(root, 'plans');
const planFiles = (await readdir(planDir, { withFileTypes: true }))
  .filter((entry) => entry.isFile() && entry.name.endsWith('.json'))
  .map((entry) => entry.name)
  .sort();

if (planFiles.length === 0) throw new Error('no trusted proof plans found');
for (const file of planFiles) {
  const id = file.slice(0, -'.json'.length);
  const plan = await loadTrustedPlan(id, planDir);
  if (plan.id !== id) throw new Error(`trusted proof plan id does not match filename: ${file}`);
}

const fixturePaths = trustedFixturePaths(join(root, 'fixtures'));
if (fixturePaths.length > 0) {
  const checked = spawnSync('python', ['-m', 'py_compile', ...fixturePaths], { encoding: 'utf8' });
  if (checked.error) throw checked.error;
  if (checked.status !== 0) throw new Error(checked.stderr || checked.stdout || `py_compile exited ${checked.status}`);
}

console.log(`validated ${planFiles.length} trusted plans and syntax-checked ${fixturePaths.length} registered fixtures`);
