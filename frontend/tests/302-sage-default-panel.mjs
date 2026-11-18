import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const panel = readFileSync(new URL('../src/components/coding/LocalModelPanel.tsx', import.meta.url), 'utf8');

// Spec 302 Criterion 7: The panel shows the four states and the two actions in Italian
// with no unit name, port or id in visible text.
const states = ['in caricamento', 'pronto', 'fermo', 'errore'];
for (const state of states) {
  assert.ok(panel.includes(state), `Panel must include Italian state "${state}"`);
}

const actions = ['Usa questo modello', 'Torna al modello standard', ' · in uso', ' · non installato'];
for (const action of actions) {
  assert.ok(panel.includes(action), `Panel must include Italian action "${action}"`);
}

// Ensure no unit names, ports or ids in visible text
assert.ok(!panel.includes('{entry.unit}'), 'Unit name must not be exposed in visible text');
assert.ok(!panel.includes('{entry.port}'), 'Port must not be exposed in visible text');
assert.match(panel, /<strong>\{entry\.label\}<\/strong>/, 'Only label should be visibly shown for entry');
assert.ok(!panel.includes('<span>{entry.id}</span>'), 'ID must not be exposed in visible text');

// Criterion 6: a failed model must still offer the way back, so an error never disables the switch buttons.
assert.match(panel, /disabled=\{loading \|\| !!pending\} onClick=\{\(\) => void switchModel\(null\)\}/, 'Way back must stay enabled after an error');
assert.match(panel, /<button type="button" disabled=\{loading \|\| !!pending \|\| \(entry\.runtime/, 'Switch must stay enabled after an error');

console.log('302 sage default panel labels test passed');
