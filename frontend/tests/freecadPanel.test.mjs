import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../src/components/bluecad/FreecadPanel.tsx", import.meta.url), "utf8");
const workbench = await readFile(new URL("../src/components/bluecad/BluecadWorkbench.tsx", import.meta.url), "utf8");
assert.match(workbench, /FreecadPanel/);
assert.match(source, />FreeCAD</);
assert.match(source, /FreeCAD non configurato/);
assert.match(source, /Add-on FreeCAD non in esecuzione/);
assert.match(source, /FreeCAD connesso/);
assert.match(source, /Anteprima/);
assert.match(source, /Applica/);
assert.match(source, /window\.confirm/);
assert.match(source, /document_unchanged/);
console.log("freecad panel static assertions passed");
