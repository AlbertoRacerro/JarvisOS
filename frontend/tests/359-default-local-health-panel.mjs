import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const panel = readFileSync(new URL('../src/components/coding/LocalModelPanel.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/api/llamaRuntime.ts', import.meta.url), 'utf8');

// Spec 359 Criterion 4: Fallback visibility in API & Settings/AI
assert.ok(api.includes('fallback_active?: boolean;'), 'LocalModels must expose fallback_active');
assert.ok(api.includes('fallback_from_id?: string | null;'), 'LocalModels must expose fallback_from_id');
assert.ok(api.includes('fallback_reason?: string | null;'), 'LocalModels must expose fallback_reason');

// Panel must render plain Italian fallback notice
assert.ok(panel.includes('models.fallback_active'), 'Panel must check models.fallback_active');
assert.ok(panel.includes('Ripristino automatico attivo:'), 'Panel must render Italian fallback notice');
assert.ok(panel.includes('models.fallback_reason'), 'Panel must show fallback reason');

// Panel must keep "Torna al modello standard" available to retry
assert.ok(panel.includes('Torna al modello standard'), 'Panel must offer Torna al modello standard');

// Ensure no unit names, ports or IDs exposed in visible text
assert.ok(!panel.includes('{entry.unit}'), 'Unit name must not be exposed');
assert.ok(!panel.includes('{entry.port}'), 'Port must not be exposed');
assert.ok(!panel.includes('<span>{entry.id}</span>'), 'ID must not be exposed');
assert.ok(!panel.includes('<span>{models.fallback_from_id}</span>'), 'Fallback model ID must not be exposed in visible text');

console.log('359 default local health panel tests passed');
