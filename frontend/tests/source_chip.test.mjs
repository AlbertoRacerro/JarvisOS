// Spec 309: source and check-state chip on numeric inputs.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");

const sourceChipSrc = read("src/components/process/SourceChip.tsx");

// 1. All four source kinds map to exact Italian labels
assert.match(sourceChipSrc, /typed:\s*"Digitato"/);
assert.match(sourceChipSrc, /literature:\s*"Letteratura"/);
assert.match(sourceChipSrc, /ingested:\s*"Estratto"/);
assert.match(sourceChipSrc, /result:\s*"Risultato"/);

// 2. All five check states map to exact Italian labels
assert.match(sourceChipSrc, /unchecked:\s*"Non verificato"/);
assert.match(sourceChipSrc, /source_checked:\s*"Da fonte"/);
assert.match(sourceChipSrc, /verified:\s*"Verificato"/);
assert.match(sourceChipSrc, /flagged:\s*"Da controllare"/);
assert.match(sourceChipSrc, /stale:\s*"Fonte modificata"/);

// 3. SourceChip markup and attributes
assert.match(sourceChipSrc, /className="source-chip"/);
assert.match(sourceChipSrc, /data-source-kind=\{resolvedKind\}/);
assert.match(sourceChipSrc, /data-check-state=\{resolvedState\}/);
assert.match(sourceChipSrc, /title=\{resolvedLabel\}/);
assert.match(sourceChipSrc, /\{kindLabel\}\s*·\s*\{stateLabel\}/);

// 4. QuantityInput wiring
const quantityInputSrc = read("src/components/process/QuantityInput.tsx");
assert.match(quantityInputSrc, /import SourceChip from "\.\/SourceChip"/);
assert.match(quantityInputSrc, /hasStored\s*=\s*stored !== undefined/);
assert.match(quantityInputSrc, /hasTyped\s*=\s*current\.text\.trim\(\)\.length > 0/);
assert.match(quantityInputSrc, /showChip\s*=\s*hasStored\s*\|\|\s*hasTyped/);
assert.match(quantityInputSrc, /isEdited\s*=\s*!hasStored\s*\|\|\s*current\.text !== String\(stored\.value\)\s*\|\|\s*current\.unit !== stored\.unit/);
assert.match(quantityInputSrc, /<SourceChip source=\{chipSource\}\s*\/>/);

// 5. ProcessDraftEditor source preservation
const editorSrc = read("src/stages/ProcessDraftEditor.tsx");
assert.match(editorSrc, /function quantitiesFrom\(form:\s*FormState,\s*stored\?:/);
assert.match(editorSrc, /unchanged\s*&&/);
assert.match(editorSrc, /quantitiesFrom\(form,\s*stream\.spec/);
assert.match(editorSrc, /quantitiesFrom\(cultureForm,\s*stream\.spec\?\.culture\)/);
assert.match(editorSrc, /quantitiesFrom\(form,\s*unit\.params\)/);

// 6. Styles for source chip
const css = read("src/stages/ProcessDraftEditor.css");
assert.match(css, /\.source-chip \{/);
assert.match(css, /\.source-chip\[data-check-state="verified"\]/);
assert.match(css, /\.source-chip\[data-check-state="flagged"\]/);

// 7. package.json includes source_chip test in build
const pkg = read("package.json");
assert.match(pkg, /tests\/source_chip\.test\.mjs/);

console.log("309 source chip frontend ok");
