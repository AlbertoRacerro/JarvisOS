import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pageSource = fs.readFileSync(path.join(root, "src/pages/Settings.tsx"), "utf8");
const clientSource = fs.readFileSync(path.join(root, "src/api/client.ts"), "utf8");

// Spec 379: Settings > System shows the last backup and the last restore test, and nothing else about the backup.
assert.ok(pageSource.includes("<dt>Ultimo backup</dt>"), "System card must show 'Ultimo backup'");
assert.ok(pageSource.includes("<dt>Ultimo test di ripristino</dt>"), "System card must show 'Ultimo test di ripristino'");
assert.ok(pageSource.includes("data-backup-status"), "the backup line carries data-backup-status for the walk");
for (const label of ['"mai"', '"non riuscito"', "`vecchio (", '"riuscito"']) {
  assert.ok(pageSource.includes(label), `Settings must render the Italian backup label ${label}`);
}
assert.ok(clientSource.includes('state: "never" | "ok" | "failed" | "stale"'), "SystemInfoResponse.backup must type the four states");
assert.ok(!/backup\??\.(repositor|paths|snapshot|password|reason)/.test(pageSource),
  "Settings must not show backup repositories, paths, snapshot ids, key sources or failure reasons");

console.log("379 backup status checks passed");
