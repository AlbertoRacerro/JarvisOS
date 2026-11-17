import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const sidecar = readFileSync(new URL('../src/components/ai/useJarvisSidecar.tsx', import.meta.url), 'utf8');
const threadsApi = readFileSync(new URL('../src/api/threads.ts', import.meta.url), 'utf8');
const settings = readFileSync(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8');
const settingsApi = readFileSync(new URL('../src/api/settings.ts', import.meta.url), 'utf8');

// Acceptance Criterion 4:
// - Role chooser in the Hermes chat composer showing the four role names and purposes
for (const text of [
  'jarvis-role-chooser',
  'jarvis-role-options',
  'jarvis-role-option',
  'jarvis-role-name',
  'jarvis-role-purpose',
  'Isaac',
  'Scientific reasoning colleague; analytical problem solving and theory.',
  'Gregor',
  'Scientific internet researcher (runs with existing broker tools until web research is brokered).',
  'Q',
  'Tool-using agent for workspace operations and procedure execution.',
  'Alfred',
  'Mail, calendar, and documents assistant (runs with existing broker tools until mail and calendar are brokered).'
]) {
  assert.ok(sidecar.includes(text), `Expected sidecar to include: ${text}`);
}

// - Sheldon toggle when Isaac is selected
assert.match(sidecar, /selectedRoleId === "isaac"/);
assert.match(sidecar, /jarvis-sheldon-mode/);
assert.match(sidecar, /Sheldon mode/);

// - Label replies with the active role name
assert.match(sidecar, /authorLabel/);
assert.match(sidecar, /roleMatch\?\.display_name/);
assert.match(sidecar, /interaction\.route_class === "hermes:agent"/);
assert.match(sidecar, /interaction\.sheldon_mode \? "Sheldon"/);

// - Show an unavailable role model as unavailable with its reason, never silently replaced
assert.match(sidecar, /jarvis-role-unavailable/);
assert.match(sidecar, /roleUnavailableReason/);
assert.match(sidecar, /effectiveRouteAvailable/);
assert.match(sidecar, /if \(role && !role\.available\)/);
assert.match(sidecar, /disabled: route\.execution_class !== "agent"/);
assert.match(sidecar, /current === "hermes:agent"/);

// - Submitting thread interaction passes roleId and sheldonMode
assert.match(sidecar, /submitThreadInteraction\(/);
assert.match(sidecar, /roleId:\s*captured\.roleId/);
assert.match(sidecar, /sheldonMode:\s*captured\.sheldonMode/);

// API typing in threads.ts
assert.match(threadsApi, /export type HermesRoleOption/);
assert.match(threadsApi, /role_id:\s*string/);
assert.match(threadsApi, /roles\?:\s*HermesRoleOption\[\]/);
assert.match(threadsApi, /roleId\?:\s*string/);
assert.match(threadsApi, /sheldonMode\?:\s*boolean/);
assert.match(threadsApi, /sheldon_mode\?:\s*boolean/);

// Settings reads canonical routes, edits each role, and writes the selected route.
assert.match(settings, /data-hermes-role-models/);
assert.match(settings, /hermesRoleNames\.map\(role =>/);
assert.match(settings, /const canonical = snapshot\.settings\.hermes_role_routes/);
assert.match(settings, /saveAISetting\(\{ hermes_role_routes: \{ \[roleId\]: route \} \}\)/);
assert.match(settingsApi, /Record<string, number \| boolean \| Record<string, string>>/);

console.log('260 named roles frontend checks passed');
