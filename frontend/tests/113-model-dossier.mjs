import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const [app, api, page] = await Promise.all([
  readFile(new URL("../src/App.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/api/modelDossier.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/ModelDossier.tsx", import.meta.url), "utf8")
]);

assert.match(app, /case "memory-models"/);
assert.match(app, /<ModelDossier workspaceId=/);
assert.match(api, /\/model-dossiers/);
assert.match(api, /encodeURIComponent\(modelVersionId\)/);
assert.match(page, /model_version_id/);
assert.match(page, /Browsing is context-neutral/);
assert.match(page, /does not add records to Jarvis context/);
// Spec 144 adds the existing modeling owner's draft-definition entry point so an
// operator can start a model from this surface. Version execution and generic
// mutation helpers remain outside this read-oriented dossier contract.
assert.match(page, /createModelSpec/);
assert.doesNotMatch(page, /updateModel|postJson|putJson|deleteJson|fetch\(/);

console.log("113 model dossier frontend contract: PASS");
