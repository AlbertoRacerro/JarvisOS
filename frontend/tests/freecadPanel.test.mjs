import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../src/components/bluecad/FreecadPanel.tsx", import.meta.url), "utf8");
const workbench = await readFile(new URL("../src/components/bluecad/BluecadWorkbench.tsx", import.meta.url), "utf8");
assert.match(workbench, /FreecadPanel/);
assert.match(source, />FreeCAD</);
assert.match(source, /FreeCAD non configurato/);
for (const code of ["FREECAD_ADDON_NOT_RUNNING", "FREECAD_MCP_UNAUTHORIZED", "FREECAD_MCP_DECLINED", "FREECAD_MCP_PENDING", "FREECAD_MCP_TOKEN_MISSING", "FREECAD_MCP_TOKEN_UNSAFE"]) {
  assert.match(source, new RegExp(code));
}
assert.match(source, /MCP On/);
assert.match(source, /modalità Ask/);
assert.match(source, /freecad_mcp_token/);
assert.match(source, /JARVISOS_FREECAD_PYTHON_SHA256/);
assert.match(source, /FreeCAD connesso/);
assert.match(source, /Anteprima/);
assert.match(source, /Applica/);
assert.match(source, /window\.confirm/);
assert.match(source, /document_unchanged/);
assert.match(source, /Proprietà di /);
assert.doesNotMatch(source, /execute_code|delete_object/);
assert.match(source, /Parte FreeCAD nella vista/);
assert.match(source, /Scegli file \.FCStd/);
assert.match(source, /Aggiungi alla vista/);
assert.match(source, /Rimuovi/);
assert.match(source, /STEP/);
assert.match(source, /STL/);
assert.match(source, /3MF/);
assert.match(workbench, /freecadRefs/);
assert.match(workbench, /FreecadPanel workspaceId=\{workspaceId\} candidateId=\{selectedId\} freecadRefs=\{freecadRefs\} onFreecadRefsChange=\{setFreecadRefs\}/);
assert.match(workbench, /\.\.\.freecadRefs\.filter\(\(reference\) => reference\.valid && reference\.glb_url\)/);
console.log("freecad panel static assertions passed");
